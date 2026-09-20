"""
Inline keyboards used for non-persistent prompts (force join, blocked list, inbox, etc.).

CRITICAL: All InlineKeyboardButton instances use ONLY standard arguments
(text, callback_data). Do NOT use style, icon_custom_emoji_id, or any
non-standard kwargs to prevent TelegramBadRequest crashes.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def force_join_kb(channel_username: str = "") -> InlineKeyboardMarkup:
    """
    Inline keyboard for the forced-join verification flow.

    Shows: [📣 عضویت در کانال]  →  [✅ عضو شدم]
    """
    builder = InlineKeyboardBuilder()
    channel_username = channel_username.strip().lstrip("@")
    if channel_username:
        builder.row(
            InlineKeyboardButton(
                text="📣 عضویت در کانال",
                url=f"https://t.me/{channel_username}",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="✅ عضو شدم",
            callback_data="check_membership",
        ),
    )
    return builder.as_markup()


def blocked_list_kb(blocked_entries: list[dict]) -> InlineKeyboardMarkup:
    """
    Inline keyboard showing blocked users with unblock buttons.

    Args:
        blocked_entries: list of dicts with keys:
            - "blocked_id": int (Telegram ID of the blocked user)
            - "label": str (display name or fallback)
    """
    builder = InlineKeyboardBuilder()
    for entry in blocked_entries:
        uid = entry["blocked_id"]
        label = entry.get("label") or f"کاربر {uid}"
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {label}",
                callback_data=f"unblock:{uid}",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="↩️ بازگشت",
            callback_data="blocked_list:back",
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Anonymous Inbox — unread messages
# ──────────────────────────────────────────────────

def unread_inbox_kb(sender_groups: dict[int, list]) -> InlineKeyboardMarkup:
    """
    Inline keyboard listing anonymous guests who sent unread messages.

    Args:
        sender_groups: dict mapping sender_id -> list of AnonymousMessage objects.

    Layout:
        [👤 کاربر ۱ (X پیام)]   (one row per sender)
        [👤 کاربر ۲ (Y پیام)]
        ...
        [↩️ بازگشت]   (back to main menu)
    """
    builder = InlineKeyboardBuilder()
    for idx, (sender_id, messages) in enumerate(sender_groups.items(), start=1):
        count = len(messages)
        persian_count = _to_persian_digits(count)
        builder.row(
            InlineKeyboardButton(
                text=f"👤 کاربر {_to_persian_digits(idx)} ({persian_count} پیام)",
                callback_data=f"anon_open:{sender_id}",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="↩️ بازگشت",
            callback_data="inbox:back",
        ),
    )
    return builder.as_markup()


def read_message_kb(message_id: int, sender_id: int) -> InlineKeyboardMarkup:
    """
    Inline keyboard shown when reading a single anonymous message (legacy).
    Kept for backwards compatibility.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🚫 بلاک فرستنده",
            callback_data=f"anon_block:{message_id}:{sender_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="↩️ بازگشت",
            callback_data="inbox:open",
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

def _to_persian_digits(n: int) -> str:
    """Convert an integer to Persian digit characters."""
    persian = "۰۱۲۳۴۵۶۷۸۹"
    return "".join(persian[int(d)] for d in str(n))
