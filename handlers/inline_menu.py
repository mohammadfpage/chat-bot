"""
The three-option menu of the inline picker.

Typing a bare ``@bot_username`` is the first thing every single user does, and
until now it answered with one article that only described the whisper syntax.
This module is that answer, rebuilt as the three things a newcomer actually
wants from the bot:

    آموزش ارسال نجوا   → the tutorial, right there in the chat they typed in
    درخواست نجوا      → "here is my numeric id, whisper me"
    درخواست پیام ناشناس → "let us talk privately, without knowing each other"

Every option is an :class:`InlineQueryResultArticle`, so Telegram publishes the
message itself the instant the user taps a row — the bot never has to send it,
which matters because the bot cannot DM anyone who has not started it. All three
therefore carry their buttons inside the result.

Division of labour
------------------
This module owns PRESENTATION (titles, descriptions, card texts, buttons) and
knows nothing about how a whisper query is parsed. The pre-filled query of
«درخواست نجوا» is handed in by :mod:`handlers.inline_anon`, which owns the
syntax — so the button a user taps cannot teach a shape the parser rejects.
"""

from __future__ import annotations

from html import escape

from aiogram.types import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    User,
)

from config import settings
from keyboards import (
    anon_chat_request_kb,
    inline_tutorial_kb,
    inline_whisper_request_kb,
)

# ──────────────────────────────────────────────────
# Prefixes of the cards these results publish
# ──────────────────────────────────────────────────
#
# ``handlers.inline_anon`` deletes the stub Telegram posts when an inline query
# is unparsable. These three cards are NOT stubs: the user picked them on
# purpose and their content is the whole point. They are recognised by their
# first characters, exactly like the whisper card above them is.

TUTORIAL_CARD_PREFIX = "کاربر عزیز"
WHISPER_REQUEST_CARD_PREFIX = "🆔"
ANON_CHAT_CARD_PREFIX = "💬"

#: Every card the menu is allowed to leave in a chat.
MENU_CARD_PREFIXES = (
    TUTORIAL_CARD_PREFIX,
    WHISPER_REQUEST_CARD_PREFIX,
    ANON_CHAT_CARD_PREFIX,
)


# ──────────────────────────────────────────────────
# Picker rows
# ──────────────────────────────────────────────────

_TUTORIAL_TITLE = "آموزش ارسال نجوا"
_TUTORIAL_DESCRIPTION = "💡 بلد نیستی؟ اینجا رو بزن"

_WHISPER_TITLE = "درخواست نجوا"
_ANON_CHAT_TITLE = "درخواست پیام ناشناس"
_ANON_CHAT_DESCRIPTION = "💡 مناسب گروه و کانال"


def whisper_request_description(user_id: int) -> str:
    """The description of «درخواست نجوا»: the reader's own numeric id.

    It has to be in the DESCRIPTION, not only in the card: while the picker is
    open the description is the only thing on screen, and this row's entire job
    is to let somebody copy that number without tapping anything.
    """
    return f"{WHISPER_REQUEST_CARD_PREFIX} آیدی‌عددی من: {user_id}"


# ──────────────────────────────────────────────────
# Cards
# ──────────────────────────────────────────────────

def tutorial_card_text(first_name: str, bot_username: str) -> str:
    """The «آموزش ارسال نجوا» card.

    Five steps, one word each. The ordering rule is what the second button
    teaches, so the recipient stays the LAST item of the sentence — and the
    long form lives in :func:`tutorial_guide_text` for anyone who needs it.
    """
    who = escape(first_name or "کاربر")
    bot = escape(bot_username or "bot")
    return (
        f"{TUTORIAL_CARD_PREFIX} <b>{who}</b>\n\n"
        "<b>ترتیب نوشتن:</b>\n"
        "1️⃣ آیدی ربات\n"
        "2️⃣ فاصله\n"
        "3️⃣ متن نجوا\n"
        "4️⃣ فاصله\n"
        "5️⃣ آیدی گیرنده (یوزرنیم یا عدد)\n\n"
        "<b>نمونه 👇</b>\n"
        f"<code>@{bot} متن نجوا @SomeUser</code>\n\n"
        "🔒 متن نجوا وارد گفتگو نمی‌شود؛ فقط گیرنده می‌بیند."
    )


def tutorial_guide_text() -> str:
    """The card «🖼️ عکس آموزشی» expands into when no photo is configured.

    One worked example instead of five separate steps: the pattern is obvious
    once it is shown whole, and this is the branch that keeps the button from
    ever being a dead end.
    """
    return (
        "🖼 <b>آموزش نوشتن نجوا</b>\n\n"
        "بیا به «سارا» نجوا بدهیم. داخل همان گروه بنویس:\n\n"
        "<code>@BotUsername سلام، فردا تلفنی حرف بزنیم @Sara</code>\n\n"
        "یعنی: آیدی ربات + فاصله + متن نجوا + فاصله + آیدی سارا.\n\n"
        "دکمهٔ «ارسال» را بزن؛ یک کارت کوچک در گروه می‌نشیند که فقط سارا "
        "می‌تواند باز کند."
    )


