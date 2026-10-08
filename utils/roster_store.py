"""
Durable half of the group roster.

Why this exists
---------------
:mod:`utils.group_members` answers "have we seen this person in a group" from
memory: free, instant, and gone the moment the process exits. That ceiling is
not a performance choice, it is the Bot API's. There is no listing for ordinary
group members, so the only evidence a bot can ever hold about an ordinary member
is *traffic it has already seen* — every message a group sends proves its author
is inside that group.

So the roster is filled passively by ``middleware.group_member_watch`` and kept
here, and the in-memory index stays a cache over this table rather than the only
copy. Without persistence, a member who had to be resolvable before the bot
restarted simply stopped being resolvable.

Why nothing awaits on the message path
--------------------------------------
The naive version of "cache the DB" is a write per message, and that is exactly
the wrong trade here: this middleware is an *outer* middleware, so its cost is
paid by every update the bot sees, including ones that have nothing to do with
whispers. A database round trip in it would put a write amplification problem in
front of the whole bot.

So :func:`note_member` is a plain set insert — no ``await``, no session, no I/O —
and a single background task drains it in batches. The worst case is losing the
last few seconds of roster on a crash, which costs nothing: the entries are
re-derived from the next message that person sends.

Why this is a separate table and not ``users``
----------------------------------------------
``users`` means "has a private conversation with this bot" and carries the
economy, ban and referral state along with it. ``has_started_bot`` is defined as
*a row exists in users*, so writing group chatter there would make it answer yes
for people who never pressed /start — silently dropping the "recipient has not
started the bot" caveat and misrouting the private-message fallback. See
:class:`database.models.GroupRoster`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, insert, select, update

from database import GroupRoster, async_session_factory

logger = logging.getLogger(__name__)

#: ``(user_id, chat_id)`` → ``(username, first_name)``. The newest data wins, so
#: a rename between two messages does not get overwritten by a staler copy.
_dirty: dict[tuple[int, int], tuple[str, str]] = {}

#: Never hold more than this many identities waiting. A pathological group (or a
#: flood) would otherwise grow the dict without bound while the flusher is down;
#: dropping the excess is safe because every entry is re-derivable from the next
#: message, whereas unbounded growth is an outage.
_DIRTY_MAX = 20_000

#: How often the flusher drains. Short enough that a restart loses almost
#: nothing, long enough that an idle bot does no work at all.
_FLUSH_INTERVAL = 20.0

#: Rows older than this are pruned on the same pass. The table is a cache of who
#: spoke recently, not an archive, and a group that changes shape over months
#: would otherwise accumulate every person who ever typed.
_RETENTION = 90 * 24 * 3600.0

#: Prune at most this often, regardless of flush count — the sweep touches every
#: row it is willing to delete, so it is the expensive part.
_PRUNE_INTERVAL = 3600.0


def note_member(
    user_id: int,
    chat_id: int,
    username: str,
    first_name: str,
) -> None:
    """Queue one membership for the next flush. Synchronous and never raises.

    Called from the outer middleware, so it must not await, allocate a session,
    or throw. Everything expensive happens later in :func:`flush_dirty`.
    """
    if not user_id or not chat_id:
        return
    if len(_dirty) >= _DIRTY_MAX:
        # Drop the oldest insertion rather than refusing new data: a member who
        # just spoke is the one most likely to be whispered to next.
        try:
            del _dirty[next(iter(_dirty))]
        except (StopIteration, KeyError, RuntimeError):
            pass
    _dirty[(user_id, chat_id)] = ((username or "").strip().lstrip("@")[:64],
                                  (first_name or "").strip()[:64])


def pending_count() -> int:
    """How many identities are waiting to be written. For logs and tests."""
    return len(_dirty)


async def flush_dirty() -> int:
    """Write every queued membership in one transaction. Returns rows written.

    Update-then-insert rather than ``session.merge()``: merge copies *every*
    attribute of the transient instance onto the loaded row, so an update that
    knows no username would silently NULL out a good one stored earlier. Going
    through an explicit ``UPDATE`` keeps the "never blank a name we already know"
    rule, and only falls back to ``INSERT`` for a pair not seen before.
    """
    if not _dirty:
        return 0

    batch = list(_dirty.items())
    _dirty.clear()

    try:
        async with async_session_factory() as session:
            for (user_id, chat_id), (username, first_name) in batch:
                values: dict = {"last_seen_at": datetime.utcnow()}
                if username:
                    values["username"] = username
                if first_name:
                    values["first_name"] = first_name

                result = await session.execute(
                    update(GroupRoster)
                    .where(
                        GroupRoster.user_id == user_id,
                        GroupRoster.chat_id == chat_id,
                    )
                    .values(**values)
                )
                if not result.rowcount:
                    # A pair we have never stored: this one wants every column,
                    # so it is built separately rather than by patching ``values``
                    # (which deliberately omits the names we do not know).
                    await session.execute(
                        insert(GroupRoster).values(
                            user_id=user_id,
                            chat_id=chat_id,
                            username=username or None,
                            first_name=first_name or None,
                            last_seen_at=values["last_seen_at"],
                        )
                    )
            await session.commit()
    except Exception as exc:
        # Put the batch back so it is retried rather than lost. Bounded by
        # _DIRTY_MAX, so a database that is down cannot exhaust memory.
        for key, value in batch:
            if len(_dirty) < _DIRTY_MAX:
                _dirty.setdefault(key, value)
        logger.warning(
            "Roster flush failed, %d entr(ies) kept for retry: %s", len(batch), exc
        )
        return 0

    logger.debug("Roster flush: wrote %d membership(s).", len(batch))
    return len(batch)


async def prune_old(max_age: float = _RETENTION) -> int:
    """Drop memberships nobody has refreshed in ``max_age`` seconds.

    Returns how many rows went. Best effort: a failure here is a slowly growing
    table, not a broken feature, so it is logged and forgotten.

    Naive UTC to match how every other timestamp in this project is stored (see
    ``utils.economy``); comparing against an aware datetime would not match the
    stored strings under SQLite.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                delete(GroupRoster).where(GroupRoster.last_seen_at < cutoff)
            )
            await session.commit()
            return result.rowcount or 0
    except Exception as exc:
        logger.debug("Roster prune skipped: %s", exc)
        return 0


