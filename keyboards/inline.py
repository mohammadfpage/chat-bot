"""
Inline keyboards used for non-persistent prompts (force join, blocked list, inbox, etc.).

Two layers of emphasis, and they do different jobs:

  1. ``style="primary" | "success" | "danger"`` — the native Bot API 9.4 button
     colours. They only tint the button on clients that support them.
  2. The leading icon in the button LABEL — part of the text, so it survives
     every client, every theme and the plain-keyboard round trip an
     ``InlineQueryResultArticle`` markup has to make. This is the layer that
     always reads, so it carries the meaning.

The icon names the ACTION, never a colour. A bare coloured circle only says "this
button is green", which forces the reader to translate a hue into a verb; a real
icon says "delete", "stats", "reply" without that step. The set is therefore
small and semantic, and a constant is reused only where the verb actually
repeats:

    ✅ confirm / accept / go      ❌ decline / cancel
    👁️ show / read               🗑️ delete
    📊 stats                      ⚙️ options / settings
    ↩️ reply                      🔙 back / navigation
    🔗 link / connect             📢 join a channel
    📖 guide / tutorial           🎁 reward / claim
    🔄 refresh / re-check         ℹ️ information

A MENU is deliberately exempt: a list of four choices all wearing the same icon
tells the reader nothing, so ``main_menu_inline_kb`` keeps its own per-row icons.

``icon_custom_emoji_id`` is no longer used here. A premium custom-emoji icon only
renders on clients that can draw it and silently disappears for everyone else,
while a plain Unicode icon in the label renders everywhere. The two keys that
remain neutral states rather than choices — «خوانده شد» and «در انتظار پذیرش» —
take their emoji from :func:`utils.emojis.get_plain_emoji`.
"""

from collections.abc import Iterable
from html import escape

from aiogram.types import DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.emojis import get_plain_emoji


# ──────────────────────────────────────────────────
# Action icons
# ──────────────────────────────────────────────────
#
# Named after the ACTION they mark, never after a colour: the name is the
# documentation, and a hue name would tell a future reader nothing about which
# button the constant belongs on.

ICON_CONFIRM = "✅"    # confirm / accept / "I am ready" / acknowledge
ICON_DECLINE = "❌"    # decline / reject / cancel
ICON_DELETE = "🗑️"    # delete / take back / drop
ICON_SHOW = "👁️"      # show / read the content
ICON_STATS = "📊"      # statistics
ICON_OPTIONS = "⚙️"   # options / description of this card
ICON_REPLY = "↩️"      # reply
ICON_BACK = "🔙"       # back / return to the previous screen
ICON_LINK = "🔗"       # link / connect / deep-link that opens the bot
ICON_JOIN = "📢"       # join a channel
ICON_GUIDE = "📖"      # guide / tutorial
ICON_SEND = "📤"       # hand something to the bot to send
ICON_PHOTO = "🖼️"     # picture / tutorial screenshot
ICON_REFRESH = "🔄"    # refresh / run the check again
ICON_HISTORY = "📜"    # history / past movements
ICON_EXCHANGE = "🔁"   # convert one balance into another
ICON_INFO = "ℹ️"       # information, no action required
ICON_BLOCK = "🚫"      # block someone
ICON_UNBLOCK = "🔓"    # lift a block
ICON_CLAIM = "🎁"      # claim a reward
ICON_CHAT = "💬"       # open a conversation
ICON_CROWN = "👑"      # become / manage admins




# ──────────────────────────────────────────────────
# Callback / deep-link contract
# ──────────────────────────────────────────────────
#
# These two strings are baked into keyboards here and matched by handlers
# elsewhere, so the definition lives with the keyboards that publish them: that
# is the only place in the import graph that both sides can depend on without a
# cycle (handlers import ``keyboards``, never the other way round).
#
# ``handlers/inline_anon.py`` re-exports both names for the modules that already
# import them from there.

#: ``start_parameter`` behind the button above the inline results, and the
#: target of the tutorial button on the group onboarding card. Telegram accepts
#: 1-64 chars of A-Z a-z 0-9 _ - only, hence the undecorated value.
HELP_START_PARAM = "help"

#: Callback that expands the tutorial card into its written guide, used when no
#: ``TUTORIAL_PHOTO_URL`` is configured for the photo button.
TUTORIAL_PHOTO_CALLBACK = "inline_menu:tutorial"

