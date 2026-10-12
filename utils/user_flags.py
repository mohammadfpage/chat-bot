"""
Coalesced, briefly-cached ``(is_admin, is_banned)`` lookup.

Two middlewares and one router filter ask "is this user an admin, and is this
user banned?" on EVERY update. Each question used to be its own session and its
own ``SELECT``, so a single button tap cost three or four round trips — which
on a remote database (Neon, ~0.5 s RTT) is the bulk of the "the buttons take
seconds" complaint.

This module answers BOTH questions with ONE query and remembers the pair for a
few seconds, so the middlewares that run one after another reuse the same
answer instead of each paying for it.

Freshness: the TTL is intentionally tiny, and the moderation handlers call
:func:`forget_user_flags` the moment an admin bans, unbans, promotes or demotes
someone, so the actor's next tap already sees the change. What the cache buys
is that an ordinary user's repeated taps in the same few seconds do not each
re-issue the same read.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from config import settings
from database import User, async_session_factory

#: How long a cached pair is trusted. Small on purpose — see the module note.
FLAGS_TTL = 5.0

#: user_id → (cached_at_monotonic, is_admin, is_banned)
_cache: dict[int, tuple[float, bool, bool]] = {}


def forget_user_flags(user_id: int) -> None:
    """Drop one user's cached flags (call right after a flag changes)."""
    _cache.pop(int(user_id), None)


def clear_user_flags_cache() -> None:
    """Drop every cached pair (tests / after a bulk change)."""
    _cache.clear()


async def load_user_flags(user_id: int) -> tuple[bool, bool]:
    """Return ``(is_admin, is_banned)`` for ``user_id``.

    Root admins (``ADMIN_IDS``) are answered from ``.env`` with no query at
    all. For everyone else one query fetches both columns — never the whole
    ``User`` row — and the result is cached for :data:`FLAGS_TTL`.
    """
    user_id = int(user_id)

    # Root admins can never be blocked and never need a database round trip.
    if user_id in settings.admin_ids_list:
        return True, False

    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and (now - cached[0]) < FLAGS_TTL:
        return cached[1], cached[2]

    async with async_session_factory() as session:
        result = await session.execute(
            select(User.is_admin, User.is_banned).where(
                User.telegram_id == user_id
            )
        )
        row = result.one_or_none()

    is_admin, is_banned = (
        (bool(row[0]), bool(row[1])) if row is not None else (False, False)
    )
    _cache[user_id] = (now, is_admin, is_banned)
    return is_admin, is_banned


__all__ = [
    "FLAGS_TTL",
    "clear_user_flags_cache",
    "forget_user_flags",
    "load_user_flags",
]
