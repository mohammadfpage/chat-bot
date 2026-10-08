"""
Group "whisper" system (نجوا) — private messages inside a public group.

Flow:
  1. The bot is added to a group and posts exactly ONE card there — the
     welcome plus the request for admin rights (see
     ``handlers.group_lifecycle``). That card is the whole discovery path:
     afterwards the group hears nothing at all, not from ``/``, not from
     ``/start``, ``/help`` or ``/menu``, and not from an unknown command.
  2. A member replies to somebody's message (or targets @username) and sends
     ``/نجوا متن``. The bot NEVER deletes that command — it stays in the chat
     exactly as the member sent it.
   3. The bot publishes a card in the group that MENTIONS the receiver and
      carries a single "👁️ مشاهده پیام" button. The text itself is
      never posted in the group.
   4. The receiver — and only the receiver — may press the button. The content
      is delivered in their private chat and the group card is switched to a
      disabled "خوانده شد" button, and on the first read it gains a
      «✅ این نجوا توسط گیرنده خوانده شد.» line so the group
      can see the message was collected. Everyone else is rejected.

  4b. The private notice that opens the flow
      ------------------------------------------------
      Before the card goes up, the receiver is DMed a durable
      «👁️ یک پیام مخفی دارید» alert carrying the read button, and its message
      id is stored on the row (``whispers.notice_message_id``). That notice is
      the only permanent trace of the whisper in the receiver's own chat, so on
      the first read it is EDITED into «✅ پیام خوانده شد» — one receipt for the
      group (on the card) and one for the person who was waiting (in their DM),
      both driven off the same committed ``is_viewed`` flip so neither can be
      claimed twice or claimed by the sender.

      A bot may not write to somebody who never started it, and that is an
      ordinary outcome here, not an error: the row stays live, the group card
      still works, and the SENDER is told the recipient has to start the bot
      (:func:`handlers.inline_anon.target_not_started_text`) instead of being
      handed a failure for a whisper that is in fact delivered. The same notice
      and the same receipt are shared with the inline picker — see
      ``handlers.inline_anon.whisper_notice_text``.

Membership is enforced at every step: the sender must still be inside the
group, the receiver must still be inside the group, and both must be inside
whatever channel/group the admin configured as a forced-join requirement (the
global ``.env`` channel is merged in by ``utils.membership``).

The pending "finish joining first" payload is parked under the *private-chat*
FSM key, because that is where the "عضو شدم" button is pressed — a state set
inside a group is keyed by the group and would be invisible in the DM.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from database import async_session_factory, BlockList, Whisper
from handlers.inline_anon import (
    _NOTICE_READ_TEXT,
    target_not_started_text,
    whisper_notice_text,
)
from keyboards import (
    INLINE_HELP_LABEL,
    NEXT_CHAT_LABEL,
    REMATCH_LABEL,
    main_menu_kb,
    whisper_card_kb,
    whisper_join_kb,
    whisper_viewed_kb,
)
from states import WhisperStates
from utils.economy import (
    authorize_chat_message,
    can_afford_whisper,
    check_and_deduct_balance,
    get_policy,
    no_balance_text,
    rate_limit_text,
    refund_balance,
    should_warn,
)
from utils.membership import (
    MEMBER_STATUSES,
    clear_membership_cache,
    configured_channels,
    get_chat_member_status,
    is_member,
    join_bullet,
    missing_required_chats,
)
from utils.target_lookup import (
    MISS_UNKNOWN,
    UNKNOWN_HINT,
    VERDICT_BAD_ID,
    VERDICT_NOT_HERE,
    has_started_bot,
    recipient_verdict,
    resolve_target,
)
from utils.whisper_config import get_whisper_config

logger = logging.getLogger(__name__)
router = Router()

#: Aliases accepted for the whisper command — short ones included, because
#: typing a long Persian word is exactly where people give up.
WHISPER_COMMANDS = ("w", "nj", "whisper", "نجوا", "نژوا", "نجا")

_JOIN_PROMPT = (
    "🔐 <b>عضویت اجباری</b>\n\n"
    "برای استفاده از قابلیت «نجوا» ابتدا باید در این موارد عضو باشید:\n"
    "{lines}\n\n"
    "پس از عضویت روی «عضو شدم» بزنید تا درخواست شما خودکار ادامه پیدا کند."
)

#: Shown when the button is real but its row is gone. Deliberately does NOT
#: claim a read-timer ran out: no such timer exists in this bot, so the old
#: wording told the reader a lie and sent them looking for a setting that is
#: not there. The truth is simpler — the card outlived a send that never
#: completed — and the only way forward is to ask the sender again.
_WHISPER_MISSING = (
    "⚠️ <b>این پیام ثبت نشده است.</b>\n\n"
    "نجوا زمان‌دار نیست و منقضی نمی‌شود؛ این دکمه روی کارتی مانده که ربات "
    "نتوانسته آن را کامل ثبت کند.\n\n"
    "لطفاً از فرستنده بخواهید پیام را دوباره بفرستد."
)

#: The read receipt. One shared wording for both whisper roads — the inline
#: picker and the ``/نجوا`` command — because a group can hold cards from either
#: and two differently-worded receipts on the same screen would look like two
#: different features. The inline side owns the definition; see
#: ``handlers.inline_anon._READ_RECEIPT_LINE``.
_READ_RECEIPT_LINE = "✅ این نجوا توسط گیرنده خوانده شد."

# The recipient's private notice and its «خوانده شد» receipt are imported from
# the inline side rather than written out longhand here, because they used to be
# pasted and drifted, leaving the two roads explaining the same alert differently
# to the same person. One definition, imported, cannot drift.
#
# ``_NOTICE_READ_TEXT`` is deliberately NOT re-aliased onto itself the way it once
# was: a module-level ``x = x`` shadowing an import is indistinguishable from a
# real binding to a reader and to a linter, and it made the import look owned
# here. The names are simply used as imported.

#: Telegram's error for an edit that would change nothing. Matched against the
#: exception text because it is a localised string, not a stable error code.
_NOT_MODIFIED = "message is not modified"

# ──────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────

def _dm_key(bot_id: int, user_id: int) -> StorageKey:
    """Private-chat FSM key — the repo convention for reaching a user in DM."""
    return StorageKey(bot_id=bot_id, user_id=user_id, chat_id=user_id)


def mention_html(_user_id: int, name: str, username: str = "") -> str:
    """Mention a user with no numeric id anywhere in the markup.

    The old body linked ``tg://user?id=…`` — copyable from the rendered card,
    which is exactly the identifier this bot's anonymity promise hides. A
    public handle now links to its ``t.me`` page (no id exists in that URL),
    and a name without one stays plain text: unclickable, but anonymous.
    ``_user_id`` is kept in the signature so the call site still reads as a
    mention of a specific person.
    """
    if username:
        handle = escape(username)
        return f'<a href="https://t.me/{handle}">@{handle}</a>'
    return escape(name or "کاربر")


async def _send_dm(bot, user_id: int, text: str, kb=None) -> bool:
    """Send a private message. Returns False when the user blocked the bot."""
    return await _dm_message(bot, user_id, text, kb) is not None


async def _dm_message(bot, user_id: int, text: str, kb=None) -> Message | None:
    """Send one private message and hand the message back, or ``None``.

    :func:`_send_dm` reports only success, which is all a caller wants when the
    content is a status line. The whisper notice needs the message id instead: it
    is the one permanent artefact in the recipient's chat, and it has to be
    EDITED into «✅ پیام خوانده شد» the moment they open the whisper — otherwise
    their chat keeps telling them they have an unread message for ever. Hence a
    second entry point rather than a changed return type under every caller.
    """
    try:
        return await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception as exc:
        logger.info("Whisper DM to %s failed: %s", user_id, exc)
        return None


async def _attach_notice(whisper_id: int, message_id: int | None) -> None:
    """Record the notice message so the first read can mark it as read.

    Best effort on purpose: the id only powers the cosmetic receipt, so losing
    it must never be able to fail a send whose row is already committed.
    """
    if not message_id:
        return
    try:
        async with async_session_factory() as session:
            row = await session.get(Whisper, whisper_id)
            if row is not None:
                row.notice_message_id = message_id
                await session.commit()
    except Exception as exc:
        logger.debug("Could not record whisper notice %s: %s", whisper_id, exc)


async def _mark_notice_read(bot, user_id: int, message_id: int | None) -> None:
    """Turn the recipient's notice into its «پیام خوانده شد» receipt.

    Silent by design. The secret has already been delivered by the time this
    runs, so a failure here leaves an out-of-date line in a chat — a cosmetic
    wart, not a lost message — and announcing it would only add a second, more
    confusing message to the very chat that is meant to be tidying itself up.
    """
    if not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=message_id,
            text=_NOTICE_READ_TEXT,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as exc:
        logger.info("Could not mark whisper notice %s read: %s", message_id, exc)


async def _notify(
    bot,
    message: Message,
    user_id: int,
    text: str,
    kb=None,
    group_note: str = "",
) -> bool:
    """Tell the user about the result of their command.

    Goes to the private chat first. That DM is the only place the details can
    live (a failure reason must never end up in the group as a reply to the
    command, or it would point at the sender). If the DM is impossible — the
    user never started the bot, or blocked it — we fall back to a short,
    content-free note in the group so the command is not silently swallowed.

    Args:
        message: the command message, used only for the group fallback.
        group_note: what to post in the group when the DM fails. Empty means
            fall back to a generic "start the bot" hint.
    """
    if await _send_dm(bot, user_id, text, kb):
        return True

    note = group_note or (
        "ℹ️ برای دریافت نتیجه، لطفاً یک‌بار ربات را <b>استارت</b> کنید."
    )
    try:
        await message.answer(note, parse_mode="HTML")
    except Exception as exc:
        logger.debug("Group fallback notice failed: %s", exc)
    return False


async def _edit_or_send(callback: CallbackQuery, text: str, kb) -> None:
    """Edit the callback's message, falling back to sending a new one."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


