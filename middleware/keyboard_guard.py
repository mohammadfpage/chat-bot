"""
Strict chat-type enforcer: a bottom reply keyboard may only ever reach a
1-on-1 private chat.

Why a session-level interceptor and not just an outer middleware
--------------------------------------------------------------
A ``BaseMiddleware`` wraps *handler invocation*. It can filter an update
before the handler runs, but it cannot reach inside a handler to rewrite the
keyword arguments it passes to ``message.answer(...)`` / ``message.reply(...)``
/ ``callback.message.edit_text(...)`` — those calls go straight to
``Bot.__call__`` → ``bot.session``. A filter-only guard would therefore be
trivially bypassed by a single missing ``F.chat.type == PRIVATE``, which is
exactly the accident that leaves a keyboard stuck at the bottom of a group.

So the enforcement point is the last hop before the wire:
:meth:`GuardedSession.__call__` inspects every outgoing method, and any
``ReplyKeyboardMarkup`` / ``ForceReply`` aimed at a group is rewritten to
``ReplyKeyboardRemove`` before it is serialised. That makes the rule a
property of the process rather than a convention every handler has to remember:
a new group handler written tomorrow is covered by default, and no existing
handler needs editing.

Chat-type resolution (no extra API call in the hot path)
--------------------------------------------------------
Stripping needs to know whether the *target* chat is a group. Naively that
means a ``get_chat`` per outgoing message. Instead:

1. :class:`ReplyKeyboardGuardMiddleware` publishes the incoming event's
   ``(chat_id, chat.type)`` into a :class:`~contextvars.ContextVar` for the
   duration of the handler.
2. The guard trusts that hint **only** when the outgoing ``chat_id`` matches
   the event's chat id — the common case, and it costs nothing. This
   check is what makes the hint safe: a group handler that DMs a user with a
   full ``main_menu_kb()`` targets a *different* chat, so the hint is
   discarded and the real type is looked up instead.
3. Anything else (background task, cross-chat send, hint mismatch) is resolved
   via ``get_chat`` and memoised in a bounded cache.

Stripping is unconditional and silent about it: the message still goes out, it
just carries the removal payload, and a ``WARNING`` names the offending method
so the handler that caused it can be found and fixed properly.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from utils.chat_types import GROUP_CHAT_TYPES, is_group

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# Policy
# ──────────────────────────────────────────────────

#: Payloads that occupy the bottom of the input area. ``ForceReply`` is in the
#: list on purpose: it is a different markup but produces the same stuck UI
#: (a forced "reply to @bot" bar), so a group gets the same treatment.
_BANNED_MARKUPS: tuple[type, ...] = (ReplyKeyboardMarkup, ForceReply)

#: ``selective=False`` is the entire point: with ``True`` Telegram hides the
#: keyboard only for users the message targets (mentions, or the replied-to
#: author) — in a group that is "nearly nobody". ``False`` clears it for every
#: member. Spelled out rather than left to the Bot API default on purpose.
_CLEAR_KEYBOARD = ReplyKeyboardRemove(remove_keyboard=True, selective=False)

#: Bounded memo for :meth:`GuardedSession._resolve_chat_type`. A basic group can
#: never become a supergroup or vice versa, so entries are effectively
#: permanent; the cap only stops an unbounded-id process from growing forever.
_CHAT_TYPE_CACHE_MAX = 4096

#: ``(chat_id, chat_type)`` of the update being handled, or ``None``.
_ambient_chat: ContextVar[tuple[int, ChatType] | None] = ContextVar(
    "keyboard_guard_ambient_chat", default=None
)


def clear_keyboard_markup() -> ReplyKeyboardRemove:
    """The single removal payload used everywhere keyboards get stripped."""
    return _CLEAR_KEYBOARD


def sanitize_reply_markup(
    chat_type: ChatType | str | None,
    reply_markup: Any,
) -> Any:
    """Return markup that is legal for ``chat_type``.

    Passes ``None`` and ``InlineKeyboardMarkup`` through untouched — inline
    keyboards are the *only* markup allowed in groups, so they are the last
    thing the guard would ever rewrite. A banned payload in a private chat is
    also returned as-is: private chats are the one place it belongs.
    """
    if reply_markup is None:
        return None
    if isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup
    if isinstance(reply_markup, _BANNED_MARKUPS) and is_group(chat_type):
        return _CLEAR_KEYBOARD
    return reply_markup


# ──────────────────────────────────────────────────
# The interceptor
# ──────────────────────────────────────────────────


class GuardedSession:
    """Transparent proxy around a :class:`BaseSession` that scrubs payloads.

    Every attribute other than ``__call__`` is delegated to the wrapped
    session, so ``bot.session.close()`` (used in ``bot.py``'s ``finally``),
    ``bot.session.middleware`` and ``stream_content`` all keep working.
    """

    def __init__(self, inner: BaseSession) -> None:
        self._inner = inner
        self._chat_types: dict[int, ChatType] = {}
        self.stripped = 0

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found on this class, so ``_inner`` itself
        # can never recurse through here.
        return getattr(self._inner, name)

    async def __call__(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        method = await self._scrub(bot, method)
        return await self._inner(bot, method, timeout=timeout)

    # ── internals ──

    async def _scrub(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> TelegramMethod[Any]:
        """Rewrite a banned markup in a group, or return ``method`` untouched."""
        if not isinstance(getattr(method, "reply_markup", None), _BANNED_MARKUPS):
            return method

        chat_id = getattr(method, "chat_id", None)
        # ``chat_id`` is None when editing an inline-sent message, which lives
        # in the sender's private chat — nothing to strip, and no id to resolve.
        if not isinstance(chat_id, int):
            return method

        chat_type = await self._resolve_chat_type(bot, chat_id)
        if not is_group(chat_type):
            return method

        self.stripped += 1
        logger.warning(
            "Keyboard guard: stripped %s from %s in %s (%s). "
            "Use an InlineKeyboardMarkup in groups.",
            type(getattr(method, "reply_markup")).__name__,
            getattr(method, "__api_method__", type(method).__name__),
            chat_id,
            chat_type,
        )
        return method.model_copy(update={"reply_markup": _CLEAR_KEYBOARD})

    async def _resolve_chat_type(
        self,
        bot: Bot,
        chat_id: int,
    ) -> ChatType | None:
        """Best-effort chat type for ``chat_id``; ``None`` means "unknown".

        Unknown is treated as *not a group*, i.e. the guard fails open. That is
        deliberate: a ``get_chat`` failure (bot kicked, id stale, network blip)
        must not silently strip a legitimate private-chat keyboard, which would
        be a worse bug than the one being fixed.
        """
        ambient = _ambient_chat.get()
        if ambient is not None and ambient[0] == chat_id:
            return ambient[1]

        cached = self._chat_types.get(chat_id)
        if cached is not None:
            return cached

        try:
            chat = await bot.get_chat(chat_id)
            # The conversion lives inside the try on purpose: an unrecognised
            # type must degrade to "unknown", never bubble up and break the
            # send that was only being inspected.
            chat_type = ChatType(chat.type)
        except Exception as exc:
            logger.debug("Keyboard guard: get_chat(%s) failed: %s", chat_id, exc)
            return None

        if len(self._chat_types) >= _CHAT_TYPE_CACHE_MAX:
            self._chat_types.clear()
        self._chat_types[chat_id] = chat_type
        return chat_type


def install_keyboard_guard(bot: Bot) -> GuardedSession:
    """Wrap ``bot.session`` so group reply keyboards can never leave the process.

    Idempotent: calling it twice would otherwise stack two proxies, and the
    outer one would mask the inner one's counters.
    """
    existing = getattr(bot, "session", None)
    if isinstance(existing, GuardedSession):
        return existing

    guard = GuardedSession(existing)
    bot.session = guard
    return guard


def get_guarded_session(bot: Bot) -> GuardedSession | None:
    """The installed guard for ``bot``, or ``None`` if it is not installed."""
    session = getattr(bot, "session", None)
    return session if isinstance(session, GuardedSession) else None


# ──────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────


class ReplyKeyboardGuardMiddleware(BaseMiddleware):
    """Publishes the incoming chat's type so the guard needs no API call.

    Registered **first** on both ``dp.message`` and ``dp.callback_query`` so it
    wraps every other outer middleware. That ordering is what lets a message
    emitted by a later middleware (``ForceJoinMiddleware`` bouncing a user into
    a DM) be checked without a ``get_chat`` round-trip.

    The middleware itself rewrites nothing — it only supplies the cheap hint
    that lets :class:`GuardedSession` skip its ``get_chat``. All stripping
    happens downstream at the session, which is the only layer that can see the
    ``reply_markup`` a handler actually passed.
    """

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        # ``Message.chat`` is the chat; ``CallbackQuery.message`` is a *Message*
        # (the one the button lives on), so its chat is one hop further down.
        # Reading ``event.message.type`` raised
        # ``AttributeError: 'Message' object has no attribute 'type'`` on every
        # single callback query, which aborted the whole update before the
        # handler ran — so every whisper read button was dead.
        if isinstance(event, Message):
            chat: Chat | None = event.chat
        else:
            # ``event.message`` may itself be None (a callback on an
            # inline-sent message), so the attribute hop cannot be chained.
            origin = event.message
            chat = origin.chat if origin is not None else None

        if chat is None:
            # Callback on an inline-sent message: no chat, no way to be a
            # group, and nothing for the guard to resolve.
            return await handler(event, data)

        chat_type = ChatType(chat.type)
        if not is_group(chat_type):
            return await handler(event, data)

        token = _ambient_chat.set((chat.id, chat_type))
        try:
            return await handler(event, data)
        finally:
            _ambient_chat.reset(token)


__all__ = [
    "GROUP_CHAT_TYPES",
    "GuardedSession",
    "ReplyKeyboardGuardMiddleware",
    "clear_keyboard_markup",
    "get_guarded_session",
    "install_keyboard_guard",
    "sanitize_reply_markup",
]
