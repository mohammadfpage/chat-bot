"""
Age-based retention sweep — the append-only tables that nothing else
ever purges: ``whispers``, ``anonymous_messages``, ``anon_chat_sessions``,
``coin_ledger`` and ``user_reports``.

Why in-process instead of cron: the bot already owns the database and the
event loop, a cron entry would depend on the deploy environment (systemd,
Windows task, docker), and a missed cron run silently means "data kept
forever". A startup hook + background task travels with the code.

Timing: first sweep 10 minutes after boot (so startup work is not competing
with a DELETE), then once per 24 h. ``*_days = 0`` disables that table's
purge (keep forever). Timestamps come from ``func.now()`` = SQLite
``CURRENT_TIMESTAMP`` = naive UTC, so cutoffs are naive UTC too.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from config import settings
from database import async_session_factory
from database.models import (
    AnonChatSession,
    AnonymousMessage,
    CoinTransaction,
    UserReport,
    Whisper,
)

logger = logging.getLogger(__name__)

#: Delay before the first sweep — give boot-time migrations and the keyboard
#: cleanup room to finish before opening a write-heavy DELETE.
FIRST_SWEEP_DELAY = 600.0
#: Time between sweeps.
SWEEP_INTERVAL = 24 * 3600.0

_task: asyncio.Task | None = None


def _utc_now_naive() -> datetime:
    """Naive UTC ``now`` matching what SQLite's ``CURRENT_TIMESTAMP`` stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def purge_expired() -> dict[str, int]:
    """Delete rows older than their configured retention. Returns counts."""
    now = _utc_now_naive()
    plan: list[tuple[str, type, int]] = [
        ("whispers", Whisper, settings.retention_whisper_days),
        ("anonymous_messages", AnonymousMessage, settings.retention_anon_message_days),
        ("anon_chat_sessions", AnonChatSession, settings.retention_anon_session_days),
        ("coin_ledger", CoinTransaction, settings.retention_ledger_days),
        ("user_reports", UserReport, settings.retention_report_days),
    ]
    counts: dict[str, int] = {}
    async with async_session_factory() as session:
        for label, model, days in plan:
            if days <= 0:
                counts[label] = 0
                continue
            cutoff = now - timedelta(days=days)
            result = await session.execute(
                delete(model).where(model.created_at < cutoff)
            )
            counts[label] = int(result.rowcount or 0)
        await session.commit()
    return counts


async def retention_loop() -> None:
    """Sleep, then purge every 24 h until cancelled."""
    await asyncio.sleep(FIRST_SWEEP_DELAY)
    while True:
        try:
            counts = await purge_expired()
            if any(counts.values()):
                logger.info(
                    "Retention sweep deleted: %s",
                    ", ".join(f"{k}={v}" for k, v in counts.items()),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Retention sweep failed — will retry next day.")
        await asyncio.sleep(SWEEP_INTERVAL)


async def start_retention() -> None:
    """Startup hook: spawn the daily sweep (kept referenced — see bot._keep)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(retention_loop(), name="retention-sweep")
    logger.info(
        "Retention sweep scheduled (first run in %d min).",
        int(FIRST_SWEEP_DELAY // 60),
    )


async def stop_retention() -> None:
    """Shutdown hook: cancel the sweep cleanly."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