#: ``callback_data`` prefix of the anonymous-chat request card's button. The
#: requester id follows it; whoever presses the button is the other side. This
#: is the ONLY thing that marks the card as publishable content rather than a
#: stub, so it is matched in two places (see ``handlers/inline_anon.py``) and
#: lives here for the same reason as the two constants above.
ANON_REQUEST_CB_PREFIX = "start_anon_chat:"

# ── Whisper action-card callbacks ──
#
# One card, three extra actions beside the read button, and one reply. The
# prefixes live here, with the buttons that publish them, and
# ``handlers/inline_anon.py`` imports them back rather than re-typing the
# strings — a callback prefix that exists in two places is a prefix that
# eventually stops matching.
#
# Every one of them is sized against the 64-byte ``callback_data`` ceiling with
# the 32-hex whisper id appended: ``whisper_options:`` is the longest at 16 + 32
# = 48 bytes.
WHISPER_STATS_CB_PREFIX = "whisper_stats:"
WHISPER_OPTIONS_CB_PREFIX = "whisper_options:"
WHISPER_DELETE_CB_PREFIX = "delete_whisper:"

#: The one action that is NOT a callback: «↩️ پاسخ» opens the picker itself, so it
#: carries an INLINE-QUERY prefix rather than ``callback_data``. It stays here
#: for the same reason as the three above — the button that publishes it and the
#: handler that reads it must not spell the string twice — but it is deliberately
#: not one of the callbacks the force-join middleware allow-lists.
#:
#: What travels is only the whisper id. Which side a reply has to be addressed
#: to depends on WHO pressed the button, and that is exactly what an inline
#: ``from_user`` can answer — so nothing else needs to be in the payload, and
#: the trailing space leaves the cursor ready for the first word of the answer.
WHISPER_REPLY_QUERY_PREFIX = "reply:"


def force_join_kb(chats: str | Iterable = "") -> InlineKeyboardMarkup:
    """
    Inline keyboard for the forced-join verification flow.

    Shown in the PRIVATE chat only. Accepts either a channel username or an
    iterable of ``utils.membership.ChatRef``, so a multi-channel configuration
    still gets one join button per channel.

    Shows: [📢 عضویت در کانال] ×N  →  [✅ تأیید عضویت]
    """
    builder = InlineKeyboardBuilder()

    if isinstance(chats, str):
        username = chats.strip().lstrip("@")
        entries = [username] if username else []
    else:
        entries = [
            (getattr(chat, "title", "") or "", getattr(chat, "url", "") or "")
            for chat in chats
            if getattr(chat, "url", None)
        ]

    for entry in entries:
        if isinstance(entry, str):
            title, url = "", entry
        else:
            title, url = entry
        if not url:
            continue
        label = f"{ICON_JOIN} عضویت در کانال{f' {escape(title)}' if title else ''}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                url=url if url.startswith("http") else f"https://t.me/{url.lstrip('@')}",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} تأیید عضویت",
            callback_data="check_membership",
        ),
    )
    return builder.as_markup()


def blocked_list_kb(blocked_entries: list[dict]) -> InlineKeyboardMarkup:
    """
    Inline keyboard showing blocked users with unblock buttons.

    Each row lifts one block, so it wears :data:`ICON_UNBLOCK` — the exact
    opposite of the :data:`ICON_BLOCK` on «بلاک فرستنده» in
    :func:`read_message_kb`, which is what makes the two rows on this screen
    read as a pair.

    Args:
        blocked_entries: list of dicts with keys:
            - "blocked_id": int (Telegram ID of the blocked user)
            - "label": str (display name; the caller never puts an id in it)
    """
    builder = InlineKeyboardBuilder()
    for entry in blocked_entries:
        uid = entry["blocked_id"]
        # The fallback must stay id-free: a keyboard label is on screen, and
        # the screen is where no numeric id may ever appear.
        label = entry.get("label") or "کاربر بلاک‌شده"
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_UNBLOCK} {label}",
                callback_data=f"unblock:{uid}",
                style="success",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_BACK} بازگشت",
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
        [🔙 بازگشت]   (back to main menu)
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
            text=f"{ICON_BACK} بازگشت",
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
            text=f"{ICON_BLOCK} بلاک فرستنده",
            callback_data=f"anon_block:{message_id}:{sender_id}",
            style="danger",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_BACK} بازگشت",
            callback_data="inbox:open",
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Group "whisper" (نجوا) — inline keyboards
# ──────────────────────────────────────────────────

