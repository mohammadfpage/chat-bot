"""
Chat-type predicates shared by the keyboard guard and the startup cleanup.

One place decides what counts as a "group", so the two halves of the
keyboard policy can never drift apart and disagree about the same chat.
"""

from __future__ import annotations

from aiogram.enums import ChatType
from aiogram.types import Chat

#: Chats where a bottom-reply keyboard must never be used. ``CHANNEL`` is
#: deliberately absent — Telegram refuses reply keyboards there outright, so
#: there is no UI to protect and no reason to strip a developer's payload.
GROUP_CHAT_TYPES: frozenset[ChatType] = frozenset(
    {ChatType.GROUP, ChatType.SUPERGROUP}
)


def is_group(chat_type: ChatType | str | None) -> bool:
    """True for groups and supergroups.

    Accepts the enum, its raw string value (``"supergroup"``) or ``None`` so
    callers never have to normalise first. An unknown/absent type is treated as
    "not a group" — callers pass this straight into payload rewriting, where
    failing open is the only safe direction.
    """
    if chat_type is None:
        return False
    try:
        return ChatType(chat_type) in GROUP_CHAT_TYPES
    except ValueError:
        return False


def is_group_chat(chat: Chat | None) -> bool:
    """True when ``chat`` is a group or supergroup."""
    return bool(chat) and is_group(chat.type)


__all__ = [
    "GROUP_CHAT_TYPES",
    "is_group",
    "is_group_chat",
]
