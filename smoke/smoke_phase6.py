"""Smoke: Phase 6 — backup rotation, retention sweep, wallet coin history."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP_DIR = Path(tempfile.mkdtemp(prefix="smoke6_"))
TMP_DB = TMP_DIR / "smoke6.db"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(TMP_DB)

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name} {detail}")


# ── 1. config defaults ──
from config import settings  # noqa: E402

check("backup_keep default 14", settings.backup_keep == 14, settings.backup_keep)
check("retention defaults",
      (settings.retention_whisper_days, settings.retention_anon_message_days,
       settings.retention_anon_session_days, settings.retention_ledger_days,
       settings.retention_report_days)
      == (30, 60, 7, 365, 180))

# ── 2. backup rotation + collision suffix ──
from scripts.backup_db import run_backup  # noqa: E402

src = TMP_DIR / "src.db"
_con = sqlite3.connect(src)
_con.execute("CREATE TABLE t (x INTEGER)")
_con.execute("INSERT INTO t VALUES (1)")
_con.commit()
_con.close()

out = TMP_DIR / "backups"
paths = [run_backup(src, out, keep=3) for _ in range(4)]
snapshots = sorted(out.glob("database-*.db"))
check("rotation keeps exactly 3", len(snapshots) == 3,
      [p.name for p in snapshots])
check("newest snapshot survives", Path(paths[-1]) in snapshots, paths[-1])
_probe = sqlite3.connect(paths[-1])
_probe_row = _probe.execute("SELECT x FROM t").fetchone()
_probe.close()
check("backup is a readable sqlite file", _probe_row == (1,), _probe_row)

# Same-second collision: pre-create the exact name run_backup would pick, and
# roll past the second so the four rotation runs cannot share this stamp.
time.sleep(1.1)
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
collision = out / f"database-{stamp}.db"
collision.write_bytes(b"pre-existing")
p = run_backup(src, out, keep=3)
check("collision gets -N suffix, no overwrite",
      p != collision and p.exists() and collision.exists(), p.name)
check("rotation still ≤ 3 after collision", len(list(out.glob("database-*.db"))) <= 3)

# ── 3. retention sweep on a throwaway DB ──
import bot as bot_mod  # noqa: E402,F401  (full import graph)
from database.engine import init_db, engine  # noqa: E402
from database import async_session_factory, CoinTransaction, User, UserReport  # noqa: E402
from sqlalchemy import insert, select  # noqa: E402
from utils.retention import (  # noqa: E402
    purge_expired, start_retention, stop_retention, retention_loop,
    FIRST_SWEEP_DELAY, SWEEP_INTERVAL,
)

OLD = datetime(2000, 1, 1, 0, 0, 0)
FRESH = datetime.now() - timedelta(hours=1)


async def _retention_main() -> None:
    await init_db()

    # Only the ledger purges; every other table is "keep forever" (days=0).
    settings.retention_whisper_days = 0
    settings.retention_anon_message_days = 0
    settings.retention_anon_session_days = 0
    settings.retention_ledger_days = 1

    async with async_session_factory() as session:
        await session.execute(
            insert(CoinTransaction).values(
                user_id=999999999, amount=5.0, reason="daily", created_at=OLD
            )
        )
        await session.execute(
            insert(CoinTransaction).values(
                user_id=999999999, amount=-1.0, reason="whisper", created_at=FRESH
            )
        )
        await session.execute(
            insert(UserReport).values(
                reporter_id=1, reported_id=2, created_at=OLD
            )
        )
        await session.commit()

    counts = await purge_expired()
    check("purge returns all five tables",
          set(counts) == {"whispers", "anonymous_messages",
                          "anon_chat_sessions", "coin_ledger",
                          "user_reports"}, counts)
    check("old ledger row purged", counts["coin_ledger"] == 1, counts)
    check("old report row purged (180d)", counts["user_reports"] == 1, counts)
    check("fresh ledger row kept", counts["whispers"] == 0 and counts["coin_ledger"] == 1)

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(CoinTransaction).where(CoinTransaction.user_id == 999999999)
        )).scalars().all()
    check("only the fresh row remains", len(rows) == 1 and rows[0].amount == -1.0,
          [r.amount for r in rows])

    # days=0 disables the purge for that table even with ancient rows around
    settings.retention_ledger_days = 0
    counts = await purge_expired()
    check("days=0 keeps everything", counts["coin_ledger"] == 0, counts)

    # ── hooks: start/stop spawn and cancel the background task ──
    await start_retention()
    from utils import retention as ret_mod
    check("start spawns the sweep task",
          ret_mod._task is not None and not ret_mod._task.done())
    await start_retention()  # idempotent
    await stop_retention()
    check("stop clears the task", ret_mod._task is None)
    check("loop timings sane",
          FIRST_SWEEP_DELAY == 600.0 and SWEEP_INTERVAL == 86400.0)

    await engine.dispose()


asyncio.run(_retention_main())

# ── 4. dispatcher wires the retention hooks ──


async def _build_main() -> None:
    settings.bot_token = "123456789:TESTtoken_abcdefghijklmnop"
    _, dp = await bot_mod.build_bot_and_dispatcher()
    startup_names = [h.callback.__name__ for h in dp.startup.handlers]
    shutdown_names = [h.callback.__name__ for h in dp.shutdown.handlers]
    check("startup registers retention", "hook_start_retention" in startup_names,
          startup_names)
    check("shutdown registers retention", "hook_stop_retention" in shutdown_names,
          shutdown_names)


asyncio.run(_build_main())

# ── 5. wallet history UI ──
from keyboards import wallet_kb, wallet_history_kb  # noqa: E402
import keyboards.admin as ka  # noqa: E402
import handlers.admin as ha  # noqa: E402
import handlers.profile as profile  # noqa: E402
from utils.economy import REASON_LABELS, reason_label  # noqa: E402


def _cbs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


def _widths(markup):
    return [len(r) for r in markup.inline_keyboard]


for claimable in (True, False):
    kb = wallet_kb(claimable)
    check(f"wallet kb {claimable} has history button",
          "wallet:history" in _cbs(kb), _cbs(kb))
    check(f"wallet kb {claimable} is 3 rows of 1", _widths(kb) == [1, 1, 1],
          _widths(kb))

hkb = wallet_history_kb()
check("history kb returns to wallet", _cbs(hkb) == ["wallet:open"], _cbs(hkb))

check("profile has wallet_history handler",
      callable(getattr(profile, "wallet_history", None)))
check("profile has wallet_open handler",
      callable(getattr(profile, "wallet_open", None)))

check("backup button root-only",
      "admin:backup" in _cbs(ka.admin_panel_kb(is_root=True))
      and "admin:backup" not in _cbs(ka.admin_panel_kb(is_root=False)))
check("admin module uses shared reason_label",
      getattr(ha, "reason_label", None) is reason_label
      and not hasattr(ha, "REASON_LABELS"))

# every ledger reason the code actually writes must translate — both keyword
# (``reason="x"``) and the positional third argument of _ledger/log_coin
seen: set[str] = set()
call_pat = re.compile(
    r'(_ledger|log_coin|add_coins|deduct_coins)\([^)]*?"([a-z][a-z_]*)"'
)
kw_pat = re.compile(r'reason\s*=\s*"([a-z][a-z_]*)"')
for path in ROOT.rglob("*.py"):
    if "venv" in path.parts or ".opencode" in path.parts or "smoke" in path.parts:
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    for m in call_pat.finditer(text):
        seen.add(m.group(2))
    seen.update(kw_pat.findall(text))
uncovered = {r for r in seen if r not in REASON_LABELS}
check("every reason literal has a label", not uncovered, uncovered)
check("label map covers the documented codes",
      set(REASON_LABELS) >= {"welcome", "daily", "referral", "invitee", "gift",
                             "whisper", "match_girl", "match_boy",
                             "match_random", "refund_whisper", "refund_match"})


# ── 6. wallet_history handler end-to-end with fakes ──
class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class _FakeCallback:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _FakeMessage()
        self.answered = False

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True


async def _history_main() -> None:
    await init_db()

    # 12 rows → the handler must show only the newest 10, newest first
    async with async_session_factory() as session:
        base = datetime.now() - timedelta(days=2)
        for i in range(12):
            await session.execute(
                insert(CoinTransaction).values(
                    user_id=424242,
                    amount=5.0 if i % 2 == 0 else -2.5,
                    reason="daily" if i % 2 == 0 else "whisper",
                    created_at=base + timedelta(minutes=i),
                )
            )
        await session.commit()

    cb = _FakeCallback(424242)
    await profile.wallet_history(cb)
    check("history renders one edit", len(cb.message.edits) == 1,
          len(cb.message.edits))
    text, kwargs = cb.message.edits[0]
    body = [ln for ln in text.splitlines() if ln.startswith(("+", "-"))]
    check("history shows 10 rows", len(body) == 10, len(body))
    # i=11 (newest) is odd → -2.5 whisper; i=10 → +5 daily: strict desc order
    check("history is newest first",
          body[0].startswith("-2.5 — هزینهٔ نجوا")
          and body[1].startswith("+5 — جایزهٔ روزانه"), body[:2])
    check("history escapes/translates reasons", "پاداش رفرال" not in text)
    check("history markup returns to wallet",
          _cbs(kwargs["reply_markup"]) == ["wallet:open"], kwargs.keys())
    check("history answered", cb.answered)

    # empty ledger → toast, no edit
    cb2 = _FakeCallback(111222333)
    await profile.wallet_history(cb2)
    check("empty history only toasts",
          not cb2.message.edits and cb2.answered)

    await engine.dispose()


asyncio.run(_history_main())

# ── 7. rematch / report inbox / user flags / whisper maxlen ──
from keyboards import (  # noqa: E402
    BACK_TO_MENU,
    NEXT_CHAT_LABEL,
    REMATCH_LABEL,
    chat_end_kb,
    rematch_offer_kb,
)
import handlers.chat as chat_h  # noqa: E402

src_chat = (ROOT / "handlers" / "chat.py").read_text(encoding="utf-8")
src_whisper = (ROOT / "handlers" / "whisper.py").read_text(encoding="utf-8")

end_rows = [[b.text for b in row] for row in chat_end_kb(True).keyboard]
check("chat end kb: next chat first, rematch, menu last",
      end_rows == [[NEXT_CHAT_LABEL], [REMATCH_LABEL], [BACK_TO_MENU]], end_rows)
end_rows_solo = [[b.text for b in row] for row in chat_end_kb(False).keyboard]
check("chat end kb drops rematch when no partner",
      end_rows_solo == [[NEXT_CHAT_LABEL], [BACK_TO_MENU]], end_rows_solo)
roffer = rematch_offer_kb()
check("rematch offer kb accept/decline",
      _cbs(roffer) == ["rematch:accept", "rematch:decline"], _cbs(roffer))
check("rematch handlers exist",
      all(callable(getattr(chat_h, n, None)) for n in
          ("cb_rematch_ask", "cb_rematch_accept", "cb_rematch_decline",
           "next_chat", "rematch_request", "_ask_rematch")))
check("rematch offers module state",
      isinstance(chat_h.rematch_offers, dict) and chat_h._REMATCH_TTL == 600.0)
check("end flow builds its own keyboard (reply_kb param is gone)",
      "reply_kb" not in src_chat)
check("end cards carry the chat-ended menu",
      "chat_end_kb(True)" in src_chat
      and "chat_end_kb(user_id in last_partner)" in src_chat)
check("chat-ended labels have handlers",
      "F.text == NEXT_CHAT_LABEL" in src_chat
      and "F.text == REMATCH_LABEL" in src_chat)
check("last search mode is remembered", "last_mode[user_id] = mode" in src_chat)
check("decline records a lockout",
      "_rematch_declined.add" in src_chat and "_decline_key" in src_chat)
check("whisper escapes the chat-ended labels",
      "NEXT_CHAT_LABEL" in src_whisper and "REMATCH_LABEL" in src_whisper)
check("report_user stores a UserReport (source)",
      "session.add(UserReport(" in src_chat)
check("_announce_match has charge flag",
      "charge" in inspect.signature(chat_h._announce_match).parameters)

# ── 8. privacy: no numeric id on any user-facing surface ──
src_profile = (ROOT / "handlers" / "profile.py").read_text(encoding="utf-8")
src_inline = (ROOT / "keyboards" / "inline.py").read_text(encoding="utf-8")
src_anon = (ROOT / "handlers" / "anonymous.py").read_text(encoding="utf-8")
src_inline_anon = (ROOT / "handlers" / "inline_anon.py").read_text(encoding="utf-8")

check("blocked list labels carry no id",
      'label = f"کاربر {entry.blocked_id}"' not in src_profile
      and "کاربر {target_id}" not in src_profile)
check("blocked kb fallback carries no id",
      'f"کاربر {uid}"' not in src_inline)
check("anon inbox header carries no sender id",
      "از کاربر {sender_id}" not in src_anon)
check("no tg:// id href in user-facing cards",
      'href="tg://user?id=' not in src_whisper
      and 'href="tg://user?id=' not in src_inline_anon)
check("inline whisper cards never print a raw id",
      "شناسهٔ <code>" not in src_inline_anon
      and "({target_id})" not in src_inline_anon)

_u = _cbs(ka.admin_users_kb())
check("users kb has flags + reports",
      {"admin:user:flags", "admin:reports"} <= set(_u), _u)
check("users kb widths <= 2", max(_widths(ka.admin_users_kb())) <= 2,
      _widths(ka.admin_users_kb()))
check("admin handlers exist",
      all(callable(getattr(ha, n, None)) for n in
          ("cb_reports_inbox", "cb_flags_start", "msg_flags_target",
           "cb_flags_toggle", "cb_whisper_maxlen_start", "msg_whisper_maxlen")))

_w = _cbs(ka.admin_whisper_kb(SimpleNamespace(enabled=True, max_length=700)))
check("whisper kb has maxlen editor", "admin:whisper:maxlen" in _w, _w)


class _FakeEdit:
    """Message stand-in for ``_edit_or_answer``."""

    def __init__(self) -> None:
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    async def answer(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _FakeEdit()
        self.answers: list[tuple] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append(args)


async def _admin_ui_main() -> None:
    await init_db()

    async with async_session_factory() as session:
        if await session.scalar(
            select(User).where(User.telegram_id == 888777)
        ) is None:
            session.add(User(telegram_id=888777, first_name="Smoke"))
        session.add(UserReport(reporter_id=1, reported_id=2))
        session.add(UserReport(reporter_id=3, reported_id=4))
        await session.commit()

    # ── report inbox lists the stored rows ──
    cb = _FakeCallback("admin:reports")
    await ha.cb_reports_inbox(cb)
    check("inbox renders one card", len(cb.message.edits) == 1, cb.message.edits)
    text, kw = cb.message.edits[0]
    check("inbox lists both reports",
          "<code>1</code>" in text and "<code>3</code>" in text, text[:120])
    check("inbox back lands on panel",
          "admin:panel" in _cbs(kw["reply_markup"]), kw.keys())

    # ── flag toggle flips the column and re-renders the card ──
    cb2 = _FakeCallback("admin:flags:toggle:is_vip:888777")
    await ha.cb_flags_toggle(cb2)
    async with async_session_factory() as session:
        row = await session.scalar(
            select(User).where(User.telegram_id == 888777)
        )
        flipped_on = row.is_vip is True
    check("flag toggle turned is_vip on", flipped_on)
    text2, kw2 = cb2.message.edits[-1]
    check("flag card shows new state",
          "اشتراک ویژه: <b>فعال</b>" in text2, text2[:200])
    _btn_texts = [b.text for r in kw2["reply_markup"].inline_keyboard for b in r]
    check("flag kb marks VIP as on", "✅ اشتراک ویژه (VIP)" in _btn_texts,
          _btn_texts)
    check("flag kb carries the target id",
          "admin:flags:toggle:is_vip:888777" in _cbs(kw2["reply_markup"]))

    cb3 = _FakeCallback("admin:flags:toggle:is_vip:888777")
    await ha.cb_flags_toggle(cb3)  # toggle back — leave no permanent state
    async with async_session_factory() as session:
        row = await session.scalar(
            select(User).where(User.telegram_id == 888777)
        )
    check("flag toggle restored", row.is_vip is False)

    # ── whisper max_length persists through the same save path ──
    from utils.whisper_config import get_whisper_config  # noqa: E402

    await ha._mutate_whisper_config(max_length=1234)
    cfg = await get_whisper_config()
    check("maxlen persists", cfg.max_length == 1234, cfg.max_length)
    await ha._mutate_whisper_config(max_length=700)
    cfg = await get_whisper_config()
    check("maxlen restored", cfg.max_length == 700, cfg.max_length)

    await engine.dispose()


asyncio.run(_admin_ui_main())


# ── 9. rematch lockout + offer card, functionally ──
async def _rematch_lockout_main() -> None:
    await init_db()

    class _IdleState:
        async def get_state(self) -> str:
            return chat_h.ChatState.idle.state

    sent: list[tuple[int, str]] = []

    class _Bot:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            sent.append((chat_id, text))

    bot = _Bot()
    state = _IdleState()

    # A declined pair may never be asked again — either direction.
    chat_h._rematch_declined.add(chat_h._decline_key(11, 22))
    chat_h.last_partner[11] = 22
    refusal = await chat_h._ask_rematch(11, 22, bot, state, "Ali")
    check("declined ask is refused with the decline text",
          isinstance(refusal, str) and "رد" in refusal, refusal)
    check("declined ask forgets the stored partner",
          11 not in chat_h.last_partner)
    refusal_back = await chat_h._ask_rematch(22, 11, bot, state, "Sara")
    check("decline locks BOTH directions", isinstance(refusal_back, str),
          refusal_back)
    check("refusals send no card", not sent, sent)

    # A fresh pair gets exactly one card, naming the requester, no id.
    chat_h.last_partner[33] = 44
    res = await chat_h._ask_rematch(33, 44, bot, state, "Nima")
    check("fresh ask sends the offer", res is None, res)
    check("exactly one card to the partner",
          len(sent) == 1 and sent[0][0] == 44, sent)
    if sent:
        card = sent[0][1]
        check("offer names the requester, prints no numeric id",
              "Nima" in card and "33" not in card, card[:160])
    res2 = await chat_h._ask_rematch(33, 44, bot, state, "Nima")
    check("second ask while pending is refused",
          isinstance(res2, str) and "قبلاً" in res2, res2)
    check("dedupe sends no second card", len(sent) == 1, len(sent))

    chat_h.rematch_offers.clear()
    chat_h._rematch_declined.clear()
    chat_h.last_partner.clear()
    chat_h.last_mode.clear()

    await engine.dispose()


asyncio.run(_rematch_lockout_main())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL PHASE-6 SMOKE CHECKS PASSED")
