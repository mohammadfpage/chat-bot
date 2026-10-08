"""
First-sight watcher: any group the bot hears from gets its cached reply
keyboard cleared once, immediately, without a restart.

Why this exists
---------------
``utils/group_cleanup`` sweeps every group it knows about at startup, but it
can only know groups that are already in the database (whisper rows, the
force-join target, ``group_chats``). A group the bot was *just* added to is
invisible to that sweep — and Telegram caches the last reply keyboard **per
chat** and never flushes it when the bot is removed and re-added. So a
keyboard left behind by an older build would stay stuck at the bottom of that
group for every member until a restart happened *after* the group was first
recorded.

This middleware closes the gap at the earliest possible moment: the first
update that arrives from a group schedules
:func:`clear_group_keyboard_once`, which registers the group and pushes one
``ReplyKeyboardRemove`` into it. The clear itself runs as a fire-and-forget
task so the 2-second client-settling delay inside the cleanup never holds up
the update being handled.

Division of labour (all three pieces are needed):

* ``middleware.keyboard_guard``  — makes it impossible to *send* a reply
  keyboard into a group in the first place (last-hop session guard).
* this module                   — clears a keyboard that is *already* stuck
  in a group the bot has never seen before in this process.
* ``utils.group_cleanup``       — clears known groups again at every startup,
  because the cached keyboard can also arrive from an older build while the
  bot is offline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from utils.chat_types import is_group
from utils.group_cleanup import clear_group_keyboard_once

logger = logging.getLogger(__name__)

#: Strong references to in-flight clear tasks. ``asyncio`` does not keep
#: references to tasks created by application code, and a task with no
#: remaining reference can be garbage-collected mid-flight — which would make
#: the clear silently vanish. The done-callback drops the reference again.
_pending: set[asyncio.Task] = set()


def spawn_group_keyboard_clear(
    bot, chat_id: int, title: str | None = None
) -> None:
    """Schedule a first-sight keyboard clear for ``chat_id``.

    Never raises and never blocks: used from middleware and from the
    ``my_chat_member`` handler, both of which must return control to the
    dispatcher immediately.
    """
    try:
        task = asyncio.create_task(
            clear_group_keyboard_once(bot, chat_id, title)
        )
    except RuntimeError:
        # No running loop (e.g. called from sync test code) — nothing to do.
        logger.debug("Keyboard watch: no event loop, skipping clear of %s", chat_id)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Keyboard watch: clear task failed: %s", exc)


class GroupKeyboardWatchMiddleware(BaseMiddleware):
    """Spawns the one-shot group keyboard clear on the first group update.

    Runs after :class:`ReplyKeyboardGuardMiddleware` (registration order in
    ``bot.py``) so the guard stays the outermost wrapper; it only observes,
    never rewrites, and its own work is delegated to a background task.
    """

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat = event.chat
        else:
            # ``event.message`` is None for callbacks on inline-sent cards;
            # InaccessibleMessage still exposes ``chat``.
            message = event.message
            chat = message.chat if message is not None else None

        if chat is not None and is_group(chat.type):
            bot = data.get("bot") or getattr(event, "bot", None)
            if bot is not None:
                spawn_group_keyboard_clear(bot, chat.id, chat.title)

        return await handler(event, data)


__all__ = [
    "GroupKeyboardWatchMiddleware",
    "spawn_group_keyboard_clear",
]
