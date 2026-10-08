"""
Backfills the group roster in :mod:`utils.group_members` from Telegram itself.

Why this module exists
----------------------
The registry that the inline whisper picker resolves a recipient against is fed
incrementally by ``chat_member`` updates and by ordinary group messages. Both
start from nothing on a cold process:

* a ``chat_member`` update only reaches a bot that is an **administrator**, and
  only for changes that happen while it is running — everyone who joined before
  the process started is simply missing;
* a group message only records **its own author**, so a group where nobody has
  typed since the last restart contributes nothing at all.

The symptom is exactly what a user hits in practice: the bot is added to a
group, they type ``🆔 <id> 💬 …`` for somebody who is a member but has never
posted, and the picker answers «پیدا نشد». This module tops the registry up so
that "somebody who is here but has never spoken" stops being invisible.

What the Bot API can and cannot do — read this before changing anything
-----------------------------------------------------------------------
There is **no** ``getChatMembers`` in the Telegram Bot API. It exists only in
MTProto, for userbots. The previous version of this file called
``bot.get_chat_members(...)``, which aiogram does not even define, so every
call raised ``AttributeError`` and the roster stayed permanently empty — the
"recipient not found" report, once per group, every cycle.

The complete set of member listings a bot actually has is:

=========================  ==================================  =========
Method                     Returns                            Needs
=========================  ==================================  =========
``getChatAdministrators``  every admin and the owner           admin
``getChatMember``          one known member                    member
``getChatMemberCount``     a number, no identities             member
=========================  ==================================  =========

So there is no way to enumerate ordinary members, by design — Telegram treats
the member list of a group as private to its members. What *is* obtainable is
the full administrator list, and that is what this module pulls. Combined with
the two incremental feeds (group messages, ``chat_member``) the registry covers
every admin plus everybody who has ever spoken, which is the reachable ceiling
without a userbot.

Division of labour — all three feeds are needed, none is sufficient alone:

* :mod:`middleware.group_member_watch` — keeps a plain member up to date, no
  admin rights and no API budget required.
* ``chat_member`` in :mod:`handlers.group_lifecycle` — authoritative and free,
  but administrator-only.
* this module — the full administrator list, on a cold start and after every
  promotion.

:func:`utils.target_lookup` closes the remaining gap from the other side: a
person who has started the bot is deliverable even if the roster has never
heard of them, so a lookup consults both.

Everything here is best-effort by construction: Telegram refuses the listing for
a bot that lacks admin rights (logged at DEBUG, because that is the normal state
for most groups), and no failure is ever allowed to reach startup or the
dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from utils.group_members import remember_member

logger = logging.getLogger(__name__)


#: Pause between groups. ``getChatAdministrators`` is one call per group, but a
#: sweep of sixty groups at full speed is exactly the shape Telegram answers
#: with flood limits, and flood limits throttle the whole bot.
_CHAT_DELAY = 1.0

#: How many groups one sweep walks. Ordering is by ascending chat id, which is
#: effectively arbitrary — this is a backstop against a pathological fleet, not
#: a guarantee that every group is covered on every pass.
_CHATS_PER_SWEEP_CAP = 60

#: The periodic refresh is a safety net, not the primary feed: ``chat_member``
#: already keeps an administrator's view current, so re-reading the same
#: membership list every few minutes is pure belt-and-braces.
_REFRESH_INTERVAL = 30 * 60.0

#: How long to wait before a failed lookup may trigger another sweep. Without it
#: a user holding the picker open against an unknown id would ask for a full
#: re-scan on every keystroke.
_MISS_SYNC_COOLDOWN = 120.0


async def sync_chat_roster(bot: Bot, chat_id: int) -> int:
    """Pull one group's *administrators* into the registry.

    Returns how many were recorded (0 when Telegram would not list them, which is
    the normal answer for a group the bot is not an administrator in).

    Administrators only — see the module docstring for why that is the ceiling
    the Bot API imposes. They are, however, precisely the members an admin is
    most likely to whisper: they are the people a group is run by.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramAPIError as exc:
        # By far the common case: no admin rights, "bot is not a member", or a
        # stale chat id from a group the bot has since been kicked out of.
        logger.debug("Roster sync: cannot list admins of %s: %s", chat_id, exc)
        return 0
    except Exception as exc:  # never let a sync take down its caller
        logger.warning("Roster sync: listing %s failed: %s", chat_id, exc)
        return 0

    seen = 0
    for member in admins:
        user = member.user
        if user.is_bot:
            continue
        # ``is_anonymous`` admins are attributed to Telegram's
        # ``GroupAnonymousBot`` sentinel, which stands in for a real person and
        # would poison the username index. ``remember_member`` drops that id on
        # its own, so nothing has to be special-cased here.
        remember_member(
            user.id,
            user.first_name or "",
            user.username or "",
            chat_id=chat_id,
        )
        seen += 1

    return seen