async def _post_card(bot, chat_id: int, text: str, kb) -> int | None:
    """Publish the group card, retrying once. ``None`` when it never went out.

    A single attempt is not enough: right after the bot is added to a group
    Telegram regularly answers the first ``sendMessage`` with a retryable
    error, and flood control does the same when several members send a whisper
    at once. Both clear by themselves in a second, so the card is simply tried
    again rather than being lost.
    """
    for attempt in (1, 2):
        try:
            card = await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return card.message_id
        except Exception as exc:
            logger.warning(
                "Could not post whisper card in %s (attempt %d): %s",
                chat_id,
                attempt,
                exc,
            )
            if attempt == 1:
                await asyncio.sleep(1.2)
    return None


async def _are_blocked(a: int, b: int) -> bool:
    """True when either side blocked the other."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList)
            .where(
                ((BlockList.blocker_id == a) & (BlockList.blocked_id == b))
                | ((BlockList.blocker_id == b) & (BlockList.blocked_id == a))
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


def _join_prompt_text(missing) -> str:
    lines = "\n".join(join_bullet(chat) for chat in missing)
    return _JOIN_PROMPT.format(lines=lines)


def _whisper_card_text(
    mention: str, chat_title: str, *, reachable: bool = True
) -> str:
    """The message everyone in the group sees — it holds no whisper content.

    Deliberately short: a group is not the place for a manual.

    Args:
        reachable: False when the receiver has never opened a chat with the
            bot, which means their "view" button will not work for them yet —
            so the card spells out the one step that fixes it.
    """
    head = (
        "🔇 <b>یک پیام ناشناس برای شماست</b>\n\n"
        f"{mention} عزیز، در «{escape(chat_title)}» برای شما نجوا نوشته‌اند.\n\n"
    )
    if not reachable:
        # With no private notice on the happy path, the card is the ONLY place
        # an instruction can live, so both branches have to carry one. This one
        # names the fix rather than the feature, because the button the reader
        # is being invited to press is the thing that will not work yet.
        return head + (
            "📢 اول ربات را استارت کنید، بعد دکمهٔ «مشاهده پیام» را بزنید.\n\n"
            "🔒 متن فقط شما می‌بینید و فرستنده ناشناس می‌ماند."
        )
    return head + (
        "👁️ روی «مشاهده پیام» بزنید؛ متن در چت خصوصی خودتان باز می‌شود.\n\n"
        "🔒 فقط شما متن را می‌بینید و فرستنده برای همیشه ناشناس می‌ماند."
    )


def _usage_text() -> str:
    """The whisper guide — short enough to read at a glance."""
    return (
        "🔇 <b>راهنمای نجوا</b>\n\n"
        "روی پیام طرف <b>ریپلای</b> کنید و بنویسید:\n"
        "<code>/نجوا متن پیام</code>\n\n"
        "یا: <code>/نجوا @یوزرنیم متن</code>\n"
        "میان‌بر: <code>/w</code> · <code>/nj</code>\n\n"
        "پیام شما پاک می‌شود، فرستنده ناشناس می‌ماند و متن را فقط گیرنده "
        "می‌بیند. (متن، عکس و ویس مجاز است.)"
    )


# ──────────────────────────────────────────────────
# Target resolution
# ──────────────────────────────────────────────────

async def _resolve_target(
    message: Message, args: str
) -> tuple[int, str, str, str, str]:
    """Work out who a whisper is for.

    A reply wins — Telegram hands us the whole ``User`` object with it, so that
    path is free and always right. Otherwise the first argument is resolved by
    :func:`utils.target_lookup.resolve_target`, the same policy the inline picker
    uses, so the two entry points cannot disagree about who exists.

    Returns ``(target_id, name, username, remaining_text, error)`` where
    ``error`` is empty on success.
    """
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        return (
            reply.from_user.id,
            reply.from_user.first_name or "کاربر",
            reply.from_user.username or "",
            args.strip(),
            "",
        )

    parts = (args or "").strip().split(maxsplit=1)
    if not parts:
        return 0, "", "", "", (
            "⚠️ مشخص نشد نجوا برای چه کسی است.\n\n"
            "پیام طرف را <b>ریپلای</b> کنید و دوباره /نجوا را بفرستید، "
            "یا بنویسید: <code>/نجوا @username متن</code>"
        )

    head, rest = parts[0], (parts[1] if len(parts) > 1 else "").strip()

    # Same resolver as the inline picker, so the two entry points can never
    # disagree about who exists. It consults the in-memory group roster first
    # (free), then the ``users`` table (which proves the whisper is deliverable),
    # then Telegram. This branch used to read the ``users`` table directly, so a
    # member of the group who had never started the bot was reported as missing
    # here while the picker could resolve them, and vice versa.
    lookup = await resolve_target(message.bot, head)

    if lookup.found:
        return (
            lookup.user_id,
            lookup.first_name or "کاربر",
            lookup.username,
            rest,
            "",
        )

    if lookup.miss == MISS_UNKNOWN:
        return 0, "", "", "", (
            f"⚠️ <b>{escape(head)}</b> پیدا نشد.\n\n"
            f"{escape(UNKNOWN_HINT)}"
        )

    return 0, "", "", "", (
        "⚠️ گیرنده مشخص نشد.\n\n"
        "پیام طرف را <b>ریپلای</b> کنید یا بنویسید: "
        "<code>/نجوا @username متن</code>"
    )


# ──────────────────────────────────────────────────
# Forced-join gate
# ──────────────────────────────────────────────────

async def _missing(bot, user_id: int) -> list:
    """Required chats the user still has to join (bypassing the TTL cache)."""
    return await missing_required_chats(bot, user_id, force_refresh=True)


async def _park_draft(
    fsm_storage: BaseStorage,
    bot_id: int,
    user_id: int,
    payload: dict,
    state=WhisperStates.waiting_for_join,
) -> None:
    """Store a half-finished whisper under the private-chat FSM key."""
    key = _dm_key(bot_id, user_id)
    await fsm_storage.set_state(key=key, state=state)
    await fsm_storage.set_data(key=key, data=payload)


def _draft_payload(
    *,
    receiver_id: int,
    receiver_name: str,
    receiver_username: str,
    chat_id: int,
    chat_title: str,
    text: str,
) -> dict:
    """Normalise the parking payload so every resume path reads the same keys."""
    return {
        "pending_receiver_id": receiver_id,
        "pending_receiver_name": receiver_name,
        "pending_receiver_username": receiver_username,
        "pending_chat_id": chat_id,
        "pending_chat_title": chat_title,
        "pending_kind": "text",
        "pending_text": text,
    }


_BODY_PROMPT = (
    "✍️ <b>متن پیام ناشناس را بفرستید</b>\n\n"
    "می‌توانید یکی از این‌ها را ارسال کنید:\n"
    "• متن — ساده‌ترین حالت\n"
    "• عکس — به‌همراه کپشن دلخواه\n"
    "• ویس صوتی\n\n"
    "پیام شما به‌صورت ناشناس برای <b>{who}</b> در گروه «{group}» ارسال می‌شود."
)


def _body_prompt_text(receiver_name: str, chat_title: str) -> str:
    return _BODY_PROMPT.format(
        who=escape(receiver_name or "گیرنده"),
        group=escape(chat_title or "گروه"),
    )


# ──────────────────────────────────────────────────
# 1. A group hears NOTHING it did not ask for
# ──────────────────────────────────────────────────
#
# There used to be two answer paths into a group:
#
#   * a ``my_chat_member`` greeting card whenever the bot was added, and
#   * this router's slash hint, which answered a bare "/" and every unknown
#     command with the guide — so ``/start``, ``/help`` and ``/menu`` printed
#     the same card three more ways.
#
# Both are gone on purpose. The only message a group gets from the bot is the
# single card posted once, when the bot is added (see
# ``handlers.group_lifecycle``); every other command, present or absent, is
# left unanswered. The whisper commands themselves (``/نجوا`` and friends)
# keep working — they are what the card points at.


@router.callback_query(F.data == "whisper:help")
async def cb_whisper_help(callback: CallbackQuery) -> None:
    """Expand the usage card into the instructions (no extra buttons)."""
    await callback.answer()
    await _edit_or_send(callback, _usage_text(), None)


@router.callback_query(F.data == "whisper:noop")
async def cb_whisper_noop(callback: CallbackQuery) -> None:
    """Absorbs presses on decorative / disabled buttons."""
    await callback.answer("ℹ️ این دکمه فقط برای نمایش است.")


# ──────────────────────────────────────────────────
# 2. Delivery (shared by the live command and the resumed draft)
# ──────────────────────────────────────────────────

async def _deliver(
    bot,
    *,
    sender_id: int,
    receiver_id: int,
    receiver_name: str,
    receiver_username: str,
    chat_id: int,
    chat_title: str,
    kind: str,
    text: str,
    file_id: str,
) -> tuple[Whisper | None, str, bool]:
    """Validate, store and publish a whisper.

    Returns ``(whisper, error, reachable)``. ``reachable`` says whether the
    recipient has ever opened a chat with the bot, which is the one thing about
    this send the SENDER needs to be told and cannot be worked out afterwards:
    somebody who never pressed ``/start`` cannot use the «مشاهده پیام» button
    until they do, and a cosmetic caveat is not worth an ambiguity that would put
    a scary warning in front of a whisper that is working perfectly well.

    It is answered from the ``users`` table rather than by attempting a private
    message. That used to be how it was found — send the recipient a notice and
    see whether it arrived — which meant every recipient's chat got a
    «you have an anonymous message» alert purely as a side effect of a delivery
    probe, and every whisper spent an API call re-learning what the database
    already holds. :func:`utils.target_lookup.has_started_bot` asks the same
    question for free.
    """
    config = await get_whisper_config()

    if not config.enabled:
        return None, "⛔ قابلیت نجوا در حال حاضر غیرفعال است.", False

    if receiver_id == sender_id:
        return None, "⚠️ نمی‌توانید به خودتان نجوا بدهید.", False
    if receiver_id == bot.id:
        return None, "⚠️ نمی‌توانید به ربات نجوا بدهید.", False

    # Sender and receiver must both still be inside the group.
    if not await is_member(bot, chat_id, sender_id):
        return None, f"⛔ شما دیگر عضو گروه «{escape(chat_title)}» نیستید.", False

    # The receiver check goes through the shared verdict rather than
    # ``is_member``, which answers False when the lookup itself failed. A failed
    # lookup is not evidence of absence: in every group where the bot is still a
    # plain member that is what an ordinary member looks like, so the old check
    # refused people who were standing right there. See
    # :func:`utils.target_lookup.recipient_verdict`.
    #
    # A receiver who has never started the bot is deliberately allowed through —
    # the group card carries the button that reveals the whisper to the tapper,
    # and _send_dm below reports honestly when the private ping was impossible.
    verdict = await recipient_verdict(bot, chat_id, receiver_id)
    if verdict == VERDICT_BAD_ID:
        return None, (
            "⛔ کاربری با این آیدی در گروه «"
            f"{escape(chat_title)}» پیدا نشد؛ شماره را بررسی کنید یا "
            "پیام طرف را ریپلای کنید."
        ), False
    if verdict == VERDICT_NOT_HERE:
        return None, "⛔ گیرندهٔ نجوا عضو این گروه نیست، بنابراین پیام ارسال نشد.", False

    if await _are_blocked(sender_id, receiver_id):
        return None, "⛔ ارسال نجوا بین شما و این کاربر ممکن نیست.", False

    if kind == "text":
        if not text:
            return None, (
                "⚠️ متن پیام خالی است.\n\nپس از دستور، متن پیام را بنویسید."
            ), False
        if len(text) > (config.max_length or 700):
            return None, (
                f"⚠️ متن نجوا طولانی است.\nحداکثر مجاز: "
                f"<b>{config.max_length}</b> کاراکتر (شما: {len(text)})."
            ), False

    # The coin, charged before the flood gate — the same order and the same single
    # charge as the inline picker's ``_run_send_gates``, so a whisper costs the
    # same however it was typed and neither road can be used to dodge the other.
    # The value returned is the exact amount taken, handed back verbatim if a
    # later gate refuses (see :func:`refund_balance`).
    charged = await check_and_deduct_balance(sender_id)
    if charged is None:
        _, coins = await can_afford_whisper(sender_id)
        return None, no_balance_text(coins), False

    # Charged once, and only when the whisper is really going out. The sender
    # may never have pressed /start — a group member can whisper straight away
    # — so the charge registers them on the spot instead of reporting a quota
    # they never had.
    # The only refusal left is the flood limit; whisper coins are refunded
    # so the sender is never charged for a message that was never delivered.
    auth = await authorize_chat_message(sender_id)
    if not auth.allowed:
        await refund_balance(sender_id, charged)
        if not should_warn(sender_id):
            return None, "", False
        try:
            policy = await get_policy()
            return None, rate_limit_text(auth.wait, policy.messages_per_minute), False
        except Exception as exc:  # noqa: BLE001 — a warning must not crash the send
            logger.info("Could not render the rate-limit warning: %s", exc)
            return None, rate_limit_text(auth.wait), False

    async with async_session_factory() as session:
        whisper = Whisper(
            sender_id=sender_id,
            receiver_id=receiver_id,
            chat_id=chat_id,
            chat_title=chat_title,
            content=text or None,
            media_type=None if kind == "text" else kind,
            media_file_id=file_id or None,
            is_viewed=False,
        )
        session.add(whisper)
        await session.commit()
        whisper_id = whisper.id

    mention = mention_html(receiver_id, receiver_name, receiver_username)

    # Does this person have a private chat with the bot? A free database read,
    # and the only input the sender's caveat needs.
    reachable = await has_started_bot(receiver_id)

    # The recipient is NOT pinged on the happy path. The card in the group is
    # the entire delivery, and a private «you have an anonymous message» on top
    # of it duplicated the feature into the recipient's own chat for no gain: the
    # card already names them, and the reader opens it from the group.
    #
    # So the card is posted FIRST, and the private notice below is a genuine
    # fallback — it fires only when posting failed, where it is the one route
    # left and dropping it would leave a committed row nothing can ever read.
    card_text = _whisper_card_text(mention, chat_title, reachable=reachable)
    card_message_id = await _post_card(
        bot, chat_id, card_text, whisper_card_kb(whisper_id)
    )

    if card_message_id is not None:
        async with async_session_factory() as session:
            row = await session.get(Whisper, whisper_id)
            if row is not None:
                row.card_message_id = card_message_id
                await session.commit()
    else:
        # No card in the group: the whisper is now reachable ONLY through the
        # receiver's direct message, so this is the one case where the notice is
        # the delivery rather than a courtesy — and the sender is told where it
        # went, otherwise they would sit there waiting for a card that never
        # arrives.
        notice = await _dm_message(
            bot,
            receiver_id,
            whisper_notice_text(chat_title),
            whisper_card_kb(whisper_id),
        )
        await _attach_notice(whisper_id, notice.message_id if notice else None)
        await _send_dm(
            bot,
            sender_id,
            "ℹ️ <b>کارت نجوا در گروه منتشر نشد.</b>\n\n"
            "پیام شما مستقیم به چت خصوصی گیرنده فرستاده شد؛ او با دکمهٔ "
            "«مشاهده پیام» آن را می‌خواند.",
        )

    return whisper, "", reachable


def _sent_ok_text(receiver_name: str, reachable: bool = True) -> str:
    """What the SENDER is told once a whisper is live.

    One definition for all three send paths (``/نجوا`` in the group, the
    «عضو شدم» resume, and the parked-body resume). They used to carry three
    separately-worded copies of the same confirmation, which is exactly how the
    "not started the bot" caveat ended up on one road and missing from the other
    two — the sender would be told their whisper went out perfectly while the
    recipient had never heard of it.

    The caveat is appended rather than replacing the confirmation, and never the
    other way round: the whisper really was sent and really is readable, so
    leading with a warning would make a working feature look broken.
    """
    who = escape(receiver_name) if receiver_name else "گیرنده"
    text = (
        "✅ <b>نجوای شما فرستاده شد</b>\n\n"
        f"گیرنده: {who}\n\n"
        "🔒 نام شما روی کارت گروه نمی‌آید و متن فقط در چت خصوصی گیرنده باز "
        "می‌شود.\n"
        "👁️ به‌محض اینکه آن را بخواند، روی کارت گروه «خوانده شد» می‌نشیند."
    )
    if reachable:
        return text
    return f"{text}\n\n" + target_not_started_text(receiver_name)


async def _confirm_to_sender(
    bot,
    message: Message,
    sender_id: int,
    receiver_name: str,
    reachable: bool = True,
) -> None:
    """Confirm privately — never in the group, that would leak the sender."""
    who = escape(receiver_name) if receiver_name else "گیرنده"
    await _notify(
        bot,
        message,
        sender_id,
        _sent_ok_text(receiver_name, reachable),
        group_note=f"✅ <b>نجوا ارسال شد</b> — فقط برای {who}.",
    )


def _extract_body(message: Message) -> tuple[str, str, str] | None:
    """Read a supported message body. Returns ``None`` for anything else."""
    if message.content_type == "text":
        text = (message.text or "").strip()
        return ("text", text, "") if text else None
    if message.content_type == "photo":
        return "photo", (message.caption or "").strip(), message.photo[-1].file_id
    if message.content_type == "voice":
        return "voice", "", message.voice.file_id
    return None


# ──────────────────────────────────────────────────
# 3. The group command
# ──────────────────────────────────────────────────

@router.message(
    Command(*WHISPER_COMMANDS),
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def cmd_whisper(
    message: Message,
    command: CommandObject,
    fsm_storage: BaseStorage,
) -> None:
    """Handle ``/نجوا`` inside a group."""
    user_id = message.from_user.id
    args = (command.args or "").strip()

    config = await get_whisper_config()
    if not config.enabled:
        await _notify(
            message.bot, message, user_id, "⛔ قابلیت نجوا غیرفعال است."
        )
        return

    target_id, target_name, target_username, text, error = await _resolve_target(
        message, args
    )
    if error:
        await _notify(
            message.bot,
            message,
            user_id,
            error,
            group_note="⚠️ <b>نجوا ارسال نشد.</b>\nروی پیام طرف ریپلای کنید و "
            "بنویسید: <code>/نجوا متن پیام</code>",
        )
        return

    draft = _draft_payload(
        receiver_id=target_id,
        receiver_name=target_name,
        receiver_username=target_username,
        chat_id=message.chat.id,
        chat_title=message.chat.title or "",
        text=text,
    )

    # ── Forced-join requirement ──
    # A sender who has never started the bot is still allowed to send:
    # they register themselves on the spot (authorize_chat_message does it)
    # and their whisper goes out normally. The RECEIVER, however, must be
    # inside the required channel to open it — that is enforced at the
    # view/callback level, not here, so a never-started sender is never
    # blocked from composing and sending their message.
    missing = await _missing(message.bot, user_id)
    if missing:
        # Still, if the sender is a brand-new user who has never started the
        # bot, we notify them warmly about joining (this is the "UI/UX"
        # part): they can now send right away, and the receiver will need
        # the channel join before reading their message.
        await _park_draft(
            fsm_storage, message.bot.id, user_id, draft
        )
        await _send_dm(
            message.bot,
            user_id,
            # ``_JOIN_REMINDER`` lives in middleware/force_join.py and is not
            # exported from it, so naming it here raised NameError on every
            # /نجوا sent by a user who had not joined the channel — the one
            # audience this branch exists for. Use this module's own prompt.
            _join_prompt_text(missing),
            whisper_join_kb(missing),
        )
        # Inform the sender in the group that their whisper is already
        # going out (no dead-end). We do this with a minimal, friendly note
        # instead of a rejection so the user feels their request is in
        # progress, not refused.
        try:
            await message.answer(
                "✅ <b>نجوا شما ارسال شد!</b>\n\n"
                "بعد از عضویت در کانال، گیرنده به ربات سر می‌زند تا پیام را بخواند.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    clear_membership_cache()

    # ── No body yet → collect it privately (text, photo or voice) ──
    if not text:
        await _park_draft(
            fsm_storage,
            message.bot.id,
            user_id,
            draft,
            state=WhisperStates.waiting_for_body,
        )
        await _notify(
            message.bot,
            message,
            user_id,
            _body_prompt_text(target_name, draft["pending_chat_title"]),
            group_note="✍️ متن پیام را در گفتگوی خصوصی ربات بفرستید.",
        )
        return

    whisper, error, reachable = await _deliver(
        message.bot,
        sender_id=user_id,
        receiver_id=target_id,
        receiver_name=target_name,
        receiver_username=target_username,
        chat_id=message.chat.id,
        chat_title=message.chat.title or "",
        kind="text",
        text=text,
        file_id="",
    )
    if whisper is None:
        if error:
            await _notify(
                message.bot,
                message,
                user_id,
                error,
                group_note="⚠️ <b>نجوا ارسال نشد.</b> جزئیات در پی‌وی ربات.",
            )
        return
    await _confirm_to_sender(
        message.bot, message, user_id, target_name, reachable
    )


# ──────────────────────────────────────────────────
# 4. "عضو شدم" — resume whatever was waiting
# ──────────────────────────────────────────────────

@router.callback_query(F.data == "whisper:check")
async def cb_whisper_check(
    callback: CallbackQuery, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Re-check membership after the user tapped "عضو شدم".

    A never-started user who taps this sees a warm, short prompt; after they
    join they tap it again and their parked draft auto-resumes. No dead end,
    no bouncing loop — the join gate is re-checked inside the handler.
    """
    user_id = callback.from_user.id
    data = await state.get_data()

    missing = await _missing(callback.bot, user_id)
    if missing:
        # Fresh join attempt: show a warm, actionable reminder. The user
        # taps "عضو شدم" which re-triggers THIS callback, so the loop is
        # intentional and short — they join, come back, tap again.
        # Plain text on purpose: answerCallbackQuery never renders HTML, so
        # <b> tags used to reach the user literally, nested tags included.
        await callback.answer(
            "🔐 برای ادامه «نجوا» باید در «{title}» عضو شوید.\n\n"
            "⏹️ از لینک عضو شوید، بعد از احراز به گوشی بزنید؛ دکمهٔ "
            "«عضو شدم» خودکار درخواست شما را ادامه می‌دهد.".format(
                title=missing[0].title
            ),
            show_alert=True,
        )
        await _edit_or_send(
            callback,
            _JOIN_PROMPT.format(lines=join_bullet(missing[0])),
            whisper_join_kb(missing),
        )
        return

    clear_membership_cache()
    await state.clear()

    # ── A. Receiver waiting to open an existing whisper ──
    whisper_id = data.get("pending_whisper_id")
    if whisper_id:
        try:
            resolved_id = int(whisper_id)
        except (TypeError, ValueError):
            await callback.answer(
                "⚠️ درخواست منقضی شده است؛ لطفاً دوباره تلاش کنید.",
                show_alert=True,
            )
            await state.clear()
            return
        await callback.answer("✅ عضویت تایید شد — پیام ارسال می‌شود.")
        await _open_whisper(callback.bot, user_id, resolved_id)
        return

    # ── B. Sender with a parked draft ──
    if data.get("pending_receiver_id"):
        chat_title = data.get("pending_chat_title") or ""
        body = (data.get("pending_text") or "").strip()

        # Still no body → ask for it now that the join gate is cleared.
        if not body:
            await _park_draft(
                fsm_storage,
                callback.bot.id,
                user_id,
                dict(data),
                state=WhisperStates.waiting_for_body,
            )
            await callback.answer("✅ عضویت تایید شده است")
            await _send_dm(
                callback.bot,
                user_id,
                _body_prompt_text(data.get("pending_receiver_name") or "", chat_title),
            )
            return

        # The park holds whatever an earlier flow wrote; a corrupt or partial
        # draft must degrade into a clear refusal, never a KeyError that dies
        # in the logs while the user stares at a spinner.
        try:
            receiver_id = int(data["pending_receiver_id"])
            chat_id = int(data.get("pending_chat_id") or 0)
        except (KeyError, TypeError, ValueError):
            await callback.answer(
                "⚠️ درخواست منقضی شده است؛ لطفاً دوباره تلاش کنید.",
                show_alert=True,
            )
            return

        await callback.answer("✅ عضویت تایید شد — در حال ارسال...")
        whisper, error, reachable = await _deliver(
            callback.bot,
            sender_id=user_id,
            receiver_id=receiver_id,
            receiver_name=data.get("pending_receiver_name") or "کاربر",
            receiver_username=data.get("pending_receiver_username") or "",
            chat_id=chat_id,
            chat_title=chat_title,
            kind=data.get("pending_kind") or "text",
            text=body,
            file_id=data.get("pending_file_id") or "",
        )
        if whisper is None:
            if error:
                await _send_dm(callback.bot, user_id, error)
            return
        await _send_dm(
            callback.bot,
            user_id,
            _sent_ok_text(
                data.get("pending_receiver_name") or "", reachable
            ),
        )
        return

    # ── C. Nothing was pending ──
    await callback.answer("✅ عضویت تایید شد!")
    await _edit_or_send(
        callback,
        "✅ <b>عضویت تایید شد!</b>\n\n"
        "حالا در گروه، پیام طرف را ریپلای کنید و "
        "<code>/نجوا متن پیام</code> بفرستید.",
        None,
    )


