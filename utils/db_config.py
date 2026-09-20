"""
Database-backed system configuration — DEPRECATED.

Channel settings now live in .env via config.py. This module is kept only
for backward compatibility. All functions now delegate to the config module.
"""

from __future__ import annotations

from config import settings

# ── Known setting keys (kept for reference) ──
KEY_CHANNEL_USERNAME = "force_join_channel_username"
KEY_CHANNEL_ID = "force_join_channel_id"


async def get_channel_username() -> str:
    """Return the current force-join channel username from .env config."""
    return settings.channel_username.strip().lstrip("@")


async def get_channel_id() -> int:
    """Return the current force-join channel id from .env config."""
    return settings.channel_id


async def set_channel_username(username: str) -> None:
    """No-op: channel username is configured in .env, not at runtime."""
    pass


async def set_channel_id(channel_id: int) -> None:
    """No-op: channel id is configured in .env, not at runtime."""
    pass


async def get_channel_settings() -> dict[str, str | int]:
    """
    Return the full force-join channel configuration from .env.

    Returns:
        ``{"username": str, "id": int}`` (either may be empty/0).
    """
    username = settings.channel_username.strip().lstrip("@")
    channel_id = settings.channel_id
    return {"username": username, "id": channel_id}


__all__ = [
    "KEY_CHANNEL_USERNAME",
    "KEY_CHANNEL_ID",
    "get_channel_username",
    "get_channel_id",
    "set_channel_username",
    "set_channel_id",
    "get_channel_settings",
]
