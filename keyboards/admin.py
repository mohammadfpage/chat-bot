"""
Admin panel inline keyboards.

Every button follows the dual-fallback emoji strategy from
``utils/emojis.py``:

  1. ``get_plain_emoji(key)`` injects the plain Unicode emoji into ``text``
     (visible today without a Premium subscription).
  2. ``icon_custom_emoji_id=get_premium_id(key)`` always passes the premium
     ID if present; it safely returns ``None`` today, which aiogram ignores.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.emojis import get_plain_emoji, get_premium_id

# ──────────────────────────────────────────────────────────────────────────
# Constants — shared callback prefixes/values
# ──────────────────────────────────────────────────────────────────────────
BACK_TO_PANEL = "admin:panel"


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


# ──────────────────────────────────────────────────────────────────────────
# Admin Panel — main menu
# ──────────────────────────────────────────────────────────────────────────
def admin_panel_kb(*, is_root: bool = False) -> InlineKeyboardMarkup:
    """
    Main admin panel.

    Root Admins additionally see the "مدیریت ادمین‌ها" section.

    Args:
        is_root: ``True`` when the viewer is a Root (Super) Admin.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('stats')} آمار زنده",
        callback_data="admin:stats",
        style="primary",
        icon_custom_emoji_id=get_premium_id("stats"),
    )
    builder.button(
        text=f"{get_plain_emoji('broadcast')} ارسال همگانی",
        callback_data="admin:broadcast",
        style="primary",
        icon_custom_emoji_id=get_premium_id("broadcast"),
    )
    builder.button(
        text=f"{get_plain_emoji('users')} مدیریت کاربران",
        callback_data="admin:users",
        style="primary",
        icon_custom_emoji_id=get_premium_id("users"),
    )
    if is_root:
        builder.button(
            text=f"{get_plain_emoji('crown')} مدیریت ادمین‌ها",
            callback_data="admin:admins",
            style="danger",
            icon_custom_emoji_id=get_premium_id("crown"),
        )
    builder.adjust(2, 1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Live Stats
# ──────────────────────────────────────────────────────────────────────────
def admin_stats_kb() -> InlineKeyboardMarkup:
    """Keyboard under live stats — refresh or go back."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('refresh')} به‌روزرسانی",
        callback_data="admin:stats",
        style="primary",
        icon_custom_emoji_id=get_premium_id("refresh"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="secondary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Broadcast (confirmation step)
# ──────────────────────────────────────────────────────────────────────────
def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Confirm or cancel the broadcast before it is actually sent."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} ارسال کن",
        callback_data="admin:broadcast:confirm",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:broadcast:cancel",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.adjust(2)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# User management (ban / unban)
# ──────────────────────────────────────────────────────────────────────────
def admin_users_kb() -> InlineKeyboardMarkup:
    """Sub-menu: ban or unban a user by ID."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('ban')} بلاک‌کردن کاربر",
        callback_data="admin:user:ban",
        style="danger",
        icon_custom_emoji_id=get_premium_id("ban"),
    )
    builder.button(
        text=f"{get_plain_emoji('unban')} رفع بلاک کاربر",
        callback_data="admin:user:unban",
        style="success",
        icon_custom_emoji_id=get_premium_id("unban"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="secondary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Root-only: manage admins
# ──────────────────────────────────────────────────────────────────────────
def admin_manage_admins_kb() -> InlineKeyboardMarkup:
    """Promote / demote admins (Root Admins only)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} ارتقا به ادمین",
        callback_data="admin:admins:promote",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} برکناری ادمین",
        callback_data="admin:admins:demote",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.button(
        text=f"{get_plain_emoji('users')} لیست ادمین‌ها",
        callback_data="admin:admins:list",
        style="secondary",
        icon_custom_emoji_id=get_premium_id("users"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="secondary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2, 1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Cancel helper — used inside FSM "enter an ID / username" flows
# ──────────────────────────────────────────────────────────────────────────
def admin_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel the current admin input flow and return to the panel."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:cancel_input",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    return builder.as_markup()


__all__ = [
    "BACK_TO_PANEL",
    "admin_panel_kb",
    "admin_stats_kb",
    "broadcast_confirm_kb",
    "admin_users_kb",
    "admin_manage_admins_kb",

    "admin_cancel_kb",
]