def whisper_request_card_text(user_id: int) -> str:
    """The «درخواست نجوا» card: a card you can hand out to be whispered at."""
    return (
        f"{WHISPER_REQUEST_CARD_PREFIX} <b>آیدی‌عددی من:</b> "
        f"<code>{user_id}</code>\n\n"
        "برای دریافت نجوای ناشناس، دکمهٔ زیر را بزن 👇"
    )


def anon_chat_card_text(requester_id: int) -> str:
    """The «درخواست پیام ناشناس» card posted in a group or a private chat.

    Carries no name, no photo and nothing the requester would recognise
    themselves by — the anonymity of the feature is the point of the card, so
    the card must not leak the requester back to them.
    """
    return (
        f"{ANON_CHAT_CARD_PREFIX} <b>درخواست پیام ناشناس</b>\n\n"
        "می‌خواهی ناشناس با هم چت کنیم؟ دکمهٔ زیر را بزن.\n\n"
        "🔒 تا وقتی نپذیرم، نام و آیدی‌ات نزد من نمایش داده نمی‌شود."
    )


# ──────────────────────────────────────────────────
# Articles
# ──────────────────────────────────────────────────

def _article(
    result_id: str,
    title: str,
    description: str,
    message_text: str,
    *,
    thumbnail: str,
    reply_markup=None,
) -> InlineQueryResultArticle:
    """Build one picker row.

    ``thumbnail_url`` is only set when a URL is configured: the Bot API
    validates it (http/https, ≤1 KB, ≤200×200) and an empty string is a hard
    error, so the field has to be omitted rather than blanked. A row without a
    thumbnail is valid and renders perfectly well.
    """
    article = InlineQueryResultArticle(
        id=result_id,
        title=title[:256],
        description=description[:256],
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=reply_markup,
    )
    if thumbnail:
        article.thumbnail_url = thumbnail
        article.thumbnail_width = 64
        article.thumbnail_height = 64
    return article


def build_menu_results(
    *,
    bot_username: str,
    user: User,
    whisper_prefill: str,
    recipient_ref: str,
) -> list[InlineQueryResultArticle]:
    """The three rows shown for a bare ``@bot`` query, in display order.

    Args:
        bot_username: this bot's username, without ``@``.
        user: the person typing the query — its id and first name are what the
            first two cards address.
        whisper_prefill: the query «ارسال نجوا به من» types for the user. Built
            by the caller because it owns the whisper syntax.
        recipient_ref: how the reader refers to THEMSELVES inside a whisper
            query (``@username`` when they have one, otherwise the numeric id).
            It is what the tutorial's second button pre-fills.

    The order is not arbitrary: teach → tell them your id → talk to me. It is
    also the order that keeps the anonymity promise: the third card is the one
    a stranger can use without ever learning who posted it.
    """
    user_id = user.id
    first_name = user.first_name or "کاربر"
    clean_bot = (bot_username or "bot").lstrip("@")

    return [
        _article(
            "menu-tutorial",
            _TUTORIAL_TITLE,
            _TUTORIAL_DESCRIPTION,
            tutorial_card_text(first_name, clean_bot),
            thumbnail=settings.thumbnail_url("tutorial"),
            reply_markup=inline_tutorial_kb(
                first_name,
                recipient_ref,
                tutorial_photo_url=settings.tutorial_photo_url,
            ),
        ),
        _article(
            "menu-whisper",
            _WHISPER_TITLE,
            whisper_request_description(user_id),
            whisper_request_card_text(user_id),
            thumbnail=settings.thumbnail_url("whisper"),
            reply_markup=inline_whisper_request_kb(whisper_prefill),
        ),
        _article(
            "menu-anon-chat",
            _ANON_CHAT_TITLE,
            _ANON_CHAT_DESCRIPTION,
            anon_chat_card_text(user_id),
            thumbnail=settings.thumbnail_url("anon_chat"),
            reply_markup=anon_chat_request_kb(user_id),
        ),
    ]


__all__ = [
    "ANON_CHAT_CARD_PREFIX",
    "MENU_CARD_PREFIXES",
    "TUTORIAL_CARD_PREFIX",
    "WHISPER_REQUEST_CARD_PREFIX",
    "anon_chat_card_text",
    "build_menu_results",
    "tutorial_card_text",
    "tutorial_guide_text",
    "whisper_request_card_text",
    "whisper_request_description",
]