#: Main-menu labels — typing one of these always abandons a parked whisper,
#: so a half-finished draft can never trap a user away from the rest of the bot.
#:
#: Verified against ``keyboards.main_menu_kb()``: five entries here went stale
#: when the menu was relabelled (🔀 اتصال شانسی, 📩/🔗 prefix swap, 📖), which
#: meant pressing those buttons mid-park did NOT escape — the fresh label was
#: swallowed as the whisper's own body. The three connect labels are all
#: present because any of them starts a match. The old spellings and the admin
#: panels are kept: an older reply keyboard can still be on screen, and the
#: panels are real buttons for admins. The chat-ended pair (next chat / rematch)
#: is kept for the same reason: an end card can be on screen when the whisper
#: draft gets parked, and a swallowed tap there is a trap.
_MENU_ESCAPE_TEXTS = {
    "🔀 اتصال شانسی",
    "👩 چت با دختر",
    "👨 چت با پسر",
    NEXT_CHAT_LABEL,
    REMATCH_LABEL,
    "🔗 اتصال به ناشناس",
    "👤 پروفایل من",
    "📩 پیام‌های ناشناس من",
    "📬 پیام‌های ناشناس من",
    "🔗 لینک ناشناس من",
    "📬 لینک ناشناس من",
    "🏆 امتیازات و سکه",
    "🎁 دعوت دوستان",
    INLINE_HELP_LABEL,
    "📖 راهنما و قوانین",
    "📋 راهنما و قوانین",
    "⛔️ لیست مسدودی‌ها",
    "👑 پنل مدیریت",
    "👤 پنل کاربری",
}