def whisper_card_kb(whisper_id: int) -> InlineKeyboardMarkup:
    """
    The card published in a group after someone sends a whisper.

    The content itself is never here — only this button, and only the person
    it was addressed to may press it.

    Layout:
        [👁️ مشاهده پیام]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_SHOW} مشاهده پیام",
            callback_data=f"whisper:view:{whisper_id}",
            style="success",
        ),
    )
    return builder.as_markup()


def whisper_read_kb(token: str) -> InlineKeyboardMarkup:
    """
    The single button attached to an inline-mode whisper ANCHOR — and to every
    card that has not been opened by its recipient yet.

    Unlike :func:`whisper_card_kb` this keyboard is baked into the message by
    Telegram when the inline result is picked, so it must be built before the
    row exists — ``token`` is the (pre-minted) whisper id that later identifies
    it in the database.

    Deliberately free of ``style``/``icon_custom_emoji_id``: the markup travels
    through ``InlineQueryResultArticle``, where a plain Bot API keyboard is the
    only thing guaranteed to be accepted, and it stays that way while the card is
    still an unopened envelope — the bot re-keys it to
    :func:`get_whisper_action_keyboard` the moment the recipient presses it,
    which is also the first moment ``style`` is allowed on it.

    The label matches :func:`whisper_card_kb` on purpose — it is the same
    action on the same card, and two different names for it only made the
    group texts that mention the button ("کارت «...» در گروه منتشر شد") read as
    a feature that had gone missing.

    Layout:
        [👁️ مشاهده پیام]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_SHOW} مشاهده پیام",
            callback_data=f"read_whisper:{token}",
        ),
    )
    return builder.as_markup()


def get_whisper_action_keyboard(
    whisper_id: str, sender_id: int, target_id: int
) -> InlineKeyboardMarkup:
    """
    The full action keyboard of a whisper card.

    Replaces the single-button :func:`whisper_read_kb` once the RECIPIENT has
    opened the whisper. That is the whole gating rule and it lives with the
    handler, not here: until the target opens the card a whisper carries the
    read button alone, so this is never attached at publish time — see
    ``_card_keyboard`` in ``handlers/inline_anon.py``. In particular the sender
    has no «🗑️ حذف» on a card the recipient has not opened, which is the
    deliberate trade: four live-looking buttons in front of a whole group on an
    unclaimed card are worse than none.

    Layout:
        [📊 آمار]  [👁️ نمایش پیام]  [🗑️ حذف]
        [⚙️ گزینه‌ها]  [↩️ پاسخ]

    **Who may press what is decided in the handler, not here.** A card is one
    message rendered for everybody at once and Telegram freezes its markup the
    moment it is published, so a keyboard physically cannot hide a button from
    the readers it is not meant for. What it can do is keep every button
    pointing at a handler that checks who is asking — which is exactly what the
    three ``whisper_*``/``*_whisper`` handlers do. The reply button points at
    the inline handler instead, and is checked the same way, by
    ``on_inline_query`` in ``handlers/inline_anon.py``.

    ``sender_id`` / ``target_id`` are the two parties of this whisper. They are
    NOT baked into any button (the reply handler resolves the parties from the
    row itself, so a button can never disagree with the database), and they are
    used here for the one invariant a card cannot violate: the reply button needs
    an *opposite* side to talk to, and a card addressed from someone to
    themselves would put four buttons on a chat where every one of them
    dead-ends. Every entry point already refuses such a send; this is the guard
    that keeps a future caller from having to remember.

    Args:
        whisper_id: the 32-hex token, exactly as it appears in
            ``read_whisper:`` callbacks.
        sender_id:  who sent the whisper.
        target_id:  who it was addressed to.
    """
    if not whisper_id or sender_id == target_id:
        return whisper_read_kb(whisper_id)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_STATS} آمار",
            callback_data=f"{WHISPER_STATS_CB_PREFIX}{whisper_id}",
            style="primary",
        ),
        InlineKeyboardButton(
            text=f"{ICON_SHOW} نمایش پیام",
            callback_data=f"read_whisper:{whisper_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text=f"{ICON_DELETE} حذف",
            callback_data=f"{WHISPER_DELETE_CB_PREFIX}{whisper_id}",
            style="danger",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_OPTIONS} گزینه‌ها",
            callback_data=f"{WHISPER_OPTIONS_CB_PREFIX}{whisper_id}",
            style="primary",
        ),
        InlineKeyboardButton(
            text=f"{ICON_REPLY} پاسخ",
            # The ONE action that is not a callback. ``switch_inline_query_current_chat``
            # re-opens THIS bot's own picker in the same chat, with the whisper id
            # already typed, so the user's very next action is writing the answer.
            #
            # Only the id travels, and that is not a simplification — it is the
            # whole design. A callback could not work here: which id a reply has
            # to carry depends on WHO pressed it (the target answers the sender,
            # the sender answers the target), and the markup is published once
            # for everybody, so no single payload is right for both. Reading
            # ``inline_query.from_user`` sidesteps that entirely, and it also
            # removes the DM hop this used to need — the reply starts in the same
            # group the whisper was read in.
            switch_inline_query_current_chat=f"{WHISPER_REPLY_QUERY_PREFIX}{whisper_id} ",
            style="success",
        ),
    )
    return builder.as_markup()


