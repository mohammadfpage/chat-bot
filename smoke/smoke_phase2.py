"""Phase-2 infra smoke: config anchoring, SQLite PRAGMAs, migration guard,
logging setup, hook wrappers."""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
import logging

TMP_DB = r"C:\Users\Amin\AppData\Local\Temp\opencode\phase2_old.db"
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(TMP_DB + suffix)
    except FileNotFoundError:
        pass

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + TMP_DB
os.environ["LOG_FILE"] = r"C:\Users\Amin\AppData\Local\Temp\opencode\phase2.log"
os.environ["LOG_LEVEL"] = "DEBUG"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

checks = []
def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

# ── old-schema bot_policy: no messages_per_minute/hour, no daily_bonus ──
conn = sqlite3.connect(TMP_DB)
conn.execute(
    "CREATE TABLE bot_policy (id INTEGER PRIMARY KEY, whisper_cost REAL DEFAULT 1, "
    "chat_girl_cost REAL DEFAULT 1, chat_boy_cost REAL DEFAULT 1, random_chat_cost REAL DEFAULT 0)"
)
conn.execute("INSERT INTO bot_policy (id) VALUES (1)")
conn.commit()
conn.close()

import config
check("config .env anchored to BASE_DIR", str(config.BASE_DIR / ".env") == config.Settings.model_config["env_file"])
check("config DB url absolutized", config.settings.database_url.startswith("sqlite+aiosqlite:///") and os.path.isabs(config.settings.database_url.split("///", 1)[1]))
check("config LOG_LEVEL read from env", config.settings.log_level == "DEBUG")

from database.engine import init_db, engine
import bot as bot_mod

async def main():
    await init_db()
    check("init_db on old schema did not crash", True)
    async with engine.connect() as cur:
        jm = (await cur.exec_driver_sql("PRAGMA journal_mode")).scalar()
        bt = (await cur.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        cols = [r[1] for r in (await cur.exec_driver_sql("PRAGMA table_info(bot_policy)")).fetchall()]
    check(f"WAL active (got {jm})", jm == "wal")
    check(f"busy_timeout=5000 (got {bt})", bt == 5000)
    check("messages_per_minute added", "messages_per_minute" in cols)
    check("messages_per_hour added", "messages_per_hour" in cols)
    check("daily_bonus_coins added", "daily_bonus_coins" in cols)
    await init_db()
    check("init_db idempotent (2nd run)", True)
    await engine.dispose()

asyncio.run(main())

conn = sqlite3.connect(TMP_DB)
row = conn.execute("SELECT messages_per_minute, messages_per_hour FROM bot_policy").fetchone()
conn.close()
check(f"old default 6/40 widened to 20/300 (got {row})", row == (20, 300))

# ── logging: file sink + token redaction ──
bot_mod._setup_logging()
root = logging.getLogger()
check("console handler installed", any(type(h).__name__ == "StreamHandler" for h in root.handlers))
check("rotating file handler installed", any(type(h).__name__ == "RotatingFileHandler" for h in root.handlers))
rec = logging.LogRecord("t", logging.ERROR, __file__, 1,
                        "url=https://api.telegram.org/bot123456789:AAHdqgcvru456789012345678901234567890/sendMessage", (), None)
flt = bot_mod._TokenRedactingFilter()
flt.filter(rec)
check("token redacted in logs", "AAHdqgcvru" not in rec.getMessage() and "***:***" in rec.getMessage())
check("LOG_LEVEL=DEBUG honoured", root.level == logging.DEBUG)

# ── hook wrapper: failure doesn't propagate, kwargs filtered ──
seen = {}
async def _boom(bot):  # accepts only 'bot'
    seen["bot"] = bot
    raise RuntimeError("hook exploded")
wrapped = bot_mod._hook("boom", _boom)
async def _emit():
    await wrapped(bot="B", dp="D", storage="S")  # extra kwargs must be filtered
asyncio.run(_emit())
check("hook kwargs filtered to signature", seen.get("bot") == "B")

print(f"\n{sum(1 for _, ok in checks if ok)}/{len(checks)} checks passed")
sys.exit(0 if all(ok for _, ok in checks) else 1)