async def _escape_park(message: Message, state: FSMContext) -> bool:
    """Abandon a parked whisper when the user reaches for the main menu.

    Returns ``True`` when the message was consumed as an escape attempt, in
    which case the caller must stop processing it.
    """
    text = (message.text or "").strip()

    if text.startswith("/start"):
        await state.clear()
        await message.answer("منوی اصلی:", reply_markup=main_menu_kb())
        return True

    if text in _MENU_ESCAPE_TEXTS:
        await state.clear()
        await message.answer(
            "✅ درخواست ناشناس لغو شد.\n\nمنوی اصلی:",
            reply_markup=main_menu_kb(),
        )
        return True

    return False


@router.message(WhisperStates.waiting_for_join, F.chat.type == ChatType.PRIVATE)
async def whisper_waiting_for_join_private(
    message: Message, state: FSMContext
) -> None:
    """Nudge the user while a whisper is parked behind the join gate."""
    if await _escape_park(message, state):
        return
    await message.answer(
        "🔐 برای ادامهٔ درخواست نجوا ابتدا باید عضویت اجباری را کامل کنید.\n\n"
        "روی دکمهٔ «عضو شدم» در پیام بالا بزنید تا درخواست شما خودکار "
        "ارسال شود.",
    )


@router.message(
    WhisperStates.waiting_for_body,
    F.chat.type == ChatType.PRIVATE,
    F.content_type.in_({"text", "photo", "voice"}),
)
async def whisper_collect_body(
    message: Message, state: FSMContext
) -> None:
    """Receive the whisper body (text / photo / voice) sent in the DM."""
    user_id = message.from_user.id

    if await _escape_park(message, state):
        return

    data = await state.get_data()

    if not data.get("pending_receiver_id") or not data.get("pending_chat_id"):
        await state.clear()
        await message.answer("⚠️ درخواست نامعتبر است. دوباره از گروه تلاش کنید.")
        return

    body = _extract_body(message)
    if body is None:
        await message.answer(
            "⚠️ فقط متن، عکس یا ویس صوتی پشتیبانی می‌شود.\n"
            "لطفاً یکی از این‌ها را بفرستید.",
        )
        return

    kind, text, file_id = body
    chat_title = data.get("pending_chat_title") or ""

    # Same guard as the resume path above: the truthiness check on line ~1085
    # rules out missing keys, but a corrupted value would still raise inside
    # int() — and that must land on a friendly message, not a dead handler.
    try:
        receiver_id = int(data["pending_receiver_id"])
        chat_id = int(data["pending_chat_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.answer("⚠️ درخواست نامعتبر است. دوباره از گروه تلاش کنید.")
        return

    whisper, error, reachable = await _deliver(
        message.bot,
        sender_id=user_id,
        receiver_id=receiver_id,
        receiver_name=data.get("pending_receiver_name") or "کاربر",
        receiver_username=data.get("pending_receiver_username") or "",
        chat_id=chat_id,
        chat_title=chat_title,
        kind=kind,
        text=text,
        file_id=file_id,
    )

    await state.clear()

    if whisper is None:
        if error:
            await message.answer(error, parse_mode="HTML")
        return

    await message.answer(
        _sent_ok_text(data.get("pending_receiver_name") or "", reachable),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────
# 5. Opening a whisper — receiver only
# ──────────────────────────────────────────────────

async def _open_whisper(
    bot, user_id: int, whisper_id: int, *, card: Message | None = None
) -> None:
    """Deliver the whisper content into the reader's private chat.

    ``card`` is the group message the button was pressed on, when the caller has
    it. It is used for exactly one thing: writing the read receipt onto the card
    (:data:`_READ_RECEIPT_LINE`) the first time it is opened. Passing it is
    optional — the second entry point (the one that resumes after a forced-join)
    no longer has the message in hand, and losing the receipt there is better
    than re-fetching a whole chat history to recover it.
    """
    first_read = False
    notice_message_id = None
    row_missing = False
    async with async_session_factory() as session:
        whisper = await session.get(Whisper, whisper_id)
        if whisper is None:
            # Nothing here expires: a whisper stays readable for as long as the
            # row exists. So the old wording ("دیگر در دسترس نیست" — which
            # reads as an expired read-timer) described a failure the user
            # could do nothing about and blamed the wrong thing. Say what is
            # actually true instead: this button belongs to a card whose send
            # was never completed. The send itself happens AFTER the session
            # closes — a Bot API round-trip must never pin a pooled connection.
            row_missing = True
        else:
            if not whisper.is_viewed:
                whisper.is_viewed = True
                first_read = True
                await session.commit()
            content = whisper.content or ""
            media_type = whisper.media_type
            file_id = whisper.media_file_id
            chat_id = whisper.chat_id
            card_message_id = whisper.card_message_id
            notice_message_id = whisper.notice_message_id

    if row_missing:
        await _send_dm(bot, user_id, _WHISPER_MISSING)
        return

    if not content.strip() and not file_id:
        await _send_dm(
            bot,
            user_id,
            "⚠️ این پیام خالی است.\n\n"
            "احتمالاً فرستنده متن را نفرستاده. لطفاً از او بخواهید دوباره "
            "بفرستد.",
        )
        return

    # The recipient's own «👁️ یک پیام مخفی دارید» notice becomes its receipt on
    # the FIRST read, exactly like the group card does one block below — driven
    # off the same committed flag, so it can never be claimed twice, and never
    # claimed by the sender re-opening their own card. Placed after the
    # empty-content guard on purpose: that path does not deliver the message, so
    # writing «پیام خوانده شد» there would be a lie about a message they never
    # saw.
    if first_read:
        await _mark_notice_read(bot, user_id, notice_message_id)

    await _send_dm(
        bot,
        user_id,
        "🔇 <b>پیام ناشناس برای شما</b>\n\n"
        "فرستندهٔ این پیام ناشناس است و هویت او مشخص نیست.\n"
        "——————————————",
    )

    try:
        if media_type == "photo" and file_id:
            await bot.send_photo(
                user_id,
                photo=file_id,
                caption=escape(content) if content else None,
                parse_mode="HTML",
            )
        elif media_type == "voice" and file_id:
            await bot.send_voice(user_id, voice=file_id)
            if content:
                await bot.send_message(
                    user_id, escape(content), parse_mode="HTML"
                )
        else:
            await bot.send_message(
                user_id,
                f"💬 <b>{escape(content or '(پیام خالی)')}</b>",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Could not deliver whisper %s: %s", whisper_id, exc)
        await _send_dm(bot, user_id, "⚠️ ارسال محتوای پیام ناموفق بود.")

    # Flip the group card to a disabled "read" button, and on the FIRST read say
    # so in the card's own text as well. The button greying out is the receipt
    # for whoever looks closely; the line is the receipt for the rest of the
    # group, who would otherwise never learn whether the message was collected.
    if card_message_id is not None:
        if first_read and card is not None and (card.text or "").strip():
            # Text AND keyboard in one edit: two edits of the same message is one
            # round trip too many, and the second would race the first.
            try:
                await card.edit_text(
                    f"{card.text.rstrip()}\n\n{_READ_RECEIPT_LINE}",
                    parse_mode="HTML",
                    reply_markup=whisper_viewed_kb(),
                )
            except TelegramBadRequest as exc:
                # Already stamped — two presses in the same moment, or the same
                # card reached from the picker and from the button.
                if _NOT_MODIFIED not in str(exc).lower():
                    logger.debug("Could not stamp whisper card %s: %s", whisper_id, exc)
            except Exception as exc:
                logger.debug("Could not stamp whisper card %s: %s", whisper_id, exc)
            return

        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=card_message_id,
                reply_markup=whisper_viewed_kb(),
            )
        except Exception as exc:
            logger.debug("Could not update whisper card: %s", exc)


@router.callback_query(F.data.startswith("whisper:view:"))
async def cb_whisper_view(
    callback: CallbackQuery, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Only the addressed receiver may open a whisper.

    Everyone else is refused — and recorded in ``whisper.snoopers`` on the way
    out, so the sender and the receiver can later see that the card was fished.
    """
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("⚠️ خطا در پردازش.", show_alert=True)
        return
    try:
        whisper_id = int(parts[2])
    except (TypeError, ValueError):
        await callback.answer("⚠️ شناسهٔ نامعتبر.", show_alert=True)
        return

    user_id = callback.from_user.id

    # The refusals below are decided inside the session but DELIVERED after it
    # closes: answerCallbackQuery is a network call, and a Bot API round-trip
    # must never hold one of the five pooled connections open.
    refusal: str | None = None
    async with async_session_factory() as session:
        whisper = await session.get(Whisper, whisper_id)
        if whisper is None:
            refusal = "⚠️ این پیام یافت نشد."
        elif whisper.receiver_id != user_id:
            # Not the addressed receiver: record the attempt on the row so the
            # two people this whisper is about can see, from «📊 آمار», that
            # somebody was knocking. De-duplicated by user_id and keeping the
            # first press each — the ledger answers "who tried", not "who tried
            # hardest". Bookkeeping only: the refusal below does not depend on
            # this write succeeding.
            seen = list(whisper.snoopers or [])
            if not any(entry.get("user_id") == user_id for entry in seen):
                seen.append(
                    {
                        "user_id": user_id,
                        "name": " ".join(
                            (
                                callback.from_user.full_name
                                or callback.from_user.first_name
                                or (
                                    f"@{callback.from_user.username}"
                                    if callback.from_user.username
                                    else str(user_id)
                                )
                            ).split()
                        ),
                        "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                    }
                )
                # Reassigned, never mutated in place: SQLAlchemy only sees a
                # change it can watch.
                whisper.snoopers = seen
                await session.commit()
            refusal = "🚫 شما اجازه خواندن این نجوا را ندارید!"
        else:
            chat_id = whisper.chat_id

    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return

    # ── The reader must still be inside the group it was sent in ──
    status = await get_chat_member_status(callback.bot, chat_id, user_id)
    if status is None:
        # A failed lookup means the bot itself can no longer see that group,
        # so the membership of the reader cannot be trusted either.
        await callback.answer(
            "⚠️ این پیام دیگر قابل نمایش نیست (ربات در آن گروه دسترسی ندارد).",
            show_alert=True,
        )
        return
    if status not in MEMBER_STATUSES:
        await callback.answer(
            "⛔ شما دیگر عضو این گروه نیستید، بنابراین پیام نمایش داده نمی‌شود.",
            show_alert=True,
        )
        return

    # ── Forced-join requirement ──
    # The reader must be inside the required channel to open the whisper.
    # A never-started user is welcomed warmly: they are told to join, and
    # their "عضو شدم" press re-enters this handler, which picks the whisper
    # back up automatically.
    missing = await _missing(callback.bot, user_id)
    if missing:
        await _park_draft(
            fsm_storage, callback.bot.id, user_id, {"pending_whisper_id": whisper_id}
        )
        # Plain text: answerCallbackQuery does not parse HTML (see cb_whisper_check).
        await callback.answer(
            "🔐 برای خواندن این پیام باید در «{title}» عضو شوید.\n\n"
            "⏹️ از لینک عضو شوید؛ بعد از احراز، دکمهٔ «عضو شدم» خودکار "
            "پیام را به شما ارسال می‌کند.".format(title=missing[0].title),
            show_alert=True,
        )
        await _send_dm(
            callback.bot,
            user_id,
            _JOIN_PROMPT.format(lines=join_bullet(missing[0])),
            whisper_join_kb(missing),
        )
        return

    clear_membership_cache()
    await callback.answer("✅ پیام برای شما ارسال شد.")
    await _open_whisper(
        callback.bot, user_id, whisper_id, card=callback.message
    )


# ──────────────────────────────────────────────────
# 6. Private-chat explanation
# ──────────────────────────────────────────────────

@router.message(Command(*WHISPER_COMMANDS), F.chat.type == ChatType.PRIVATE)
async def cmd_whisper_private(message: Message) -> None:
    """``/نجوا`` in a private chat just explains what the feature is."""
    config = await get_whisper_config()
    if not config.enabled:
        await message.answer("⛔ قابلیت نجوا در حال حاضر غیرفعال است.")
        return

    required = ""
    if config.require_join:
        # The admin-panel list (``required_channels``) is the source of truth
        # now; the legacy single-chat fields are migrated into it on first
        # open of the force-join section and no longer read here.
        channels = await configured_channels()
        if channels:
            first = channels[0].title
            more = (
                f" و {len(channels) - 1} کانال دیگر" if len(channels) > 1 else ""
            )
            required = f"\n🔐 عضویت اجباری: <b>{escape(first)}</b>{more}\n"

    await message.answer(
        "🔇 <b>نجوا چیست؟</b>\n\n"
        "یک پیام خصوصی داخل گروه: فقط یک نفر می‌تواند آن را ببیند و بقیهٔ "
        "اعضا اصلاً به محتوا دسترسی ندارند.\n\n"
        "برای فرستادن نجوا باید ربات در آن گروه عضو باشد:\n"
        "۱) پیام طرف مقابل را ریپلای کنید\n"
        "۲) بنویسید <code>/نجوا متن پیام</code>\n\n" + required,
        parse_mode="HTML",
    )