def whisper_verify_kb(chats, whisper_id: str) -> InlineKeyboardMarkup:
    """The card keyboard shown while the recipient still misses channels.

    One URL button per channel they have NOT joined, then a single verify
    button that re-runs the check. The verify button deliberately carries the
    SAME ``callback_data`` as the default read button
    (:func:`whisper_read_kb`) — one handler serves both, so the card needs no
    second callback and the user's tap means the same thing on either state.

    Rewriting the card rather than posting a new message is deliberate: the
    prompt belongs to the card, and the card is the only handle the recipient
    has on this whisper.

    Channels with no resolvable link are skipped — a button that cannot be
    pressed is worse than no button.

    Layout:
        [📢 عضویت در …]      (one row per missing channel)
        [🔄 بررسی عضویت و مشاهده]
    """
    builder = InlineKeyboardBuilder()
    for chat in chats:
        url = chat.url if hasattr(chat, "url") else str(chat)
        if not url:
            continue
        title = getattr(chat, "title", "") or "کانال"
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_JOIN} عضویت در {escape(title)}",
                url=url,
                style="primary",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_REFRESH} بررسی عضویت و نمایش",
            callback_data=f"read_whisper:{whisper_id}",
            style="success",
        ),
    )
    return builder.as_markup()


def whisper_viewed_kb() -> InlineKeyboardMarkup:
    """
    Replacement for :func:`whisper_card_kb` once the whisper was opened.

    The button is kept (so the card keeps its original height) but disabled
    and greyed out — the content is delivered in the reader's private chat.
    This is the read receipt in button form: it pairs with the «خوانده شد» line
    the card text gains the moment the recipient opens the whisper.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{get_plain_emoji('verified')} خوانده شد",
            callback_data="whisper:noop",
            style="primary",
            disabled=DisabledButton(),
        ),
    )
    return builder.as_markup()


def whisper_join_kb(chats, check_callback: str = "whisper:check") -> InlineKeyboardMarkup:
    """
    Forced-join prompt for the whisper flow.

    Args:
        chats: iterable of ``utils.membership.ChatRef`` the user still misses.
        check_callback: callback fired by the "member now" button, so the
            handler knows the flow to resume.

    Layout:
        [📢 عضویت در <title>]   (one row per missing chat)
        [✅ عضو شدم]
    """
    builder = InlineKeyboardBuilder()
    for chat in chats:
        if not chat.url:
            continue
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_JOIN} عضویت در {chat.title}",
                url=chat.url,
                style="primary",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} عضو شدم",
            callback_data=check_callback,
            style="success",
        ),
    )
    return builder.as_markup()


def whisper_help_kb() -> InlineKeyboardMarkup:
    """
    Buttons offering the written whisper guide.

    Layout:
        [📖 راهنمای نجوا]
        [✅ متوجه شدم]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_GUIDE} راهنمای نجوا",
            callback_data="whisper:help",
            style="primary",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} متوجه شدم",
            callback_data="whisper:noop",
            style="success",
        ),
    )
    return builder.as_markup()


