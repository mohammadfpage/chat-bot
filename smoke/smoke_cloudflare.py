"""Smoke: Cloudflare Containers deployment wiring.

Covers what no other smoke touches:
  1. config — postgres URL normalisation, database_dialect, container detection
  2. database/engine — engine_options() per backend
  3. ChatPair mirror schema
  4. FUNCTIONAL rebuild: persist → wipe RAM → rebuild_chat_state() →
     queue/pairs/FSM/last_mode restored, broken & banned entries demoted
  5. the persistence helpers themselves (_persist_pair/_persist_ended/leave)
  6. bot.py — build_fsm_storage backends, _install_stop_signal, startup order
  7. the OPTIONAL Cloudflare/Docker files under deploy/cloudflare/ — and the
     guarantee that none of them sit in the repo root (so no platform
     auto-runs `npx wrangler deploy`), plus the Render deploy config

Runs offline against a throwaway SQLite database; touches neither the repo's
database.db nor the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Print safely on a Windows console (verify.py does the same).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Must be set BEFORE config is imported: the engine and session factory are
# module-level, so the whole process has to run against the throwaway file.
TMP_DIR = Path(tempfile.mkdtemp(prefix="smoke_cf_"))
TMP_DB = TMP_DIR / "smoke_cf.db"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(TMP_DB)

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name} {detail}")


# ── 1. config: URL normalisation, dialect, container detection ──
from config import Settings, settings  # noqa: E402

pg = Settings(_env_file=None, database_url="postgres://user:pass@host:5432/db")
check("postgres:// upgraded to asyncpg",
      pg.database_url == "postgresql+asyncpg://user:pass@host:5432/db",
      pg.database_url)
check("dialect of pasted postgres url", pg.database_dialect == "postgresql",
      pg.database_dialect)
plain = Settings(_env_file=None, database_url="postgresql://u:p@h/db")
check("postgresql:// upgraded too",
      plain.database_url == "postgresql+asyncpg://u:p@h/db", plain.database_url)
typed = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/db")
check("explicit driver left alone",
      typed.database_url == "postgresql+psycopg://u:p@h/db", typed.database_url)
check("local run reports sqlite", settings.database_dialect == "sqlite",
      settings.database_dialect)
check("not a container by default", settings.in_cloudflare_container is False)
on = Settings(_env_file=None, cloudflare_deployment_id="deploy-123")
check("deployment id flips detection", on.in_cloudflare_container is True)
check("sqlite url absolutised under project root",
      settings.database_url.startswith("sqlite+aiosqlite:///")
      and not settings.database_url[len("sqlite+aiosqlite:///"):].startswith("."),
      settings.database_url)

# ── 2. engine_options per backend ──
from database.engine import engine_options  # noqa: E402

sq = engine_options("sqlite+aiosqlite:///x.db")
check("sqlite gets a 30s busy timeout", sq == {"connect_args": {"timeout": 30}}, sq)
pgopts = engine_options("postgresql+asyncpg://u:p@h/db")
check("postgres pins timezone UTC",
      pgopts.get("connect_args", {}).get("server_settings") == {"timezone": "UTC"},
      pgopts)
check("postgres pool sized for the container",
      (pgopts.get("pool_size"), pgopts.get("max_overflow"), pgopts.get("pool_recycle"))
      == (10, 20, 1800), pgopts)
check("unknown backend gets no options", engine_options("mysql://h/db") == {})

# ── 3. dependency pins ──
req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("requirements pins asyncpg", "asyncpg==" in req)
check("requirements pins redis", "redis==" in req)

# ── 4. ChatPair mirror schema ──
from database import ChatPair  # noqa: E402

check("tablename", ChatPair.__tablename__ == "chat_pairs", ChatPair.__tablename__)
cols = {c.name for c in ChatPair.__table__.columns}
check("columns",
      {"user_id", "partner_id", "status", "mode", "gender", "opened_at"} <= cols,
      cols)
_pk = ChatPair.__table__.c.user_id
check("user_id is PK without autoincrement",
      bool(_pk.primary_key) and _pk.autoincrement is False,
      (_pk.primary_key, _pk.autoincrement))
check("status is indexed", bool(ChatPair.__table__.c.status.index))

# ── 5. functional rebuild on a throwaway database ──
import bot as bot_mod  # noqa: E402,F401  (full import graph, like every smoke)
import handlers.chat as chat_h  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from database import User, async_session_factory  # noqa: E402
from database.engine import engine, init_db  # noqa: E402
from utils.economy import MODE_BOY, MODE_GIRL  # noqa: E402


class _FakeBot:
    """Minimal Bot stand-in: rebuild never talks to Telegram for fresh rows."""

    id = 777

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))


async def _rebuild_main() -> None:
    await init_db()
    now = time.time()

    async with async_session_factory() as session:
        session.add_all([
            ChatPair(user_id=101, status="queued", partner_id=None,
                     mode=None, gender="female"),
            ChatPair(user_id=201, status="paired", partner_id=202,
                     mode=MODE_BOY, gender="male", opened_at=now),
            ChatPair(user_id=202, status="paired", partner_id=201,
                     mode=None, gender=None, opened_at=now),
            # one-sided pair: the partner row does not exist → demote
            ChatPair(user_id=301, status="paired", partner_id=999,
                     mode=MODE_GIRL, gender="female", opened_at=now),
            # banned user with a live row → demote
            User(telegram_id=401, first_name="Banned", is_banned=True),
            ChatPair(user_id=401, status="queued", partner_id=None,
                     mode=None, gender=None),
        ])
        await session.commit()

    # Pretend the process just woke: every RAM map empty.
    chat_h.pair_map.clear()
    chat_h.search_queue.clear()
    chat_h.chat_opened_at.clear()
    chat_h.last_mode.clear()

    storage = MemoryStorage()
    fake_bot = _FakeBot()
    await chat_h.rebuild_chat_state(fake_bot, storage)

    check("queue restored",
          chat_h.search_queue.get(101) == (None, "female"),
          dict(chat_h.search_queue))
    check("mutual pair restored both ways",
          chat_h.pair_map.get(201) == 202 and chat_h.pair_map.get(202) == 201,
          dict(chat_h.pair_map))
    check("one-sided pair kept out of RAM", 301 not in chat_h.pair_map,
          dict(chat_h.pair_map))
    check("banned user kept out of queue", 401 not in chat_h.search_queue,
          dict(chat_h.search_queue))
    check("last_mode restored, including ended rows",
          chat_h.last_mode.get(201) == MODE_BOY
          and chat_h.last_mode.get(301) == MODE_GIRL, dict(chat_h.last_mode))
    check("expiry clock translated from epoch to monotonic",
          abs(time.monotonic() - chat_h.chat_opened_at.get(201, 0)) < 60,
          chat_h.chat_opened_at.get(201))

    q_key = StorageKey(bot_id=777, chat_id=101, user_id=101)
    p_key = StorageKey(bot_id=777, chat_id=201, user_id=201)
    check("FSM back to in_queue for the waiter",
          await storage.get_state(q_key) == chat_h.ChatState.in_queue.state,
          await storage.get_state(q_key))
    check("FSM back to in_chat for both partners",
          await storage.get_state(p_key) == chat_h.ChatState.in_chat.state,
          await storage.get_state(p_key))
    check("fresh rebuild sends nothing to Telegram", not fake_bot.sent,
          fake_bot.sent)

    async with async_session_factory() as session:
        r301 = await session.get(ChatPair, 301)
        r401 = await session.get(ChatPair, 401)
        r101 = await session.get(ChatPair, 101)
        r201 = await session.get(ChatPair, 201)
    check("demotion persisted as ended (one-sided)",
          r301 is not None and r301.status == "ended"
          and r301.partner_id is None, r301)
    check("demotion persisted as ended (banned)",
          r401 is not None and r401.status == "ended", r401)
    check("healthy rows untouched by demotion",
          r101.status == "queued" and r201.status == "paired"
          and r201.partner_id == 202, (r101.status, r201.status))

    # ── 5b. the write path itself ──
    await chat_h._persist_pair(701, 702,
                               user_mode=MODE_GIRL, user_gender="female")
    async with async_session_factory() as session:
        a = await session.get(ChatPair, 701)
        b = await session.get(ChatPair, 702)
    check("pair written in both directions",
          a.status == b.status == "paired"
          and a.partner_id == 702 and b.partner_id == 701,
          (a.status, b.status, a.partner_id, b.partner_id))
    check("pair stamps opened_at", a.opened_at is not None, a.opened_at)

    await chat_h._persist_ended(701, 702, 555555)  # 555555 has no row at all
    async with async_session_factory() as session:
        a = await session.get(ChatPair, 701)
        ghost = await session.get(ChatPair, 555555)
    check("end marks both sides, inserts nothing for strangers",
          a.status == "ended" and a.partner_id is None and ghost is None,
          (a.status, ghost))

    chat_h.search_queue[601] = (None, None)
    await chat_h._persist_queue(601, None, "male")
    await chat_h.leave_search_queue(601)
    async with async_session_factory() as session:
        r601 = await session.get(ChatPair, 601)
    check("leave_search_queue empties RAM and the mirror",
          601 not in chat_h.search_queue
          and r601 is not None and r601.status == "ended",
          (601 in chat_h.search_queue, r601.status if r601 else None))

    # leave the module state clean for any later hook in this process
    chat_h.pair_map.clear()
    chat_h.search_queue.clear()
    chat_h.chat_opened_at.clear()
    chat_h.last_mode.clear()
    await engine.dispose()


asyncio.run(_rebuild_main())

# ── 6. bot.py wiring ──
check("empty REDIS_URL → MemoryStorage",
      isinstance(bot_mod.build_fsm_storage(""), MemoryStorage))
_r = bot_mod.build_fsm_storage("redis://default:secret@127.0.0.1:6379/0")
check("redis URL → RedisStorage", type(_r).__name__ == "RedisStorage",
      type(_r).__name__)

check("stop signal is a no-op without a loop", bot_mod._install_stop_signal() is None)


async def _wire_main() -> None:
    # _install_stop_signal inside the loop that runs the bot (main() does this)
    bot_mod._install_stop_signal()
    bot_mod.settings.bot_token = "123456789:TESTtoken_abcdefghijklmnop"
    _, dp = await bot_mod.build_bot_and_dispatcher()
    names = [h.callback.__name__ for h in dp.startup.handlers]
    check("rebuild registered FIRST on startup",
          bool(names) and names[0] == "hook_rebuild_chat_state", names)
    check("other startup hooks still present",
          {"hook_start_chat_expiry", "hook_start_retention"} <= set(names), names)


asyncio.run(_wire_main())

# ── 7. optional Cloudflare/Docker files live under deploy/cloudflare/ ──
CF = ROOT / "deploy" / "cloudflare"
raw = (CF / "wrangler.jsonc").read_text(encoding="utf-8")
body = "\n".join(
    line for line in raw.splitlines() if not line.strip().startswith("//")
)
cfg = json.loads(body)  # raises → smoke fails loudly on a syntax error

cont = (cfg.get("containers") or [{}])[0]
check("one container class from the local Dockerfile",
      cont.get("class_name") == "BotContainer" and cont.get("image") == "./Dockerfile",
      cont)
check("max_instances pinned to 1 (RAM state is per-process)",
      cont.get("max_instances") == 1, cont.get("max_instances"))
bind = (cfg.get("durable_objects", {}).get("bindings") or [{}])[0]
check("Durable Object binding",
      bind.get("name") == "BOT_CONTAINER" and bind.get("class_name") == "BotContainer",
      bind)
check("exports declare the DO with sqlite storage",
      cfg.get("exports", {}).get("BotContainer")
      == {"type": "durable-object", "storage": "sqlite"},
      cfg.get("exports"))
check("no legacy migrations next to exports", "migrations" not in cfg,
      "migrations" in cfg)
check("main points at the worker", cfg.get("main") == "worker/index.js",
      cfg.get("main"))

v = cfg.get("vars", {})
check("webhook mode + default path",
      v.get("RUN_MODE") == "webhook" and v.get("WEBHOOK_PATH") == "/telegram/webhook",
      (v.get("RUN_MODE"), v.get("WEBHOOK_PATH")))
check("binds all interfaces on the container port",
      v.get("WEBAPP_HOST") == "0.0.0.0" and v.get("WEBAPP_PORT") == "8080",
      (v.get("WEBAPP_HOST"), v.get("WEBAPP_PORT")))
check("group sweep off, file logging off",
      v.get("GROUP_KEYBOARD_CLEANUP") == "false" and v.get("LOG_FILE") == "",
      (v.get("GROUP_KEYBOARD_CLEANUP"), v.get("LOG_FILE")))
check("webhook base deliberately empty until the first deploy",
      v.get("WEBHOOK_BASE_URL") == "", v.get("WEBHOOK_BASE_URL"))
_secrets = {"BOT_TOKEN", "DATABASE_URL", "WEBHOOK_SECRET", "REDIS_URL", "PROXY_URL"}
check("no secrets in vars", not (_secrets & set(v)), _secrets & set(v))

# ── 8. .dockerignore (optional Cloudflare image) ──
di = {
    line.strip()
    for line in (CF / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.strip().startswith("#")
}
_needed = {".env", "venv/", ".git/", "logs/", "backups/",
           "*.db", "*.db-wal", "*.db-shm", "node_modules/"}
check(".dockerignore keeps secrets and local state out of the image",
      _needed <= di, _needed - di)
check(".env.example still ships (documented, harmless)",
      "!.env.example" in di, di)

# ── 9. Dockerfile (optional Cloudflare image) ──
df = (CF / "Dockerfile").read_text(encoding="utf-8")
check("python 3.12 base", "FROM python:3.12" in df, df.splitlines()[:6])
check("deps installed before code", "requirements.txt" in df and "COPY . ." in df)
check("exposes the webhook port", "EXPOSE 8080" in df)
check("starts bot.py", 'CMD ["python", "bot.py"]' in df)

# ── 10. worker/index.js ──
w = (CF / "worker" / "index.js").read_text(encoding="utf-8")
check("imports @cloudflare/containers", 'from "@cloudflare/containers"' in w)
check("one named container instance", 'getByName("bot")' in w)
check("proxy port matches WEBAPP_PORT", "defaultPort = 8080" in w)
check("idles after 10 minutes", 'sleepAfter = "10m"' in w)
check("forwards vars + secrets as string env entries",
      'typeof value === "string"' in w and "this.envVars = forwarded" in w)
check("gates on the configured webhook path (POST only)",
      "env.WEBHOOK_PATH" in w and "request.method" in w)
check("health route is forwarded", '"/health"' in w)
check("everything else 404s without waking the bot", "404" in w)
check("no container fetch on the 404 path",
      w.count("BOT_CONTAINER") == 1, w.count("BOT_CONTAINER"))

# ── 11. package.json (optional Cloudflare tooling) ──
pkg = json.loads((CF / "package.json").read_text(encoding="utf-8"))
check("containers library pinned",
      (pkg.get("dependencies", {}).get("@cloudflare/containers") or "").startswith("^0."),
      pkg.get("dependencies"))
check("wrangler pinned as a dev dependency",
      (pkg.get("devDependencies", {}).get("wrangler") or "").startswith("^4."),
      pkg.get("devDependencies"))
check("worker module is ESM", pkg.get("type") == "module", pkg.get("type"))

# ── 12. the CURRENT deployment path must NOT be Cloudflare/Docker ──
# None of the Wrangler/Docker triggers may sit in the repository root, or a
# Python host could auto-detect a Node/Worker project and run
# `npx wrangler deploy` (the failure this whole layout exists to prevent).
for _name in ("package.json", "package-lock.json", "wrangler.jsonc",
              "wrangler.toml", "wrangler.json", "Dockerfile"):
    check(f"root has no {_name}", not (ROOT / _name).exists())
check("root has no worker/ directory", not (ROOT / "worker").exists())

rend = (ROOT / "render.yaml").read_text(encoding="utf-8")
check("render.yaml declares a python web service",
      "type: web" in rend and "runtime: python" in rend)
check("render.yaml installs requirements",
      "pip install -r requirements.txt" in rend)
check("render.yaml starts the bot directly",
      "startCommand: python bot.py" in rend)
check("render.yaml runs webhook mode on the platform port",
      "RUN_MODE" in rend and "value: webhook" in rend and "WEBAPP_HOST" in rend)
check("render.yaml exposes /health", "healthCheckPath: /health" in rend)
# Comments may mention Wrangler/Docker; the actual commands must not.
_cmds = "\n".join(
    line for line in rend.splitlines() if not line.strip().startswith("#")
).lower()
check("render.yaml never runs wrangler or docker",
      "wrangler" not in _cmds and "docker" not in _cmds)

# ── 13. PORT support in config ──
from config import Settings as _S  # noqa: E402
_pl = _S(_env_file=None, port="10000")
check("PORT overrides WEBAPP_PORT", _pl.webapp_port == 10000, _pl.webapp_port)
check("PORT widens the bind address when WEBAPP_HOST is unset",
      _pl.webapp_host == "0.0.0.0", _pl.webapp_host)
_explicit = _S(_env_file=None, port="10000", webapp_host="127.0.0.1")
check("explicit WEBAPP_HOST wins over PORT default",
      _explicit.webapp_host == "127.0.0.1", _explicit.webapp_host)
check("no PORT keeps the default WEBAPP_PORT",
      _S(_env_file=None).webapp_port == 8080)

# ── 14. env example documents the container knobs ──
env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
check(".env.example documents REDIS_URL", "REDIS_URL" in env_example)
check(".env.example documents DATABASE_URL", "DATABASE_URL" in env_example)
check(".env.example documents PORT", "PORT=" in env_example)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CLOUDFLARE SMOKE CHECKS PASSED")
