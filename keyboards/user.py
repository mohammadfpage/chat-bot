"""
User-facing inline keyboards.

Buttons carry a *semantic* icon rather than a literal picture of themselves: the
icon says what tapping the button will DO, so the reader can scan a screen and
find the one destructive or confirming control without reading a single word.

    ✅ confirm / accept / start        ❌ decline / cancel
    👁️ show / read                   🗑️ delete
    📊 stats                          ⚙️ options / settings
    ↩️ reply                          🔙 back / navigation
    🔗 link / connect                 📢 join
    📖 guide / tutorial               🎁 reward / claim

A menu whose every row is the same kind of thing keeps its own icons
(:func:`main_menu_inline_kb`): four identical icons side by side say nothing that
the labels did not already say.

The plain Unicode icon is used rather than
``icon_custom_emoji_id=get_premium_id(key)`` on purpose. A premium custom-emoji
icon only renders for clients that can draw it, and it disappears for everyone
else; an icon inside the button label survives every client, every theme, and the
plain-keyboard round trip an ``InlineQueryResultArticle`` markup has to make. The
icon and the native ``style`` do not conflict — the first names the action, the
second tints the button — so both are set where both are wanted.

Note: ``main_menu_kb`` (the persistent ReplyKeyboardMarkup used for chat
state navigation) lives in ``keyboards/reply.py`` and is unchanged. The
functions here are inline keyboards for user-facing prompts and menus.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from keyboards.inline import (
    ICON_BACK,
    ICON_CHAT,
    ICON_CLAIM,
    ICON_CONFIRM,
    ICON_DECLINE,
    ICON_EXCHANGE,
    ICON_GUIDE,
    ICON_HISTORY,
    ICON_INFO,
    ICON_JOIN,
    ICON_LINK,
    ICON_OPTIONS,
    ICON_REPLY,
    add_to_group_link,
)

__all__ = [
    "main_menu_inline_kb",
    "welcome_group_kb",
    "welcome_inline_kb",
    "wallet_kb",
    "wallet_history_kb",
    "rematch_offer_kb",
    "profile_card_kb",
    "profile_edit_kb",
    "support_menu_kb",
    "support_history_kb",
    "support_ticket_kb",
]


def main_menu_inline_kb() -> InlineKeyboardMarkup:
    """
    User inline main menu (e.g., shown inside a welcome message when the
    persistent reply menu is not appropriate).

    Four destinations, no destructive control: this is a menu, so every row
    keeps its own icon rather than one shared marker.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{ICON_LINK} اتصال به ناشناس", callback_data="user:connect")
    builder.button(text="👤 پروفایل من", callback_data="user:profile")
    builder.button(text="🏆 امتیازات", callback_data="user:points")
    builder.button(text=f"{ICON_GUIDE} راهنما", callback_data="user:help")
    builder.adjust(2, 2)
    return builder.as_markup()


def welcome_inline_kb() -> InlineKeyboardMarkup:
    """
    Welcome prompt shown right after the user starts the bot.

    Layout:
        [💬 شروع چت ناشناس]
        [🔗 لینک ناشناس من]   [👤 تکمیل پروفایل]
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_CHAT} شروع چت ناشناس",
        callback_data="user:welcome:start",
        style="success",
    )
    builder.button(
        text=f"{ICON_LINK} لینک ناشناس من",
        callback_data="user:welcome:link",
        style="primary",
    )
    builder.button(
        text="👤 تکمیل پروفایل",
        callback_data="user:welcome:profile",
        style="success",
    )
    builder.adjust(1, 2)
    return builder.as_markup()


def welcome_group_kb(bot_username: str) -> InlineKeyboardMarkup | None:
    """The «add me to your group» card shown under the ``/start`` welcome.

    A whisper only makes sense inside a group, so this is the one control that
    turns a private conversation into a working feature — and the link asks for
    admin rights on the way in, which is what makes the roster (and therefore
    ``@username`` targeting) work afterwards. See :func:`add_to_group_link` for
    the link's exact form.

    Its own message rather than extra rows on the greeting, because a message
    carries exactly one markup and the greeting's reply keyboard is how the user
    navigates; dropping that for a URL button would be a bad trade.

    ``None`` when there is no username to build a link from, so the caller can
    skip the message instead of posting an empty card.
    """
    link = add_to_group_link(bot_username)
    if not link:
        return None

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_JOIN} افزودن ربات به گروه",
            url=link,
            style="primary",
        ),
    )
    return builder.as_markup()


def wallet_kb(daily_claimable: bool = False) -> InlineKeyboardMarkup:
    """
    Inline keyboard for the coins wallet screen.

    Layout:
        [🎁 دریافت جایزه روزانه]   (only when claimable)
        [ℹ️ اطلاعات]               (only when there is nothing to claim)
        [📜 تاریخچه سکه]
        [🔙 بازگشت به منوی اصلی]

    There is no convert button any more: coins are the only currency, so there
    is nothing to exchange them for.
    """
    builder = InlineKeyboardBuilder()
    if daily_claimable:
        builder.button(
            text=f"{ICON_CLAIM} دریافت جایزه روزانه",
            callback_data="wallet:daily",
            style="success",
        )
    else:
        builder.button(
            text=f"{ICON_INFO} اطلاعات",
            callback_data="wallet:info",
            style="primary",
        )
    builder.button(
        text=f"{ICON_HISTORY} تاریخچه سکه",
        callback_data="wallet:history",
        style="primary",
    )
    builder.button(
        text=f"{ICON_BACK} بازگشت به منوی اصلی",
        callback_data="wallet:back",
        style="primary",
    )
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def wallet_history_kb() -> InlineKeyboardMarkup:
    """Back row for the coin-history card — returns inside the wallet, not to
    the main menu, because the history is a drill-down of the wallet screen."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_BACK} بازگشت به کیف پول",
        callback_data="wallet:open",
        style="primary",
    )
    builder.adjust(1)
    return builder.as_markup()