def whisper_commands_kb() -> InlineKeyboardMarkup:
    """
    Keyboard for the "you typed /" hint card posted in a group.

    ONE button only — a group card must stay as quiet as possible; the
    decorative "متوجه شدم" button that used to sit under it is gone.

    Layout:
        [📖 راهنمای نجوا]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_GUIDE} راهنمای نجوا",
            callback_data="whisper:help",
            style="primary",
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Inline menu — the 3 options of a bare "@bot" query
# ──────────────────────────────────────────────────
#
# The keyboards in this block are BAKED INTO an InlineQueryResultArticle, i.e.
# Telegram serialises them and republishes them in whatever chat the result is
# picked in. They therefore follow the same rule as :func:`whisper_read_kb` and
# stay free of ``style``/``icon_custom_emoji_id``/``disabled``: a plain Bot API
# keyboard is the only thing guaranteed to survive that round trip.
#
# Two of the three buttons are ``switch_inline_query_*`` — they do not send
# anything, they re-open THIS bot's own picker with the request already typed in.


def inline_tutorial_kb(
    first_name: str,
    recipient_ref: str,
    tutorial_photo_url: str = "",
) -> InlineKeyboardMarkup:
    """Buttons under the «آموزش ارسال نجوا» card.

    ``recipient_ref`` is how the reader refers to *themselves* in an inline
    query — their ``@username`` when they have one, otherwise their numeric id.
    It is baked into the second button's query, so tapping «ارسال به …» opens the
    picker with the whole pattern filled in, ready to type the secret into.

    The first button is a photo when one is configured and a callback when it
    is not: a button labelled «عکس آموزشی» that leads nowhere is the worst
    possible outcome for a brand-new user, so the fallback expands the card
    into a written guide instead.

    Layout:
        [🖼️ عکس آموزشی]
        [📤 ارسال به <first_name>]
    """
    builder = InlineKeyboardBuilder()

    photo = (tutorial_photo_url or "").strip()
    if photo:
        builder.row(InlineKeyboardButton(text=f"{ICON_PHOTO} عکس آموزشی", url=photo))
    else:
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_PHOTO} عکس آموزشی",
                callback_data=TUTORIAL_PHOTO_CALLBACK,
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_SEND} ارسال به {first_name}",
            switch_inline_query_current_chat=f" متن نجوا {recipient_ref}",
        ),
    )
    return builder.as_markup()


def inline_whisper_request_kb(prefill: str) -> InlineKeyboardMarkup:
    """The «📤 ارسال نجوا به من» button of the «درخواست نجوا» card.

    ``prefill`` is the query Telegram will have typed for the user when the
    button is pressed. It is built by the caller (which owns the query syntax)
    so this function never has to know how a whisper is addressed.

    Layout:
        [📤 ارسال نجوا به من]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_SEND} ارسال نجوا به من",
            switch_inline_query_current_chat=prefill,
        ),
    )
    return builder.as_markup()


def anon_chat_request_kb(requester_id: int) -> InlineKeyboardMarkup:
    """The button that turns an anonymous-chat card into a request.

    Only the *requester* id travels in the callback; the other side is whoever
    presses the button. That is exactly what the feature needs and it keeps the
    data tiny — a group can hold hundreds of these cards and every one of them
    stays well inside Telegram's 64-byte ``callback_data`` ceiling.

    Layout:
        [💬 شروع چت ناشناس]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CHAT} شروع چت ناشناس",
            callback_data=f"{ANON_REQUEST_CB_PREFIX}{requester_id}",
            style="success",
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Anonymous chat — the private side of the request
# ──────────────────────────────────────────────────
#
# From here on the keyboards are ordinary messages the bot posts itself, so the
# native ``style`` colours apply and the requester identity can be hidden behind
# a disabled button once it is answered.


def anon_chat_open_kb(deep_link: str) -> InlineKeyboardMarkup:
    """«Start me first» — sent to a user who has never pressed ``/start``.

    A bot may not open a conversation with someone who has not started it, so
    the only way through is Telegram's own deep link: press, ``/start`` happens
    automatically with our payload attached, and the request continues from the
    ``/start`` handler.

    Layout:
        [🔗 باز کردن ربات و ارسال درخواست]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_LINK} باز کردن ربات و ارسال درخواست",
            url=deep_link,
            style="primary",
        ),
    )
    return builder.as_markup()


