"""
User-facing inline keyboards.

Every button follows the dual-fallback emoji strategy from
``utils/emojis.py``:

  1. ``get_plain_emoji(key)`` injects the plain Unicode emoji into ``text``
     (visible today without a Premium subscription).
  2. ``icon_custom_emoji_id=get_premium_id(key)`` always passes the premium
     ID if present; it safely returns ``None`` today, which aiogram ignores.

Note: ``main_menu_kb`` (the persistent ReplyKeyboardMarkup used for chat
state navigation) lives in ``keyboards/reply.py`` and is unchanged. The
functions here are inline keyboards for user-facing prompts and menus.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.emojis import get_plain_emoji, get_premium_id


# ──────────────────────────────────────────────────────────────────────────
# Internal helper — builds a single, consistently-styled inline button.
# ──────────────────────────────────────────────────────────────────────────
def _button(
    text: str,
    callback_data: str,
    emoji: str,
    *,
    style: str = "primary",
) -> InlineKeyboardButton:
    """Create an inline button with an emoji and native styling."""
    return InlineKeyboardButton(
        text=f"{get_plain_emoji(emoji)} {text}",
        callback_data=callback_data,
        style=style,
        icon_custom_emoji_id=get_premium_id(emoji),
    )


def main_menu_inline_kb() -> InlineKeyboardMarkup:
    """
    User inline main menu (e.g., shown inside a welcome message when the
    persistent reply menu is not appropriate).
    Layout:
        [🔗 اتصال به ناشناس]  [👤 پروفایل من]
        [🏆 امتیازات]          [📋 راهنما]
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('connect')} اتصال به ناشناس",
        callback_data="user:connect",
        style="primary",
        icon_custom_emoji_id=get_premium_id("connect"),
    )
    builder.button(
        text=f"{get_plain_emoji('profile')} پروفایل من",
        callback_data="user:profile",
        style="primary",
        icon_custom_emoji_id=get_premium_id("profile"),
    )
    builder.button(
        text=f"{get_plain_emoji('points')} امتیازات",
        callback_data="user:points",
        style="secondary",
        icon_custom_emoji_id=get_premium_id("points"),
    )
    builder.button(
        text=f"{get_plain_emoji('help')} راهنما",
        callback_data="user:help",
        style="secondary",
        icon_custom_emoji_id=get_premium_id("help"),
    )
    builder.adjust(2, 2)
    return builder.as_markup()


def welcome_inline_kb() -> InlineKeyboardMarkup:
    """
    Welcome prompt shown right after the user starts the bot.
    Layout:
        [🔗 شروع چت ناشناس]
        [📬 لینک ناشناس من]  [👤 تکمیل پروفایل]
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('connect')} شروع چت ناشناس",
        callback_data="user:welcome:start",
        style="primary",
        icon_custom_emoji_id=get_premium_id("connect"),
    )
    builder.button(
        text=f"{get_plain_emoji('link')} لینک ناشناس من",
        callback_data="user:welcome:link",
        style="secondary",
        icon_custom_emoji_id=get_premium_id("link"),
    )
    builder.button(
        text=f"{get_plain_emoji('profile')} تکمیل پروفایل",
        callback_data="user:welcome:profile",
        style="success",
        icon_custom_emoji_id=get_premium_id("profile"),
    )
    builder.adjust(1, 2)
    return builder.as_markup()


__all__ = [
    "main_menu_inline_kb",
    "welcome_inline_kb",
]
