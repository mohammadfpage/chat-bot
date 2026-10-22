"""
Coalesced, briefly-cached ``(is_admin, is_banned, is_profile_complete)`` lookup.

Two middlewares AND three router filters ask questions about the ONE user of
an update on EVERY update: "is this user an admin?", "is this user banned?",
"is the profile complete?". Each question used to be its own session and its
own ``SELECT``, so a single button tap cost three or four round trips — which
on a remote database (Neon, ~0.5 s RTT) is the bulk of the "the buttons take
seconds" complaint.

This module answers ALL THREE questions with ONE query and remembers the triple
for a few seconds, so any filter/middleware that runs later in the same update
reuses the same answer instead of paying for it again.

Freshness: the TTL is intentionally tiny, and the moderation handlers call
:func:`forget_user_flags` the moment an admin bans, unbans, promotes or demotes
someone, so the actor's next tap already sees the change. Same for the profile
wizard: the handler that writes ``is_profile_complete = 1`` drops the cache too,
so the very next update sees the completed profile. What the cache buys is that
repeated taps in the same few seconds do not each re-issue the same read.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from config import settings
from database import User, async_session_factory

#: How long a cached pair is trusted. Small on purpose — see the module note.
FLAGS_TTL = 5.0

#: user_id → (cached_at_monotonic, is_admin, is_banned, is_profile_complete)
_cache: dict[int, tuple[float, bool, bool, bool]] = {}


def forget_user_flags(user_id: int) -> None:
    """Drop one user's cached flags (call right after a flag changes)."""
    _cache.pop(int(user_id), None)


def clear_user_flags_cache() -> None:
    """Drop every cached pair (tests / after a bulk change)."""
    _cache.clear()


async def load_user_flags(user_id: int) -> tuple[bool, bool]:
    """Return ``(is_admin, is_banned)`` for ``user_id``.

    Root admins (``ADMIN_IDS``) are answered from ``.env`` with no query at
    all. For everyone else one query fetches all three columns — never the
    whole ``User`` row — and the result is cached for :data:`FLAGS_TTL`.
    """
    user_id = int(user_id)

    # Root admins can never be blocked and never need a database round trip.
    if user_id in settings.admin_ids_list:
        return True, False

    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and (now - cached[0]) < FLAGS_TTL:
        return cached[1], cached[2]

    is_admin, is_banned, is_complete = await _fetch_flags(user_id)
    _cache[user_id] = (now, is_admin, is_banned, is_complete)
    return is_admin, is_banned


async def _fetch_flags(user_id: int) -> tuple[bool, bool, bool]:
    """One DB SELECT for all three flags — the only query this module issues."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(
                User.is_admin, User.is_banned, User.is_profile_complete
            ).where(User.telegram_id == user_id)
        )
        row = result.one_or_none()
    return (
        (bool(row[0]), bool(row[1]), bool(row[2]))
        if row is not None
        else (False, False, False)
    )


async def load_profile_complete(user_id: int) -> bool:
    """Return ``is_profile_complete`` — the third column of the same cached row.

    Used by the ``IsProfileComplete`` filter so the profile check costs ZERO
    extra round trips whenever the ``(is_admin, is_banned)`` pair was already
    loaded for this user in this TTL window (and one shared query otherwise).
    """
    user_id = int(user_id)

    # Deliberately NO root-admin short-circuit here: "is admin" comes from
    # ``.env``, but "profile complete" is a database fact and a root admin who
    # never ran the wizard is still incomplete. (The filter is currently not
    # attached to any router, so this is about keeping the helper honest.)
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and (now - cached[0]) < FLAGS_TTL:
        return cached[3]

    is_admin, is_banned, is_complete = await _fetch_flags(user_id)
    _cache[user_id] = (now, is_admin, is_banned, is_complete)
    return is_complete


__all__ = [
    "FLAGS_TTL",
    "clear_user_flags_cache",
    "forget_user_flags",
    "load_user_flags",
    "load_profile_complete",
]