def rematch_offer_kb() -> InlineKeyboardMarkup:
    """Accept / decline row on the rematch request sent to the partner."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_CONFIRM} پذیرش اتصال مجدد",
        callback_data="rematch:accept",
        style="success",
    )
    builder.button(
        text=f"{ICON_DECLINE} رد درخواست",
        callback_data="rematch:decline",
        style="danger",
    )
    builder.adjust(1)
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Profile  (own card + «✏️ ویرایش پروفایل» section)
# ──────────────────────────────────────────────────

def profile_card_kb() -> InlineKeyboardMarkup:
    """The single «✏️ ویرایش پروفایل» button under the user's own profile card.

    Inline rather than a reply button so it sits directly beneath the card —
    the reply menu, which is persistent, stays on screen at the same time.

    Layout:
        [✏️ ویرایش پروفایل]
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_OPTIONS} ویرایش پروفایل",
        callback_data="profile:edit",
        style="primary",
    )
    builder.adjust(1)
    return builder.as_markup()


def profile_edit_kb() -> InlineKeyboardMarkup:
    """The «ویرایش پروفایل» section: one button per editable field.

    Every field gets its own row button so the user edits exactly one value and
    nothing else changes — the whole point of the picker over re-running the
    setup wizard.

    Layout:
        [🎂 سن]         [🏙 شهر]
        [🚻 جنسیت]      [📏 قد]
        [🖼 عکس پروفایل]
        [🔙 بازگشت]
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🎂 سن", callback_data="profile:edit:age", style="primary")
    builder.button(text="🏙 شهر", callback_data="profile:edit:city", style="primary")
    builder.button(
        text="🚻 جنسیت", callback_data="profile:edit:gender", style="primary"
    )
    builder.button(text="📏 قد", callback_data="profile:edit:height", style="primary")
    builder.button(
        text="🖼 عکس پروفایل", callback_data="profile:edit:photo", style="primary"
    )
    builder.button(
        text=f"{ICON_BACK} بازگشت", callback_data="profile:edit:back", style="primary"
    )
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Support  («🎧 پشتیبانی»)
# ──────────────────────────────────────────────────

def support_menu_kb(*, has_history: bool = False) -> InlineKeyboardMarkup:
    """The support home card.

    Layout:
        [💬 نوشتن پیام]                 (always — the primary action)
        [📜 پیگیری‌های من]               (only when the user has any ticket)
        [🔙 بازگشت به منوی اصلی]

    The history row is hidden for a first-time visitor: an empty list is not a
    destination, and showing it would make a two-button card look like it has a
    dead option.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_CHAT} نوشتن پیام",
        callback_data="support:write",
        style="success",
    )
    if has_history:
        builder.button(
            text=f"{ICON_HISTORY} پیگیری‌های من",
            callback_data="support:history",
            style="primary",
        )
    builder.button(
        text=f"{ICON_BACK} بازگشت به منوی اصلی",
        callback_data="support:back",
        style="primary",
    )
    builder.adjust(1)
    return builder.as_markup()


def support_history_kb(rows: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One button per past ticket + a way home.

    ``rows`` is ``[(ticket_id, label), …]`` built by the handler, because only
    the handler knows the peer's status text and date.
    """
    builder = InlineKeyboardBuilder()
    for ticket_id, label in rows:
        builder.button(
            text=label,
            callback_data=f"support:view:{ticket_id}",
            style="primary",
        )
    builder.button(
        text=f"{ICON_BACK} بازگشت",
        callback_data="support:home",
        style="primary",
    )
    builder.adjust(1)
    return builder.as_markup()


def support_ticket_kb() -> InlineKeyboardMarkup:
    """Under a ticket transcript: follow up, or step back.

    «ادامهٔ گفتگو» is deliberately the same action as the home card's
    «نوشتن پیام» — a reply to an answered ticket is just the next message, and
    pretending otherwise would need a second, near-identical flow.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{ICON_REPLY} ادامهٔ گفتگو",
        callback_data="support:write",
        style="success",
    )
    builder.button(
        text=f"{ICON_HISTORY} پیگیری‌های من",
        callback_data="support:history",
        style="primary",
    )
    builder.button(
        text=f"{ICON_BACK} بازگشت",
        callback_data="support:home",
        style="primary",
    )
    builder.adjust(1)
    return builder.as_markup()
