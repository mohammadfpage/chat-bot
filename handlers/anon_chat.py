"""
Real-time anonymous 1-on-1 secret chat.

Started from the «درخواست پیام ناشناس» card that the inline picker publishes
(:mod:`handlers.inline_menu`), and carried entirely in the database so a restart
cannot strand two people who believe they are still talking.

    A publishes the card in a group
      └─ B presses «💬 شروع چت ناشناس»
           ├─ B never started the bot → deep link, B presses it, /start carries
           │  the requester id and the flow resumes from here
           └─ B is a member          → A gets «قبول چت» / «رد» in their DM
                                          ├─ رد         → A's DM, B is told
                                          └─ قبول چت   → the pair goes live
                                                         (every PM message is
                                                          copied across, no
                                                          name attached)
                                                 «❌ پایان چت ناشناس» ends it
                                                         for both

Anonymity is structural, not cosmetic
-------------------------------------
Messages are relayed with ``copyMessage``, never ``forwardMessage``: a forward
carries a «Forwarded from …» header that would name the sender, which is the
one thing this feature promises not to do. ``copyMessage`` publishes the bot as
the author, so the receiver sees a message in their own chat with the bot and
cannot tell who typed it — and neither can anyone who later reads that chat's
history on the other phone.

Why the router is registered early
----------------------------------
This router matches *every* private message, so it has to be first among the
message handlers (right behind ``inline_anon_router``, which owns the message
Telegram posts from our own inline result): any state-gated catch-all below it —
a parked whisper body, a profile wizard — would otherwise swallow the message and
the conversation would stall with no visible error. That is also why the bail-out
below is not optional: ``/start``, ``/menu`` and every main-menu label have to
travel straight through to the routers that own them. It is the only router in
the project that answers by DELEGATING: when there is no live session it raises
:class:`SkipHandler` instead of returning, which is aiogram's supported way of
saying "not mine, keep looking".
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from keyboards import (
    ANON_REQUEST_CB_PREFIX,
    ANON_SESSION_END_LABEL,
    BACK_TO_MENU_TEXTS,
    anon_chat_decision_kb,
    anon_chat_open_kb,
    anon_chat_request_kb,
    anon_chat_session_kb,
    anon_chat_waiting_kb,
    main_menu_kb,
)
from keyboards.reply import anon_session_menu_kb
from utils import anon_sessions

logger = logging.getLogger(__name__)
router = Router()

#: Relaying is private-chat only. A group must never receive a copy of somebody
#: else's private message, and the reply keyboard below would be rewritten into
#: a removal by the keyboard guard anyway.
router.message.filter(F.chat.type == ChatType.PRIVATE)

# ──────────────────────────────────────────────────
# Callback-data contract
# ──────────────────────────────────────────────────

_ACCEPT_PREFIX = "anon_chat:accept:"
_DECLINE_PREFIX = "anon_chat:decline:"
_END_CB = "anon_chat:end"
_NOOP_PREFIX = "anon_chat:noop"

#: ``start_parameter`` of the deep link handed to a user who has not started the
#: bot. Telegram allows ``A-Za-z0-9_-`` only, so the separator is ``_``.
#: ``handlers/start.py`` matches this exact prefix to resume the request.
ANON_DEEP_PARAM = "anon_chat_"

#: Content types ``copyMessage`` can carry across. Anything else is answered
#: with an explanation instead of failing mid-copy, so the user learns why their
#: file did not arrive rather than watching it vanish.
_RELAYABLE = frozenset(
    {
        "text",
        "photo",
        "voice",
        "video",
        "video_note",
        "audio",
        "document",
        "sticker",
        "animation",
        "contact",
        "location",
        "venue",
        "poll",
        "dice",
    }
)

#: Telegram's own service notices. They are never relayed and never deserve an
#: "unsupported type" reply either — a user who just joined a group must not be
#: told their chat is broken. Anything else that is not in :data:`_RELAYABLE`
#: DOES get the explanation, because that is a real thing the user tried to send.
_SERVICE = frozenset(
    {
        "new_chat_members",
        "left_chat_member",
        "pinned_message",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "video_chat_started",
        "video_chat_ended",
        "web_app_data",
    }
)


# ──────────────────────────────────────────────────
# Texts
# ──────────────────────────────────────────────────

_REQUEST_ACCEPTED_DM = (
    "💬 <b>یک نفر درخواست چت ناشناس شما را پذیرفت!</b>\n\n"
    "آیا مایل به شروع گفتگو هستید؟\n\n"
    "🔒 تا وقتی «قبول چت» را نزنید، طرف مقابل چیزی از شما نمی‌بیند."
)

_REQUEST_SENT_DM = (
    "⏳ <b>درخواست شما ثبت شد.</b>\n\n"
    "به‌محض اینکه طرف مقابل آن را بپذیرد، چت ناشناس بین شما دو فعال می‌شود "
    "و از همین‌جا پیام‌ها رد و بدل می‌شود.\n\n"
    "🔒 تا آن لحظه هویت شما نزد او نمایش داده نمی‌شود."
)

_CONNECTED_TEXT = (
    "✨ <b>چت ناشناس متصل شد!</b>\n\n"
    "اکنون هر پیامی بفرستید به صورت ناشناس برای طرف مقابل ارسال می‌شود.\n\n"
    "🔒 نه نام شما و نه نام طرف مقابل روی پیام‌ها نمی‌آید.\n"
    "برای پایان گفتگو از دکمهٔ «❌ پایان چت ناشناس» استفاده کنید."
)

_DECLINED_TO_PRESSER = (
    "🚫 <b>درخواست رد شد.</b>\n\n"
    "شما درخواست چت ناشناس را نپذیرفتید. اگر خواستید، می‌توانید دوباره یک "
    "کارت «درخواست پیام ناشناس» منتشر کنید."
)

_DECLINED_TO_ACCEPTED = (
    "🚫 <b>درخواست شما پذیرفته نشد.</b>\n\n"
    "طرف مقابل گفتگو را نپذیرفت. می‌توانید دوباره درخواست بدهید."
)

_ENDED_TEXT = "❌ <b>چت ناشناس پایان یافت.</b>\n\nپیام‌های بعدی دیگر ارسال نمی‌شوند."

_UNSUPPORTED_TEXT = (
    "⚠️ این نوع پیام قابل ارسال نیست.\n\n"
    "متن، عکس، ویس، ویدیو، فایل و استیکر پشتیبانی می‌شوند."
)

#: Group card rewritten once the request has been answered, so a second person
#: pressing the button sees that it is already on its way.
_CARD_PENDING_TEXT = (
    "⏳ <b>درخواست شما ارسال شد.</b>\n\n"
    "منتظر پذیرش طرف مقابل بمانید؛ به‌محض پذیرش، چت ناشناس فعال می‌شود."
)


# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

async def _dm(bot: Bot, user_id: int, text: str, kb=None) -> bool:
    """Send one private message. False when the user cannot receive it.

    A bot may not message someone who never started it and gets an error for
    someone who blocked it. Both are routine here (the whole feature is built
    around strangers), so every DM is best-effort and the caller decides what to
    do about a ``False``.
    """
    try:
        await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        return True
    except Exception as exc:
        logger.info("Anon-chat DM to %s failed: %s", user_id, exc)
        return False


async def _edit(callback: CallbackQuery, text: str, kb=None) -> None:
    """Rewrite the pressed card. Falls back to a new message when it is gone.

    ``callback.message`` is ``None`` for presses on cards older than a few
    minutes, and the edit fails for a message the bot cannot touch — in a group
    it was not an admin of, for instance. Neither may raise: the press has
    already been recorded and must not be lost to a UI failure.
    """
    message = callback.message
    if message is None:
        return
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        logger.debug("Could not rewrite the anon-chat card: %s", exc)


def deep_link(bot_username: str, requester_id: int) -> str:
    """``https://t.me/<bot>?start=anon_chat_<id>`` — the only way into a DM
    with a user who has never pressed ``/start``."""
    clean = (bot_username or "").strip().lstrip("@")
    return f"https://t.me/{clean}?start={ANON_DEEP_PARAM}{requester_id}"


async def _relay(bot: Bot, message: Message, partner_id: int) -> None:
    """Copy ``message`` to ``partner_id`` without a trace of its author.

    ``copy_message`` (not ``forward_message``) is the whole anonymity guarantee:
    a forward is stamped with its origin, a copy is not. The copy is stamped as
    sent by the bot, which is exactly what the receiver already sees in every
    other message of this conversation.
    """
    await bot.copy_message(
        chat_id=partner_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )


# ──────────────────────────────────────────────────
# 1. The card is pressed
# ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith(ANON_REQUEST_CB_PREFIX))
async def cb_start_anon_chat(callback: CallbackQuery, bot: Bot) -> None:
    """«💬 شروع چت ناشناس» — User B volunteers for User A's request."""
    raw = (callback.data or "")[len(ANON_REQUEST_CB_PREFIX) :]
    if not raw.isdigit():
        await callback.answer("⚠️ این کارت معتبر نیست.", show_alert=True)
        return

    requester_id = int(raw)
    accepter_id = callback.from_user.id

    if requester_id == accepter_id:
        await callback.answer(
            "🙃 نمی‌توانید به خودتان درخواست بدهید.", show_alert=True
        )
        return

    # The requester must be reachable: without a DM there is nobody to ask.
    if not await anon_sessions.has_started(requester_id):
        await callback.answer(
            "🚫 صاحب این کارت هنوز ربات را استارت نکرده و نمی‌توان به او "
            "پیام داد.",
            show_alert=True,
        )
        return

    if not await anon_sessions.has_started(accepter_id):
        # A bot cannot open the conversation itself, so hand out Telegram's own
        # deep link: pressing it runs /start for them with our payload attached,
        # and the flow continues in ``handle_anon_chat_deep_link``.
        me = await bot.me()
        await callback.answer(
            "برای شروع چت ناشناس ابتدا ربات را باز کنید 👇",
            show_alert=True,
        )
        # answerCallbackQuery cannot carry a keyboard — the deep-link button
        # goes onto the card itself, under the original «شروع» row, which the
        # rest of the group must keep.
        try:
            card_msg = callback.message
            if isinstance(card_msg, Message):
                base = anon_chat_request_kb(requester_id)
                opener = anon_chat_open_kb(deep_link(me.username, requester_id))
                await card_msg.edit_reply_markup(
                    InlineKeyboardMarkup(
                        inline_keyboard=[
                            *base.inline_keyboard,
                            *opener.inline_keyboard,
                        ]
                    )
                )
        except Exception as exc:
            logger.debug("Could not attach deep-link button: %s", exc)
        return

    if await anon_sessions.active_partner(accepter_id):
        await callback.answer(
            "💬 شما هم‌اکنون در یک چت ناشناس فعال هستید.", show_alert=True
        )
        return

    # The publisher has to be free too, not just the person pressing the button:
    # the «قبول چت» card is sitting in THEIR DM, so opening a request for a
    # publisher who is already mid-chat would leave them a choice between two
    # conversations and one working ``active_session`` lookup.
    if await anon_sessions.active_partner(requester_id):
        await callback.answer(
            "💬 صاحب این کارت هم‌اکنون در چت ناشناس دیگری است.",
            show_alert=True,
        )
        return

    row = await anon_sessions.open_request(requester_id, accepter_id)

    if row.status == anon_sessions.STATUS_ACTIVE:
        # The card was pressed again after the request was accepted — the chat
        # is already up, so point the user at it instead of asking twice.
        await callback.answer("✨ چت ناشناس شما از قبل فعال است.", show_alert=True)
        return

    sent = await _dm(
        bot,
        requester_id,
        _REQUEST_ACCEPTED_DM,
        anon_chat_decision_kb(row.token),
    )
    if not sent:
        await callback.answer(
            "🚫 ارسال درخواست ممکن نشد؛ شاید طرف مقابل ربات را بلاک کرده باشد.",
            show_alert=True,
        )
        return

    await _dm(bot, accepter_id, _REQUEST_SENT_DM, main_menu_kb())
    await _edit(callback, _CARD_PENDING_TEXT, anon_chat_waiting_kb(row.token))
    await callback.answer("✅ درخواست ارسال شد.", show_alert=True)