_last_prune = 0.0


async def persistence_loop() -> None:
    """Drain the queue forever. Registered on ``dp.startup``.

    An idle bot costs one clock read per interval and no queries at all: the
    flush returns immediately when nothing is queued, and the prune is rate
    limited by its own clock.
    """
    global _last_prune

    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)

            written = await flush_dirty()

            now = time.monotonic()
            if now - _last_prune >= _PRUNE_INTERVAL:
                _last_prune = now
                dropped = await prune_old()
                if dropped:
                    logger.info("Roster prune: dropped %d stale membership(s).",
                                dropped)
            if written:
                logger.debug("Roster persist: %d membership(s) written.", written)
        except asyncio.CancelledError:
            # Flush what we are holding on the way out; a clean shutdown should
            # not cost the roster its last few seconds.
            try:
                await flush_dirty()
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.warning("Roster persistence loop error: %s", exc)
            await asyncio.sleep(_FLUSH_INTERVAL)


async def lookup(token: str, *, user_id: int | None = None) -> dict | None:
    """Find one roster row by ``@username`` or numeric id. ``None`` if unknown.

    Read by the resolver as a *fallback*, never as the first stop: the in-memory
    index is free and this costs a query. A miss is silence, not a negative — the
    table only holds people whose traffic we happened to see.
    """
    raw = (token or "").strip()
    if not raw:
        return None

    try:
        async with async_session_factory() as session:
            if user_id is not None:
                result = await session.execute(
                    select(GroupRoster)
                    .where(GroupRoster.user_id == user_id)
                    .order_by(GroupRoster.last_seen_at.desc())
                    .limit(1)
                )
            elif raw.startswith("@") and len(raw) > 1:
                result = await session.execute(
                    select(GroupRoster)
                    .where(GroupRoster.username == raw[1:].lower())
                    .order_by(GroupRoster.last_seen_at.desc())
                    .limit(1)
                )
            elif raw.lstrip("-").isdigit():
                result = await session.execute(
                    select(GroupRoster)
                    .where(GroupRoster.user_id == int(raw))
                    .order_by(GroupRoster.last_seen_at.desc())
                    .limit(1)
                )
            else:
                return None

            row = result.scalar_one_or_none()
            if row is None:
                return None
            return {
                "user_id": row.user_id,
                "username": row.username or "",
                "first_name": row.first_name or "",
            }
    except Exception as exc:
        # Advisory source: a lookup failure must never block a send.
        logger.debug("Roster lookup(%s) failed: %s", raw, exc)
        return None


async def is_member_of(chat_id: int, user_id: int) -> bool:
    """True when the durable roster has seen this person in *this* group.

    The persistent counterpart of ``utils.group_members.is_member_of_chat``: it
    survives a restart, which is exactly when the in-memory answer is empty and a
    whisper to an ordinary member would otherwise be refused.
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(GroupRoster.user_id)
                .where(
                    GroupRoster.user_id == user_id,
                    GroupRoster.chat_id == chat_id,
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None
    except Exception as exc:
        logger.debug("Roster membership check failed: %s", exc)
        return False


#: Strong reference to the flush task. ``asyncio`` does not keep references to
#: tasks created by application code, and a task whose last reference is dropped
#: can be garbage-collected mid-flight — which would silently stop persistence.
_task: asyncio.Task | None = None


async def start_persistence() -> None:
    """Startup hook: begin draining the roster queue in the background.

    Must be ``async def``: aiogram runs a *synchronous* ``dp.startup`` callback
    through ``asyncio.to_thread``, and a worker thread has no running event loop
    to spawn onto.
    """
    global _task

    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(persistence_loop())
    logger.debug("Roster persistence started.")


async def stop_persistence() -> None:
    """Shutdown hook: stop the loop, flushing whatever it is holding."""
    global _task

    task, _task = _task, None
    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # the loop's own handler already flushed
    except Exception as exc:
        logger.warning("Roster persistence shutdown: %s", exc)

    # Belt and braces: the loop flushes on cancellation, but if it died earlier
    # from an error this is the last chance to keep the queue.
    await flush_dirty()


__all__ = [
    "flush_dirty",
    "is_member_of",
    "lookup",
    "note_member",
    "pending_count",
    "persistence_loop",
    "prune_old",
    "start_persistence",
    "stop_persistence",
]