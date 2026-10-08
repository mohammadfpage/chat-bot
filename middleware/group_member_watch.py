"""
Feeds the group-member registry from ordinary group traffic.

A ``chat_member`` update only reaches a bot that is an **administrator**, and
even then only for groups somebody bothered to promote it in. Every message a
group sends, on the other hand, proves its author is inside that group — so
this middleware turns free traffic into free roster entries, and gives the
inline picker something to resolve an ``@username`` or a numeric id against
even in groups where the bot is a plain member.

Deliberately a no-await dict write on the hot path: the whole point of the
registry is that resolving a target must not cost a database round-trip, and a
middleware that awaited anything would put that back on the critical path.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from utils.chat_types import is_group
from utils.group_members import remember_member

logger = logging.getLogger(__name__)


def remember_sender(message: Message) -> None:
    """Record ``message.from_user`` as a member of ``message.chat``.

    Returns immediately for private chats, for anonymous admins (Telegram's own
    sentinel account, which says nothing about a real person) and for bots.
    """
    chat = message.chat
    if chat is None or not is_group(chat.type):
        return
    user = message.from_user
    if user is None or user.is_bot:
        return
    remember_member(user.id, user.first_name or "", user.username or "", chat_id=chat.id)


class GroupMemberWatchMiddleware(BaseMiddleware):
    """Records the sender of every group message in the member registry."""

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        message = event if isinstance(event, Message) else event.message
        if message is not None:
            try:
                remember_sender(message)
            except Exception as exc:  # never break handling over a roster write
                logger.debug("Could not remember group sender: %s", exc)
        return await handler(event, data)


__all__ = ["GroupMemberWatchMiddleware", "remember_sender"]
