"""
Defence in depth for the admin panel's inline callbacks.

``handlers/admin.py`` already refuses non-admins at the router level (the
``IsAdmin()`` filter on the router itself, plus ``IsRootAdmin()`` on the
root-only entries). This middleware repeats that check one layer earlier for
every ``admin:*`` callback tap, so a future handler added to the panel — or a
filter that gets edited away — still cannot run for a regular user.

Behaviour on refusal: the tap is answered with a short alert and swallowed.
Nothing is forwarded to the dispatcher, so no handler of the ``admin:``
family is ever entered, and the user sees a refusal instead of a dead
button (the "loading…" spinner Telegram shows when a callback is ignored).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from filters import IsAdmin

logger = logging.getLogger(__name__)

#: Callback data prefix reserved for the admin panel.
ADMIN_PREFIX = "admin:"

#: Shown to anyone who taps an admin button without being one.
_DENIED = "⛔ دسترسی غیرمجاز"


class AdminPanelGuardMiddleware(BaseMiddleware):
    """Refuse ``admin:*`` callbacks from users who are not admins."""

    _is_admin = IsAdmin()

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        payload = event.data or ""
        if not payload.startswith(ADMIN_PREFIX):
            return await handler(event, data)

        if await self._is_admin(event):
            return await handler(event, data)

        user = event.from_user
        logger.warning(
            "Blocked admin callback %r from non-admin user %s",
            payload,
            user.id if user else "?",
        )
        try:
            await event.answer(_DENIED, show_alert=True)
        except Exception:  # pragma: no cover - the refusal itself must not raise
            pass
        return None


__all__ = ["AdminPanelGuardMiddleware", "ADMIN_PREFIX"]
