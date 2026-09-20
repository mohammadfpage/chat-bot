"""
Utility helpers: text formatting, rate limiting, etc.
"""

from __future__ import annotations

import asyncio
import time
from functools import wraps
from typing import Callable, Awaitable, Any

from aiogram.types import Message


# ──────────────────────────────────────────────────
# Text formatters
# ──────────────────────────────────────────────────

def format_user_profile(
    telegram_id: int,
    first_name: str | None,
    username: str | None,
    age: int | None,
    city: str | None,
    height: str | None,
) -> str:
    """Return an HTML-formatted user profile string."""
    return (
        f"👤 <b>پروفایل کاربر</b>\n\n"
        f"🆔 آیدی: <code>{telegram_id}</code>\n"
        f"📛 نام: {first_name or 'نامشخص'}\n"
        f"📛 یوزرنیم: @{username or 'ندارد'}\n"
        f"🎂 سن: {age or '—'}\n"
        f"🏙 شهر: {city or '—'}\n"
        f"📏 قد: {height or '—'}"
    )


def format_bot_stats(
    total_users: int,
    active_users: int,
    banned_users: int,
    active_chats: int,
    queue_size: int,
) -> str:
    """Return an HTML-formatted statistics string."""
    return (
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کل کاربران: {total_users}\n"
        f"✅ کاربران فعال: {active_users}\n"
        f"⛔ بلاک شده: {banned_users}\n"
        f"💬 چت‌های فعال: {active_chats}\n"
        f"🔍 در صف جستجو: {queue_size}"
    )


# ──────────────────────────────────────────────────
# Simple per-user rate limiter (in-memory)
# ──────────────────────────────────────────────────

class RateLimiter:
    """
    Token-bucket rate limiter per user.
    Usage:
        limiter = RateLimiter(rate=5, per=10.0)  # 5 actions per 10 seconds
        if not limiter.allow(user_id):
            await message.answer("لطفاً صبر کنید...")
            return
    """

    def __init__(self, rate: int = 5, per: float = 10.0):
        self.rate = rate
        self.per = per
        self._tokens: dict[int, list[float]] = {}

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._tokens.setdefault(user_id, [])
        # Purge old entries
        timestamps[:] = [t for t in timestamps if now - t < self.per]
        if len(timestamps) >= self.rate:
            return False
        timestamps.append(now)
        return True


# Global rate limiter instance
chat_rate_limiter = RateLimiter(rate=30, per=60.0)  # 30 messages / minute
