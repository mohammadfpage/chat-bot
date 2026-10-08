"""
Emoji Management System
========================
Centralized, future-proof emoji manager for the bot.

The bot does NOT have Telegram Premium yet, so we cannot use custom
`<tg-emoji emoji-id="...">` IDs. We therefore use plain Unicode fallback
emojis today, while keeping the FULL infrastructure ready so that
upgrading to Premium emojis later is a ~1-minute, zero-keyboard-edit task.

For every UI icon we store a mapping:  keyword -> (fallback_unicode, premium_id)

  * fallback_unicode : standard emoji shown today (and always as fallback).
  * premium_id       : numeric premium emoji ID, or ``None`` until the bot
                       subscribes to Telegram Premium.

To upgrade to Premium emojis later you ONLY need to fill in the real
premium IDs in ``PREMIUM_EMOJIS`` — the keyboard files never change.

Every fallback must NAME AN ACTION or an OBJECT — ``✅`` for confirm, ``🗑️`` for
delete, ``📊`` for stats. An abstract coloured circle is banned: it encodes a hue,
which the reader still has to translate into a verb, and it renders as a blank or
monochrome dot in several Telegram clients and in every light/dark theme that
recolours emoji. "This button is green" is not information; "this button
deletes" is.
"""

from __future__ import annotations

from typing import Final

# ──────────────────────────────────────────────────────────────────────────
# Master emoji registry
# Each value is a tuple: (fallback_unicode_emoji, premium_id_or_None)
# ──────────────────────────────────────────────────────────────────────────
PREMIUM_EMOJIS: Final[dict[str, tuple[str, str | None]]] = {
    # ── Navigation & main menu ────────────────────────────────────────
    "home": ("🏠", None),
    "back": ("🔙", None),
    "reply": ("↩️", None),
    "forward": ("➡️", None),
    "next": ("⏭️", None),
    "prev": ("⏮️", None),
    "menu": ("📃", None),
    "close": ("❌", None),
    "cancel": ("❌", None),
    "search": ("🔍", None),
    "refresh": ("🔄", None),
    "exchange": ("🔁", None),

    # ── Chat / anonymous chat ─────────────────────────────────────────
    "connect": ("🔗", None),
    "disconnect": ("🔌", None),
    "report": ("🛑", None),
    "queue": ("⏳", None),
    "chat": ("💬", None),
    "star": ("⭐", None),
    "blocks": ("🧱", None),
    # Matching modes: the three buttons a user picks to be matched, named by
    # what they ARE (a dice, a woman, a man) so the price list on the admin
    # panel reads as the same three services the users see.
    "dice": ("🎲", None),
    "female": ("👩", None),
    "male": ("👨", None),

    # ── User profile & account ────────────────────────────────────────
    "profile": ("👤", None),
    "user": ("🧑", None),
    "users": ("👥", None),
    "age": ("🎂", None),
    "city": ("🏙️", None),
    "height": ("📏", None),
    "link": ("📬", None),
    "points": ("🏆", None),
    "coins": ("🪙", None),
    "wallet": ("👛", None),
    "balance": ("💰", None),

    # ── Help & info ───────────────────────────────────────────────────
    "help": ("📖", None),
    "guide": ("📖", None),
    "rules": ("📜", None),
    "info": ("ℹ️", None),
    "question": ("❓", None),
    "faq": ("❓", None),

    # ── Validation / status ───────────────────────────────────────────
    "check": ("✅", None),
    "cross": ("❌", None),
    "warning": ("⚠️", None),
    "success": ("🎉", None),
    "error": ("⛔", None),
    "ban": ("🔨", None),
    "unban": ("🔓", None),
    "locked": ("🔒", None),
    "unlocked": ("🔓", None),
    "verified": ("✔️", None),
    "pending": ("🕓", None),
    "blocked": ("🛡️", None),

    # ── Admin panel ───────────────────────────────────────────────────
    "admin": ("👑", None),
    "gear": ("⚙️", None),
    "settings": ("⚙️", None),
    "stats": ("📊", None),
    "chart": ("📈", None),
    "broadcast": ("📢", None),
    "send": ("📤", None),
    "moderation": ("🛂", None),
    "mute": ("🔇", None),
    "unmute": ("🔊", None),
    "edit": ("✏️", None),
    "delete": ("🗑️", None),
    "trash": ("🗑️", None),
    "add": ("➕", None),
    "subtract": ("➖", None),
    "save": ("💾", None),
    "confirm": ("✅", None),
    "reject": ("❌", None),
    "list": ("📋", None),
    "database": ("🗄️", None),
    "security": ("🔐", None),
    "logs": ("🧾", None),
    "crown": ("👑", None),

    # ── Shop / store (future-proofing) ────────────────────────────────
    "shop": ("🛒", None),
    "cart": ("🛒", None),
    "bag": ("👜", None),
    "store": ("🏬", None),
    "buy": ("🛍️", None),
    "sell": ("💸", None),
    "discount": ("🏷️", None),
    "gift": ("🎁", None),
    "boost": ("🚀", None),
    "premium": ("💎", None),
    "heart": ("❤️", None),
    "sparkles": ("✨", None),
    "fire": ("🔥", None),
    "thumbsup": ("👍", None),
    "thumbsdown": ("👎", None),

    # ── Categories (content) ──────────────────────────────────────────
    "category": ("🗂️", None),
    "food": ("🍔", None),
    "drink": ("🥤", None),
    "game": ("🎮", None),
    "music": ("🎵", None),
    "movie": ("🎬", None),
    "book": ("📚", None),
    "tech": ("💻", None),
    "fashion": ("👕", None),

    # ── Miscellaneous ─────────────────────────────────────────────────
    "clock": ("🕒", None),
    "calendar": ("📅", None),
    "phone": ("📞", None),
    "mail": ("📧", None),
    "pin": ("📌", None),
    "bell": ("🔔", None),
    "eye": ("👁️", None),
    "show": ("👁️", None),
    "magnet": ("🧲", None),
    "key": ("🔑", None),
    "camera": ("📷", None),
    "video": ("🎥", None),
}


