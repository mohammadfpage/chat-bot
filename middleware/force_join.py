"""
Middlewares: banned-user blocker and force-join enforcement.

Both are outer middlewares applied to messages and callbacks.
Channel settings are read directly from .env via config.py — no database
lookups needed for force-join.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import settings
from database import async_session_factory, User
from keyboards.inline import force_join_kb

# Commands that always bypass the forced-join check
_BYPASS_COMMANDS = {"start"}


async def _is_admin(user_id: int) -> bool:
    """Cheap admin check for middleware (root OR db flag)."""
    if user_id in settings.admin_ids_list:
        return True
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        db_user = result.scalar_one_or_none()
        return bool(db_user and db_user.is_admin)


async def _is_banned(user_id: int) -> bool:
    """Return True if the user is banned in the database."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        db_user = result.scalar_one_or_none()
        return bool(db_user and db_user.is_banned)


class BlockBannedMiddleware(BaseMiddleware):
    """
    Instantly blocks any banned user at the middleware level.

    Banned users cannot trigger ANY handler (messages or callbacks) — they
    only receive a short notification once. This guarantees that a ban takes
    effect immediately, even mid-conversation.
    """

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return await handler(event, data)

        # Admins can never be blocked this way.
        if await _is_admin(user.id):
            return await handler(event, data)

        if not await _is_banned(user.id):
            return await handler(event, data)

        # Banned → silently swallow the event.
        return None


class ForceJoinMiddleware(BaseMiddleware):
    """Outer middleware that enforces channel membership on every update."""

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return await handler(event, data)

        # All admins bypass the forced-join check.
        if await _is_admin(user.id):
            return await handler(event, data)

        # Read channel settings directly from .env config.
        channel_username = settings.channel_username.strip().lstrip("@")
        channel_id = settings.channel_id
        if not channel_username and not channel_id:
            return await handler(event, data)

        # Allow the membership-check callback itself through.
        if isinstance(event, CallbackQuery) and event.data == "check_membership":
            return await handler(event, data)

        # Bypass for /start
        if isinstance(event, Message) and event.text and event.text.startswith("/"):
            cmd = event.text.split()[0].lstrip("/")
            if cmd in _BYPASS_COMMANDS:
                return await handler(event, data)

        bot = data.get("bot")
        if not bot:
            return await handler(event, data)

        # Prefer the numeric channel id; fall back to resolving by username.
        chat_ref: str | int = channel_id if channel_id else f"@{channel_username}"
        try:
            member = await bot.get_chat_member(
                chat_id=chat_ref,
                user_id=user.id,
            )
            is_member = member.status not in ("left", "kicked")
        except Exception:
            is_member = False

        if is_member:
            return await handler(event, data)

        # Not a member → prompt to join
        if isinstance(event, Message):
            await event.answer(
                "⚠️ برای استفاده از ربات ابتدا باید در کانال ما عضو شوید.",
                reply_markup=force_join_kb(channel_username if channel_username else str(channel_id)),
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("⚠️ ابتدا در کانال عضو شوید.", show_alert=True)

        return None


__all__ = [
    "BlockBannedMiddleware",
    "ForceJoinMiddleware",
]