def anon_chat_decision_kb(token: str) -> InlineKeyboardMarkup:
    """«قبول چت» / «رد» — the requester's decision, in their own DM.

    Only the requester ever sees this message, so the token is enough to find
    the row; no identity has to travel in the callback.

    Layout:
        [✅ قبول چت]
        [❌ رد]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} قبول چت",
            callback_data=f"anon_chat:accept:{token}",
            style="success",
        ),
        InlineKeyboardButton(
            text=f"{ICON_DECLINE} رد",
            callback_data=f"anon_chat:decline:{token}",
            style="danger",
        ),
    )
    return builder.as_markup()


def anon_chat_session_kb() -> InlineKeyboardMarkup:
    """The single button that hangs under every message of a live session.

    Kept as an INLINE button — not a reply keyboard — so it can be re-sent as
    the footer of every forwarded message. One tap from either side ends the
    chat for both.

    Label is identical to ``keyboards.reply.ANON_SESSION_END_LABEL`` on purpose:
    the inline and the reply-keyboard control end the same session, so they must
    read the same and be matched by the same strings.

    Layout:
        [❌ پایان چت ناشناس]
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_DECLINE} پایان چت ناشناس",
            callback_data="anon_chat:end",
            style="danger",
        ),
    )
    return builder.as_markup()


