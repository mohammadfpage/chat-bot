"""
Whisper feature configuration accessor.

A single ``whisper_config`` row (id=1) holds the master switch, the forced-join
requirement and the anti-spam limits. It is cached in memory for a few seconds
so that a hot path (sending + viewing a whisper) never hits the DB more than
once per request, and so admin edits show up almost instantly.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from database import async_session_factory, WhisperConfig

CACHE_TTL = 5.0

_config_cache: WhisperConfig | None = None
_config_cache_at: float = 0.0


async def get_whisper_config() -> WhisperConfig:
    """Return the (cached) whisper config row, creating it if missing."""
    global _config_cache, _config_cache_at

    now = time.monotonic()
    if _config_cache is not None and (now - _config_cache_at) < CACHE_TTL:
        return _config_cache

    async with async_session_factory() as session:
        result = await session.execute(select(WhisperConfig).limit(1))
        config = result.scalar_one_or_none()
        if config is None:
            config = WhisperConfig()
            session.add(config)
            await session.commit()

    _config_cache = config
    _config_cache_at = time.monotonic()
    return config


async def refresh_whisper_config() -> WhisperConfig:
    """Force a reload — call right after an admin edits the config."""
    global _config_cache, _config_cache_at

    async with async_session_factory() as session:
        result = await session.execute(select(WhisperConfig).limit(1))
        config = result.scalar_one_or_none()
        if config is None:
            config = WhisperConfig()
            session.add(config)
            await session.commit()

    _config_cache = config
    _config_cache_at = time.monotonic()
    return config


async def is_whisper_enabled() -> bool:
    """True when the whisper feature is switched on for the whole bot."""
    config = await get_whisper_config()
    return bool(config.enabled)


__all__ = [
    "get_whisper_config",
    "refresh_whisper_config",
    "is_whisper_enabled",
]
