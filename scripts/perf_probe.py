"""
Offline latency probe: how many round trips does ONE update really cost?

Run from the repo root:

    python scripts/perf_probe.py

What it does (no network, no real Telegram, no production database):

* isolates the DB to a throwaway SQLite file in the OS temp dir (the real
  ``DATABASE_URL`` in ``.env`` is overridden in ``os.environ`` BEFORE the
  config stack imports);
* monkeypatches the API session so every Telegram call is recorded and
  answered with a canned payload — nothing leaves the machine;
* counts, per dispatched update:
    - FSM storage reads / writes / cache hits inside ``DatabaseStorage``,
    - raw SQL statements executed by the engine,
    - connection-pool checkouts (each one also pays a pre-ping on a remote DB),
    - outbound Telegram API calls;
* dispatches a realistic sequence: a warm update, then a repeat from the same
  user, then one chat-menu tap and one unmatched text — enough shapes to see
  both the cold-cache and the warm-cache path of any FSM change.

The counts are what matter on a remote PostgreSQL (Neon): on the production
logs, ONE statement costs ~200-340 ms at 150 ms slow threshold, so
``statements per update × RTT`` is essentially the bot's per-update wait.
Interpret with ``middleware/timing.py``'s production lines (``update`,
``fsm get_state``, ``slow DB query``) which carry the real milliseconds.

Never imports secrets into output: no token, no URL, no message content.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import MethodType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── isolation FIRST: depth-of-environment before anything imports config ──
_TMP = tempfile.mkdtemp(prefix="perf_probe_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/probe.db"
os.environ["LOG_FILE"] = ""  # probe logs stay on stdout, never in the repo logs/

import config  # noqa: E402

config.settings.bot_token = "123456789:TESTtoken_probe_no_secret"
config.settings.proxy_url = ""

# NOTE: ``import database.engine as dbeng`` would bind the re-exported
# AsyncEngine instance (Python's ``import a.b as c`` prefers ``getattr(a, 'b')``
# and ``database/__init__.py`` re-exports ``engine``), not the module — bind
# the module WITHOUT an alias to reach its real attributes.
import database.engine  # noqa: E402,F401
from database import engine as _db_engine  # noqa: E402
from database import init_db, fsm as fsm_mod  # noqa: E402

STATS: dict[str, int] = {}
BTG: list[str] = []


def _bump(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _n(key: str) -> int:
    """Current counter for ``key`` — 0 when it was never bumped."""
    return STATS.get(key, 0)


# ── count engine statements + pool checkouts ─────────────────────────────
from sqlalchemy import event  # noqa: E402

_last_stmts: list[str] = []
_UPD_TAG = {"tag": "boot"}


def _kinds_of(statement: str) -> str:
    """Coarse label so totals say WHICH work is left per update."""
    s = " ".join((statement or "").split())
    head = s.split("(")[0][:60]
    return head


def _query_flat(statement: str) -> str:
    s = " ".join((statement or "").split())
    return s[:120] + ("…" if len(s) > 120 else "")


def _ctx_no() -> str:
    return _UPD_TAG["tag"]


@event.listens_for(_db_engine.sync_engine, "after_cursor_execute")
def _count_stmt(conn, cursor, statement, parameters, context, executemany):
    _bump(STATS, "db_statements")
    key = _kinds_of(statement)
    _bump(STATS, f"stmt:{key}")
    _last_stmts.append(f"[{_ctx_no()}] {key}: " + _query_flat(statement))


@event.listens_for(_db_engine.sync_engine, "checkout")
def _count_checkout(dbapi_conn, record, *args):
    _bump(STATS, "db_checkouts")


# ── count FSM operations at the DatabaseStorage boundary ────────────────
_orig_read_state = fsm_mod.DatabaseStorage._read_state
_orig_read_data = fsm_mod.DatabaseStorage._read_data
_orig_upsert = fsm_mod.DatabaseStorage._upsert


def _fsm_column_is_cached(sk: str, column: int) -> bool:
    """True when the requested column is already known in the process cache.

    ``column`` is the index of its "known" flag: 0 = state, 2 = data.
    """
    cached = fsm_mod._cache_get(sk)
    return cached is not None and bool(cached[column])


async def _counting_read_state(self, key):
    sk = fsm_mod._storage_key(key)
    if _fsm_column_is_cached(sk, 0):
        _bump(STATS, "fsm_cache_hits")
    _bump(STATS, "fsm_reads")
    return await _orig_read_state(self, key)


async def _counting_read_data(self, key):
    sk = fsm_mod._storage_key(key)
    if _fsm_column_is_cached(sk, 2):
        _bump(STATS, "fsm_cache_hits")
    _bump(STATS, "fsm_reads")
    return await _orig_read_data(self, key)


async def _counting_upsert(self, key, **kwargs):
    _bump(STATS, "fsm_writes")
    return await _orig_upsert(self, key, **kwargs)


fsm_mod.DatabaseStorage._read_state = _counting_read_state
fsm_mod.DatabaseStorage._read_data = _counting_read_data
fsm_mod.DatabaseStorage._upsert = _counting_upsert

# ── fake Telegram transport: record + answer, never touch the network ──
from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402


def _user(user_id: int, first_name: str) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": first_name}


def _message_payload(chat_id: int, chat_type: str = "private") -> dict:
    return {
        "message_id": 7001 + (BTG.count("sendMessage")),
        "from": _user(chat_id, "کاربر"),
        "chat": {"id": chat_id, "type": chat_type},
        "date": 0,
    }


_ORIG_MAKE_REQUEST = AiohttpSession.make_request


async def _fake_make_request(self, bot, method, timeout=None):
    """Mirror aiogram's ``make_request(bot, method, timeout)`` signature.

    ``method`` is a ``TelegramMethod`` instance (Pydantic, unhashable), so the
    API name comes from ``__api_method__`` and arguments from its attributes.
    """
    name = getattr(method, "__api_method__", type(method).__name__)
    _bump(STATS, "tg_calls")
    BTG.append(name)
    if name == "sendMessage":
        return _message_payload(getattr(method, "chat_id", 0) or 0)
    if name == "getChatMember":
        return {
            "user": _user(getattr(method, "user_id", 0), "کاربر"),
            "status": "member",
        }
    if name == "getChat":
        return {"id": getattr(method, "chat_id", 0) or 0, "type": "private", "title": "کاربر"}
    if name in {"setWebhook", "deleteWebhook", "answerCallbackQuery"}:
        return True
    # Anything unexpected aborts loudly so the map grows deliberately.
    raise AssertionError(f"perf_probe: stub missing for Telegram method {name}")


AiohttpSession.make_request = _fake_make_request

import bot as bot_mod  # noqa: E402
from aiogram.types import Chat, Message, Update  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402

_UPDATE_SEQ = iter(range(9_000_000, 9_010_000))


def _private_message(text: str, user_id: int = 555_000) -> Update:
    msg = Message(
        message_id=next(_UPDATE_SEQ),
        from_user=TgUser(id=user_id, is_bot=False, first_name="کاربر"),
        chat=Chat(id=user_id, type="private"),
        date=0,
        text=text,
    )
    return Update(update_id=next(_UPDATE_SEQ), message=msg)


async def _dispatch(bot, dp, upd: Update, tag: str) -> None:
    t0 = time.perf_counter()
    stats0 = dict(STATS)
    stmt0 = len(_last_stmts)
    _UPD_TAG["tag"] = tag
    try:
        await dp.feed_update(bot, upd)
    except Exception as exc:  # never hide: report, keep counting
        print(f"    !! dispatch raised {type(exc).__name__}: {exc}")
        traceback.print_exc()
    STATS["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    print(
        f"    fsm R/W/hit={_n('fsm_reads') - stats0.get('fsm_reads', 0)}/"
        f"{_n('fsm_writes') - stats0.get('fsm_writes', 0)}/"
        f"{_n('fsm_cache_hits') - stats0.get('fsm_cache_hits', 0)}  "
        f"db_stmt={_n('db_statements') - stats0.get('db_statements', 0)}  "
        f"checkouts={_n('db_checkouts') - stats0.get('db_checkouts', 0)}  "
        f"tg={_n('tg_calls') - stats0.get('tg_calls', 0)}  "
        f"time(offline)~{STATS['elapsed_ms']}ms  "
        f"tg_methods={BTG[stats0.get('tg_calls', 0):]}"
    )
    for line in _last_stmts[stmt0:]:
        print(f"        SQL {line}")


async def main() -> None:
    t0 = time.perf_counter()
    await init_db()
    print(f"init_db: {int((time.perf_counter() - t0) * 1000)}ms "
          f"-- statements so far: {STATS['db_statements']}")

    # Patch Bot.freeze/create fiber only if needed... (nothing: dispatch only)
    bot, dp = await bot_mod.build_bot_and_dispatcher()

    print("\nPer-update counters (each line = one dispatched update):")
    print("• update 1: menu label (cold FSM cache)")
    await _dispatch(bot, dp, _private_message("🏠 منوی اصلی"), "u1")

    print("• update 2: menu label again (warm)")
    await _dispatch(bot, dp, _private_message("🏠 منوی اصلی"), "u2")

    print("• update 3: /menu command")
    await _dispatch(bot, dp, _private_message("/menu"), "u3")

    print("• update 4: unmatched text (worst-case router traversal)")
    await _dispatch(bot, dp, _private_message("سلام یک پیام معمولی"), "u4")

    print(
        "\nTotals:",
        dict(sorted(STATS.items(), key=lambda kv: -kv[1])),
    )
    print("Telegram methods seen:", sorted(set(BTG)))
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