def anon_chat_waiting_kb(token: str) -> InlineKeyboardMarkup:
    """«Waiting for the other side» — greys out the request card in place.

    Rewriting the card is what stops a group full of people from hammering a
    button that has already been answered: the button stays visible (the card
    keeps its shape) but is disabled, so there is nothing left to press.

    Layout:
        [⏳ در انتظار پذیرش]   (greyed out)
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{get_plain_emoji('pending')} در انتظار پذیرش",
            callback_data=f"anon_chat:noop:{token}",
            disabled=DisabledButton(),
        ),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────
# Group onboarding — welcome card and admin-rights nudges
# ──────────────────────────────────────────────────


#: The rights request that rides along with the onboarding card. ``""`` means
#: the bot was already added with the rights it needs and there is nothing to
#: ask for.
ONBOARDING_PROMOTE = "promote"
ONBOARDING_ANONYMOUS = "anonymous"


#: The admin rights this bot asks for, as Telegram's ``admin`` deep-link flags.
#:
#: The separator is ``+``, NOT a comma: Telegram documents the value as "a
#: combination of the following identifiers separated by +", and a
#: comma-separated value is simply not recognised, which silently degrades the
#: link to a bare "add as admin" with no rights preselected. Every flag below is a
#: real one from that list — note ``restrict_members`` and ``manage_chat`` are the
#: link-level spellings of the ``ban_users`` and ``other`` rights.
#:
#: Kept in one constant because the two links that ask for these rights —
#: :func:`add_to_group_link` (private chat) and :func:`group_admin_link` (the
#: onboarding card in a group) — must not disagree. If they did, a group owner
#: who added the bot with the full set would later be shown a weaker request if
#: the bot were demoted and re-promoted. They now share one implementation, so
#: there is only one place to widen.
#:
#: Worth knowing before widening this: ``delete_messages`` and ``invite_users``
#: are powers this bot does not currently exercise — it deletes only its own
#: messages, which needs no right, and never builds invite links. ``restrict_members``
#: (banning) and ``manage_chat`` (the catch-all ``other`` right) are the two an
#: existing group is most likely to object to. Removing a flag here is a one-line
#: change that takes effect everywhere.
_ADMIN_RIGHTS: tuple[str, ...] = (
    "delete_messages",
    "restrict_members",
    "invite_users",
    "manage_chat",
)


def _rights_query() -> str:
    """The ``admin=<rights>`` fragment, ready to append to a link."""
    return "+".join(_ADMIN_RIGHTS)


def add_to_group_link(bot_username: str) -> str:
    """A link that adds the bot to a group *and* opens the admin-rights sheet.

    Two things happen on one tap: Telegram asks which group, then shows the
    admin toggle with :data:`_ADMIN_RIGHTS` already ticked, so the owner confirms
    a promotion instead of having to find the admin panel afterwards. Per
    Telegram's own behaviour the existing rights are **combined** with these
    rather than replaced, so re-using the link on a group that already has the bot
    only ever adds rights.

    No ``startgroup`` payload is sent. Telegram documents the bare
    ``?startgroup&admin=<rights>`` form, and a payload here would be delivered to
    our own ``/start`` handler as an argument for no benefit — ``startgroup=true``
    is the kind of thing that looks meaningful and reaches ``/start`` as the
    string ``"true"``.
    """
    clean = (bot_username or "").strip().lstrip("@")
    if not clean:
        return ""
    return f"https://t.me/{clean}?startgroup&admin={_rights_query()}"


def group_admin_link(bot_username: str) -> str:
    """A link that opens Telegram's "add bot as administrator" sheet for this bot.

    The closest thing to self-promotion the Bot API allows. ``promoteChatMember``
    requires the *caller* to already be an administrator, so a bot can never
    promote itself; but a deep link opens the admin-rights picker with this bot
    pre-selected, leaving only the final confirm to a human.

    Same URL as :func:`add_to_group_link`, deliberately: the ``?admin=`` parameter
    on its own is not a working link — Telegram only honours it when it rides
    along with ``startgroup``, and without that the button used to open the bot's
    profile instead of any rights sheet. Tapped from inside a group it still lands
    on the rights step with :data:`_ADMIN_RIGHTS` pre-ticked, which is all this
    card needs.

    Empty username means no link, and the caller falls back to a text-only
    notice.
    """
    return add_to_group_link(bot_username)


def group_promote_kb(bot_username: str) -> InlineKeyboardMarkup:
    """The "promote me" card posted in a group where the bot is a plain member.

    The button used to link to the group itself, which just dropped the admin back
    where they already were and left them to hunt for the admin panel by hand. It
    now links straight into the rights sheet via :func:`group_admin_link`.

    Layout:
        [👑 ارتقای ربات به ادمین]
        [✅ متوجه شدم]
    """
    builder = InlineKeyboardBuilder()
    link = group_admin_link(bot_username)
    if link:
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_CROWN} ارتقای ربات به ادمین",
                url=link,
                style="primary",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} متوجه شدم",
            callback_data="whisper:noop",
            style="success",
        ),
    )
    return builder.as_markup()


def group_hidden_admin_kb(group_url: str) -> InlineKeyboardMarkup:
    """The "turn on anonymous admin" nudge.

    Same shape as :func:`group_promote_kb` on purpose — a group that sees the
    same two-button card twice learns one layout instead of two.

    Layout:
        [⚙️ فعال‌سازی ادمین ناشناس]
        [✅ متوجه شدم]
    """
    builder = InlineKeyboardBuilder()
    if group_url:
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_OPTIONS} فعال‌سازی ادمین ناشناس",
                url=group_url,
                style="primary",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} متوجه شدم",
            callback_data="whisper:noop",
            style="success",
        ),
    )
    return builder.as_markup()


def group_onboarding_kb(
    bot_username: str, *, ask: str = "", chat_url: str = ""
) -> InlineKeyboardMarkup:
    """Buttons for the ONE card a group gets when the bot arrives.

    The welcome and the rights request used to be two separate messages, so
    every add produced a wall of two near-identical cards. This lays out the
    tutorial, the ask (when there is one) and the acknowledgement as rows of a
    single keyboard, which is what makes them a single card.

    Layout (``ask="promote"``):
        [📖 آموزش ارسال نجوا]
        [👑 ارتقای ربات به ادمین]
        [✅ متوجه شدم]
    """
    builder = InlineKeyboardBuilder()
    clean = (bot_username or "").strip().lstrip("@")
    if clean:
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_GUIDE} آموزش ارسال نجوا",
                url=f"https://t.me/{clean}?start={HELP_START_PARAM}",
                style="primary",
            ),
        )

    if ask == ONBOARDING_PROMOTE and clean:
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_CROWN} ارتقای ربات به ادمین",
                url=group_admin_link(clean),
                style="primary",
            ),
        )
    elif ask == ONBOARDING_ANONYMOUS:
        # Straight to the group's own page: the anonymous-admin toggle lives in
        # its settings, and there is no deep link into it.
        builder.row(
            InlineKeyboardButton(
                text=f"{ICON_OPTIONS} فعال‌سازی ادمین ناشناس",
                url=chat_url or group_admin_link(clean),
                style="primary",
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text=f"{ICON_CONFIRM} متوجه شدم",
            callback_data="whisper:noop",
            style="success",
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
