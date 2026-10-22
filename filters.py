"""
Custom aiogram filters for routing logic.
"""

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from config import settings
from utils.user_flags import load_profile_complete, load_user_flags


class IsAdmin(BaseFilter):
    """
    Return ``True`` when the user is:
      1. a Root (Super) Admin listed in ``settings.admin_ids``, OR
      2. an admin promoted dynamically in the database (``User.is_admin``).

    The panel is therefore completely invisible to normal users.

    Answered through :func:`utils.user_flags.load_user_flags` so the admin
    flag is fetched and cached together with the banned flag the middlewares
    already ask for — one query instead of a separate ``SELECT`` per check.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        user = event.from_user
        if not user:
            return False
        # Root Admins short-circuit inside load_user_flags (no DB round trip).
        return (await load_user_flags(user.id))[0]


class IsRootAdmin(BaseFilter):
    """
    Return ``True`` ONLY for Root (Super) Admins from ``settings.admin_ids``.

    Used to gate root-only capabilities such as promoting/demoting admins.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        user = event.from_user
        return bool(user and int(user.id) in settings.admin_ids_list)


class IsBanned(BaseFilter):
    """Check if the user is banned.

    Answered through :func:`utils.user_flags.load_user_flags` so a filter that
    runs on an update where the admin/ban check already loaded the flags pays
    ZERO extra round trips (and one shared query otherwise) — instead of its
    own session + full-row ``SELECT`` per update.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        user = event.from_user
        if not user:
            return False
        return bool((await load_user_flags(user.id))[1])


class IsProfileComplete(BaseFilter):
    """Check if the user has completed their profile in the database.

    Answered through :func:`utils.user_flags.load_profile_complete` — the
    third column of the same briefly-cached flags row — so the wizard gate
    costs no separate session/`SELECT` per update.
    """

    async def __call__(self, event: TelegramObject, **kwargs: Any) -> bool:
        if not event.from_user:
            return False
        return await load_profile_complete(event.from_user.id)