async def handle_anon_chat_deep_link(
    message: Message, requester_id: int
) -> bool:
    """Resume the request after ``/start anon_chat_<id>``.

    Reached only from :func:`handlers.start.cmd_start`, i.e. at the exact moment
    Telegram has just opened the private chat — which is what makes writing to
    this user legal for the first time. Returns ``True`` when the request went
    through and the caller should stop handling ``/start``.
    """
    bot = message.bot
    accepter_id = message.from_user.id

    if requester_id == accepter_id:
        return False

    if not await anon_sessions.has_started(requester_id):
        await message.answer(
            "🚫 صاحب این کارت هنوز ربات را استارت نکرده و نمی‌توان به او پیام "
            "داد.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return True

    busy = await anon_sessions.live_partners(requester_id, accepter_id)
    if accepter_id in busy:
        await message.answer(
            "💬 شما هم‌اکنون در یک چت ناشناس فعال هستید.\n\n"
            "برای شروع چت جدید، اول با «❌ پایان چت ناشناس» آن را ببندید.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return True
    if requester_id in busy:
        await message.answer(
            "💬 صاحب این کارت هم‌اکنون در چت ناشناس دیگری است.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return True

    row = await anon_sessions.open_request(requester_id, accepter_id)

    if row.status == anon_sessions.STATUS_ACTIVE:
        await _open_session(bot, accepter_id, row)
        return True

    if await _dm(
        bot, requester_id, _REQUEST_ACCEPTED_DM, anon_chat_decision_kb(row.token)
    ):
        await message.answer(
            _REQUEST_SENT_DM,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return True

    await message.answer(
        "🚫 ارسال درخواست ممکن نشد؛ شاید طرف مقابل ربات را بلاک کرده باشد.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    return True


# ──────────────────────────────────────────────────
# 2. The requester's decision
# ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith(_ACCEPT_PREFIX))
async def cb_anon_chat_accept(callback: CallbackQuery, bot: Bot) -> None:
    """«✅ قبول چت» — the pair goes live."""
    token = (callback.data or "")[len(_ACCEPT_PREFIX) :]
    row = await anon_sessions.by_token(token)

    if row is None or callback.from_user.id != row.requester_id:
        # The button is only ever rendered in the requester's own DM, so a
        # mismatch means the data was edited or the row never existed.
        await callback.answer("⚠️ این درخواست دیگر معتبر نیست.", show_alert=True)
        return

    if row.status != anon_sessions.STATUS_PENDING:
        await callback.answer(
            "ℹ️ این درخواست قبلاً بررسی شده است.", show_alert=True
        )
        return

    if not await anon_sessions.activate(row):
        await callback.answer("ℹ️ این درخواست قبلاً بسته شده است.", show_alert=True)
        return

    await _open_session(bot, row.partner_id, row)
    await _edit(callback, _CONNECTED_TEXT, None)
    await callback.answer("✨ چت ناشناس فعال شد.", show_alert=True)


@router.callback_query(F.data.startswith(_DECLINE_PREFIX))
async def cb_anon_chat_decline(callback: CallbackQuery, bot: Bot) -> None:
    """«❌ رد» — the requester says no."""
    token = (callback.data or "")[len(_DECLINE_PREFIX) :]
    row = await anon_sessions.by_token(token)

    if row is None or callback.from_user.id != row.requester_id:
        await callback.answer("⚠️ این درخواست دیگر معتبر نیست.", show_alert=True)
        return

    if not await anon_sessions.close(row, anon_sessions.STATUS_DECLINED):
        await callback.answer("ℹ️ این درخواست قبلاً بسته شده است.", show_alert=True)
        return

    await _dm(bot, row.partner_id, _DECLINED_TO_ACCEPTED, main_menu_kb())
    await _edit(callback, _DECLINED_TO_PRESSER, None)
    await callback.answer("🚫 درخواست رد شد.", show_alert=True)


@router.callback_query(F.data.startswith(_NOOP_PREFIX))
async def cb_anon_chat_noop(callback: CallbackQuery) -> None:
    """Absorbs the greyed-out «در انتظار پذیرش» button on an answered card."""
    await callback.answer("ℹ️ درخواست شما قبلاً ارسال شده است.")


async def _open_session(bot: Bot, user_id: int, row) -> None:
    """Announce a live chat to one side.

    The other side already has its copy of :data:`_CONNECTED_TEXT` — the one
    written over the «قبول چت» card — so only the freshly-woken user needs the
    session keyboard here.
    """
    await _dm(bot, user_id, _CONNECTED_TEXT, anon_chat_session_kb())
    try:
        await bot.send_message(
            user_id,
            "👇 هر چیزی می‌نویسید ناشناس به طرف مقابل می‌رسد.",
            parse_mode="HTML",
            reply_markup=anon_session_menu_kb(),
        )
    except Exception as exc:
        logger.info("Could not post the session keyboard to %s: %s", user_id, exc)


# ──────────────────────────────────────────────────
# 3. Relaying — only while a session is live
# ──────────────────────────────────────────────────

@router.message(F.text == ANON_SESSION_END_LABEL)
async def on_end_label(message: Message, bot: Bot) -> None:
    """The persistent «❌ پایان چت ناشناس» keyboard button.

    Matched as plain text, not as a button: a ``ReplyKeyboardMarkup`` label
    arrives as an ordinary message, and the user must be able to end the chat
    with the keyboard alone. Routing it through the same code path as the inline
    button keeps the "who gets told what" logic in one place.
    """
    user = message.from_user
    if user is None:
        return
    await _end_session(bot, user.id, message)


@router.message()
async def relay_to_partner(message: Message, bot: Bot) -> None:
    """Copy everything a live session's user sends to the other side.

    Registered as a bare ``@router.message()`` with no content-type filter, so
    it sees EVERY private message and can decide. That is why it must delegate
    rather than consume: with no live session it raises :class:`SkipHandler` and
    the rest of the bot gets its turn. The alternative — filtering on
    "has a session" in the router — would mean a database round-trip in the
    filter itself, where a failure has nowhere to go.
    """
    user = message.from_user
    if user is None or user.is_bot:
        # Nothing sensible to relay from the bot itself or from an update with
        # no sender, and answering one would loop.
        raise SkipHandler

    user_id = user.id
    row = await anon_sessions.active_session(user_id)
    if row is None:
        raise SkipHandler

    # Any command and the main-menu label are the user's escape hatch: they
    # belong to the routers below, and swallowing somebody's /menu because they
    # wanted to see the menu would make the session a trap. Checking the slash
    # rather than listing command names also covers ``/start@bot <payload>``,
    # which is how a deep link arrives.
    if message.text and (
        message.text.startswith("/") or message.text in BACK_TO_MENU_TEXTS
    ):
        raise SkipHandler

    content_type = message.content_type

    if content_type in _SERVICE:
        raise SkipHandler
    if content_type not in _RELAYABLE:
        await message.answer(_UNSUPPORTED_TEXT, parse_mode="HTML")
        return

    partner_id = anon_sessions.partner_of(row, user_id)

    try:
        await _relay(bot, message, partner_id)
    except Exception as exc:
        logger.warning("Could not relay %s from %s: %s", content_type, user_id, exc)
        await message.answer(
            "⚠️ ارسال این پیام ممکن نشد؛ شاید طرف مقابل ربات را بلاک کرده باشد.\n"
            "با «❌ پایان چت ناشناس» می‌توانید گفتگو را ببندید.",
            parse_mode="HTML",
        )
        return

    # The copy landed, so the other side is demonstrably still here. Nothing is
    # recorded here on purpose: the 24-hour cap is measured from the moment the
    # session became active and must NOT slide, or a busy pair would talk
    # forever. Expiry is anchored in ``anon_sessions._is_stale``.


# ──────────────────────────────────────────────────
# 4. Termination
# ──────────────────────────────────────────────────

@router.callback_query(F.data == _END_CB)
async def cb_anon_chat_end(callback: CallbackQuery, bot: Bot) -> None:
    """«❌ پایان چت ناشناس» — the inline button on any session message."""
    user_id = callback.from_user.id
    partner_id = await anon_sessions.end_for_user(user_id)

    if partner_id is None:
        await callback.answer("ℹ️ چت فعالی برای شما وجود ندارد.", show_alert=True)
        return

    # Both sides are handed the main menu rather than the session keyboard: the
    # session is over, and leaving «پایان چت ناشناس» on a dead chat would only
    # invite the user to press a button that does nothing.
    await _dm(bot, partner_id, _ENDED_TEXT, main_menu_kb())
    await _edit(callback, _ENDED_TEXT, main_menu_kb())
    try:
        await bot.send_message(
            user_id,
            _ENDED_TEXT,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        pass
    await callback.answer("❌ چت ناشناس پایان یافت.", show_alert=True)


async def _end_session(bot: Bot, user_id: int, message: Message) -> None:
    """Shared ending for the keyboard button and the inline one."""
    partner_id = await anon_sessions.end_for_user(user_id)

    if partner_id is None:
        await message.answer(
            "ℹ️ چت فعالی برای شما وجود ندارد.",
            reply_markup=main_menu_kb(),
        )
        return

    await _dm(bot, partner_id, _ENDED_TEXT, main_menu_kb())
    await message.answer(_ENDED_TEXT, parse_mode="HTML", reply_markup=main_menu_kb())


__all__ = [
    "ANON_DEEP_PARAM",
    "deep_link",
    "handle_anon_chat_deep_link",
    "router",
]