async def sync_known_rosters(bot: Bot) -> tuple[int, int]:
    """Backfill every group the bot is known to be in.

    Returns ``(groups_walked, members_recorded)``.

    The chat ids come from :func:`utils.group_cleanup.known_chat_ids` — the same
    union of whisper rows, the ``group_chats`` registry and the force-join target
    that the keyboard sweep uses. No ``get_chat`` pre-check is done: for a
    non-group id (a channel, say) the listing simply fails and is swallowed,
    which costs nothing but saves an API call per group.
    """
    # Imported here rather than at module scope: utils.group_cleanup pulls in
    # middleware.keyboard_guard, and middleware/__init__ imports
    # group_keyboard_watch, which imports utils.group_cleanup right back. That
    # cycle is harmless as long as middleware is imported first (which bot.py
    # does), but a module-level import here would make "import utils.roster_sync
    # first" blow up for no reason.
    from utils.group_cleanup import known_chat_ids

    try:
        chat_ids = sorted(await known_chat_ids())
    except Exception as exc:
        logger.warning("Roster sync: could not read chat ids: %s", exc)
        return 0, 0

    groups = 0
    members = 0

    for chat_id in chat_ids[:_CHATS_PER_SWEEP_CAP]:
        members += await sync_chat_roster(bot, chat_id)
        groups += 1
        await asyncio.sleep(_CHAT_DELAY)

    return groups, members


async def roster_sync_loop(bot: Bot) -> None:
    """Refresh the rosters on a lazy cadence, forever.

    Registered as a background task at startup so the registry is populated
    before anyone types a whisper, and re-checked periodically so a group the
    bot was promoted into later is picked up without a restart.
    """
    while True:
        try:
            groups, members = await sync_known_rosters(bot)
            logger.info(
                "Roster sync: recorded %d administrator(s) across %d group(s).",
                members,
                groups,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Roster refresh failed: %s", exc)
        await asyncio.sleep(_REFRESH_INTERVAL)


#: Strong reference to the refresh task. ``asyncio`` does not keep references to
#: tasks created by application code, and a task whose last reference is dropped
#: can be garbage-collected mid-flight — which would silently end the refresh.
_refresh_task: asyncio.Task | None = None

#: Strong references to the one-shot per-group syncs, same reason as above.
_pending: set[asyncio.Task] = set()

#: ``time.monotonic()`` of the last sweep a failed lookup asked for.
_miss_sync_at = 0.0


def _spawn(coro) -> bool:
    """Run ``coro`` in the background, keeping the task referenced.

    Returns False when there is no running loop (sync test code), in which case
    the coroutine is closed rather than left un-awaited.
    """
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        logger.debug("Roster sync: no event loop, skipping background sync.")
        return False

    _pending.add(task)
    task.add_done_callback(_pending.discard)
    task.add_done_callback(_log_failure)
    return True


def _log_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Roster sync task failed: %s", exc)


async def start_roster_sync(bot: Bot) -> None:
    """Startup hook: begin refreshing the rosters in the background.

    Must be ``async def``: aiogram runs a *synchronous* ``dp.startup`` callback
    through ``asyncio.to_thread``, and a worker thread has no running event
    loop, so ``create_task`` there raises ``RuntimeError`` and takes the whole
    startup down.

    Deliberately does not await the first pass — a large fleet would otherwise
    hold up ``start_polling``, and the loop logs its own progress anyway.
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(roster_sync_loop(bot))


async def stop_roster_sync() -> None:
    """Shutdown hook: stop the refresh loop."""
    global _refresh_task

    task, _refresh_task = _refresh_task, None
    if task is None:
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def spawn_chat_roster_sync(bot: Bot, chat_id: int) -> None:
    """Fire-and-forget administrator pull for a single group.

    Called the moment the bot is promoted to administrator: that transition is
    precisely what makes the listing work, so it is the one moment where a
    roster is guaranteed to be both possible and useful.
    """
    if not _spawn(sync_chat_roster(bot, chat_id)):
        return
    logger.debug("Roster sync: scheduled a listing of %s", chat_id)


def spawn_miss_sync(bot: Bot) -> None:
    """Re-scan after a lookup failed, at most once per cooldown.

    This is the self-healing path: if the bot gained admin rights (or was added
    to a new group) since the last sweep, the very next query resolves instead of
    reporting the recipient as unknown.
    """
    global _miss_sync_at

    now = time.monotonic()
    if now - _miss_sync_at < _MISS_SYNC_COOLDOWN:
        return

    _miss_sync_at = now
    _spawn(sync_known_rosters(bot))


__all__ = [
    "roster_sync_loop",
    "spawn_chat_roster_sync",
    "spawn_miss_sync",
    "start_roster_sync",
    "stop_roster_sync",
    "sync_chat_roster",
    "sync_known_rosters",
]