"""
Custom aiogram filters for routing logic.
"""

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject
from sqlalchemy import select

from config import settings
from database import async_session_factory, User


class IsAdmin(BaseFilter):
    """
    Return ``True`` when the user is:
      1. a Root (Super) Admin listed in ``settings.admin_ids``, OR
      2. an admin promoted dynamically in the database (``User.is_admin``).

    The panel is therefore completely invisible to normal users.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        user = event.from_user
        if not user:
            return False

        # Root Admins (from .env) — ultimate power, always trusted.
        # Explicit int() cast prevents str-vs-int mismatch from env parsing.
        if int(user.id) in settings.admin_ids_list:
            return True

        # Dynamically promoted admins.
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == user.id)
            )
            db_user = result.scalar_one_or_none()
            return bool(db_user and db_user.is_admin)


class IsRootAdmin(BaseFilter):
    """
    Return ``True`` ONLY for Root (Super) Admins from ``settings.admin_ids``.

    Used to gate root-only capabilities such as promoting/demoting admins.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        user = event.from_user
        return bool(user and int(user.id) in settings.admin_ids_list)


class IsProfileComplete(BaseFilter):
    """Check if the user has completed their profile in the database."""

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        if not event.from_user:
            return False
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == event.from_user.id)
            )
            db_user = result.scalar_one_or_none()
            return bool(db_user and db_user.is_profile_complete)


class IsBanned(BaseFilter):
    """Check if the user is banned."""

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        if not event.from_user:
            return False
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == event.from_user.id)
            )
            db_user = result.scalar_one_or_none()
            return bool(db_user and db_user.is_banned)