def get_pe(key: str) -> str:
    """
    Return the HTML snippet for a custom emoji when a Premium ID exists,
    otherwise return the plain Unicode fallback.

    When a premium ID is set, the returned string is suitable for use
    inside any HTML message / caption:
        <tg-emoji emoji-id="12345">😀</tg-emoji>

    Args:
        key: The keyword keyed in ``PREMIUM_EMOJIS``.

    Returns:
        The `<tg-emoji>` snippet if a premium ID exists, else the fallback.
    """
    fallback, emoji_id = PREMIUM_EMOJIS[key]
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def get_plain_emoji(key: str) -> str:
    """
    Return ONLY the plain fallback Unicode emoji for the given key.

    This is what should be injected into button ``text`` fields today,
    guaranteeing the emoji is visible without a Premium subscription.

    Args:
        key: The keyword keyed in ``PREMIUM_EMOJIS``.

    Returns:
        The fallback Unicode emoji string.
    """
    fallback, _ = PREMIUM_EMOJIS[key]
    return fallback


def get_premium_id(key: str) -> str | None:
    """
    Return the numeric Premium emoji ID as a string if one exists,
    otherwise return ``None``.

    Passing this directly to ``icon_custom_emoji_id=...`` is safe: when it
    returns ``None``, aiogram simply ignores the argument, so you can call
    it unconditionally on every button.

    Args:
        key: The keyword keyed in ``PREMIUM_EMOJIS``.

    Returns:
        The premium ID as a string, or ``None`` if not set.
    """
    _, emoji_id = PREMIUM_EMOJIS[key]
    return emoji_id


__all__ = [
    "PREMIUM_EMOJIS",
    "get_pe",
    "get_plain_emoji",
    "get_premium_id",
]
