"""
Hybrid Async/Real-Time Anonymous Chat System.

Flow:
  1. Guest clicks deep link → enters AnonChatStates.in_session
  2. Guest sends messages (up to 10 without owner responding)
  3. Owner receives notification, opens inbox, sees guest list
  4. Owner clicks guest → enters AnonChatStates.in_session
  5. If both users are in session simultaneously → real-time routing
  6. If one user is offline → messages stored in DB, notifications sent
"""

import logging
from collections import defaultdict
from html import escape

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func as sql_func

from database import async_session_factory, AnonymousMessage, BlockList
from keyboards import main_menu_kb, anonymous_chat_menu_kb, unread_inbox_kb
from states import AnonChatStates, ChatState
from utils.economy import (
    authorize_chat_message,
    get_policy,
    rate_limit_text,
    should_warn,
)

logger = logging.getLogger(__name__)
router = Router()

# The inbox, guest list and session flow are private-chat only — none of them
# may post menus or cards into a group.
router.message.filter(F.chat.type == ChatType.PRIVATE)

MAX_UNREAD_MESSAGES = 10


def _card(callback: CallbackQuery) -> Message | None:
    """The tapped card when it is still a usable :class:`Message`.

    ``callback.message`` is ``None`` once the card has been deleted, and an
    ``InaccessibleMessage`` when Telegram refuses to hand the message back
    (inline mode, very old messages). Neither supports ``edit_text``, and a
    bare ``callback.message.answer`` on them raises — every call site in this
    module used to assume a live ``Message``.
    """
    msg = callback.message
    return msg if isinstance(msg, Message) else None


async def _fallback(callback: CallbackQuery, text: str, **kwargs) -> None:
    """Deliver ``text`` when the tapped card no longer exists.

    The callback itself cannot carry a message, so the answer goes to the
    user's private chat — which is where all of these handlers run anyway.
    """
    try:
        await callback.bot.send_message(callback.from_user.id, text, **kwargs)
    except Exception as exc:
        logger.debug("Could not deliver fallback message: %s", exc)


# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

async def _is_partner_online(
    storage: BaseStorage, bot_id: int, partner_id: int, my_id: int
) -> bool:
    """Check if partner is in AnonChatStates.in_session with us as partner."""
    try:
        key = StorageKey(bot_id=bot_id, user_id=partner_id, chat_id=partner_id)
        state = await storage.get_state(key=key)
        if state != AnonChatStates.in_session:
            return False
        data = await storage.get_data(key=key)
        return data.get("partner_id") == my_id
    except Exception:
        return False


async def _get_unread_count(sender_id: int, receiver_id: int) -> int:
    """Count unread messages from sender to receiver."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(sql_func.count(AnonymousMessage.id)).where(
                (AnonymousMessage.sender_id == sender_id)
                & (AnonymousMessage.receiver_id == receiver_id)
                & (AnonymousMessage.is_read.is_(False))
            )
        )
        return result.scalar() or 0


async def _get_sender_groups(receiver_id: int) -> dict[int, list]:
    """Get unread messages grouped by sender_id for the inbox."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonymousMessage).where(
                (AnonymousMessage.receiver_id == receiver_id)
                & (AnonymousMessage.is_read.is_(False))
            ).order_by(AnonymousMessage.created_at)
        )
        messages = result.scalars().all()

    groups = defaultdict(list)
    for msg in messages:
        groups[msg.sender_id].append(msg)
    return dict(groups)


async def _check_blocked(blocker_id: int, blocked_id: int) -> bool:
    """Check if blocker has blocked blocked_id."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == blocker_id)
                & (BlockList.blocked_id == blocked_id)
            )
        )
        return result.scalar_one_or_none() is not None


# ──────────────────────────────────────────────────
# 1. Deep Link Clicked → Enter Anonymous Session
# ──────────────────────────────────────────────────

async def handle_deep_link_anon(
    message: Message, state: FSMContext, guest_id: int, owner_id: int
) -> None:
    """Called from start.py when a valid deep link is clicked.
    
    Puts the guest into AnonChatStates.in_session with partner_id=owner_id.
    """
    # Check if guest is blocked by owner
    if await _check_blocked(owner_id, guest_id):
        await message.answer(
            "⛔ شما توسط این کاربر مسدود شده‌اید و امکان ارسال پیام ندارید.",
            reply_markup=main_menu_kb(),
        )
        return

    await state.set_state(AnonChatStates.in_session)
    await state.update_data(partner_id=owner_id, is_owner=False)

    await message.answer(
        "🔗 <b>لینک ناشناس تایید شد!</b>\n\n"
        "شما در حال ارسال پیام ناشناس هستید.\n"
        "پیام خود را بنویسید (حداکثر ۱۰ پیام پی‌درپی):",
        parse_mode="HTML",
        reply_markup=anonymous_chat_menu_kb(),
    )


# ──────────────────────────────────────────────────
# 2. Show Inbox (grouped by sender)
# ──────────────────────────────────────────────────

@router.message(F.text == "📩 پیام‌های ناشناس من")
async def show_inbox(message: Message, state: FSMContext) -> None:
    """Show inbox with guests who sent unread messages."""
    user_id = message.from_user.id

    # Check if already in an anonymous session
    current_state = await state.get_state()
    if current_state == AnonChatStates.in_session:
        await message.answer(
            "⚠️ شما در حال حاضر در یک چت ناشناس هستید.\n"
            "ابتدا چت فعلی را پایان دهید.",
            reply_markup=anonymous_chat_menu_kb(),
        )
        return

    sender_groups = await _get_sender_groups(user_id)

    if not sender_groups:
        await message.answer(
            "📭 <b>صندوق پیام‌های جدید شما خالی است.</b>\n\n"
            "لینک ناشناس خود را با دیگران به اشتراک بگذارید\n"
            "تا پیام‌های جدید در اینجا ظاهر شوند!",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return

    total_count = sum(len(msgs) for msgs in sender_groups.values())
    guest_count = len(sender_groups)
    await message.answer(
        f"📬 <b>{total_count} پیام ناشناس خوانده‌نشده از {guest_count} نفر:</b>\n\n"
        "روی یک کاربر کلیک کنید تا پیام‌هایش را ببینید:",
        parse_mode="HTML",
        reply_markup=unread_inbox_kb(sender_groups),
    )


# ──────────────────────────────────────────────────
# 3. Open Conversation with a Guest
# ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("anon_open:"))
async def cb_open_anon_conversation(
    callback: CallbackQuery, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Owner clicks on a guest → show unread messages and enter session."""
    try:
        sender_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ خطا در شناسه کاربر.", show_alert=True)
        return

    user_id = callback.from_user.id

    # Check if already in a session
    current_state = await state.get_state()
    if current_state == AnonChatStates.in_session:
        await callback.answer(
            "⚠️ شما در حال حاضر در یک چت ناشناس هستید.",
            show_alert=True,
        )
        return

    # Fetch all unread messages from this sender
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonymousMessage).where(
                (AnonymousMessage.sender_id == sender_id)
                & (AnonymousMessage.receiver_id == user_id)
                & (AnonymousMessage.is_read.is_(False))
            ).order_by(AnonymousMessage.created_at)
        )
        messages = result.scalars().all()

    if not messages:
        await callback.answer("📭 پیام جدیدی وجود ندارد.", show_alert=True)
        card = _card(callback)
        if card is not None:
            try:
                # The keyboard travels on edit_text — answerCallbackQuery has
                # no keyboard field, so a reply_markup= there is silently dead.
                await card.edit_text(
                    "📭 صندوق شما خالی است.", reply_markup=main_menu_kb()
                )
            except Exception:
                try:
                    await card.edit_reply_markup(reply_markup=main_menu_kb())
                except Exception:
                    pass
            await card.answer("منوی اصلی:")
        else:
            await _fallback(callback, "منوی اصلی:", reply_markup=main_menu_kb())
        return

    # Mark all messages as read
    async with async_session_factory() as session:
        for msg in messages:
            msg.is_read = True
        await session.commit()

    # Send all unread messages to the owner. The sender is named by POSITION in
    # the inbox («کاربر ۱») and by nothing else — printing their id here would
    # contradict the entire point of the inbox being anonymous.
    header = (
        "📩 <b>پیام‌های ناشناس دریافت‌شده:</b>\n\n"
        "——————————————"
    )
    card = _card(callback)
    if card is None:
        # The card is gone — keep the flow alive by writing to the PM.
        await _fallback(callback, header, parse_mode="HTML")
        for msg in messages:
            # _fallback writes with the global HTML parse mode — unescaped
            # user content (a stray "<") would make the whole send fail.
            await _fallback(callback, f"💬 {escape(msg.content)}")
    else:
        try:
            await card.edit_text(header, parse_mode="HTML")
        except Exception:
            pass
        for msg in messages:
            await card.answer(f"💬 {msg.content}")

    # Check if guest is currently online (in session with us)
    is_guest_online = await _is_partner_online(
        fsm_storage, callback.bot.id, sender_id, user_id
    )

    if is_guest_online:
        online_text = (
            "✅ <b>مخاطب شما آنلاین است! پیام‌های شما مستقیماً ارسال می‌شوند.</b>"
        )
    else:
        online_text = (
            "⏳ <b>مخاطب شما آفلاین است.</b>\n"
            "پیام‌های شما ذخیره شده و مخاطب پس از بازدید پاسخ خواهد داد."
        )
    # answerCallbackQuery never renders HTML — the toast gets a tag-free twin,
    # otherwise the user reads literal "<b>" in the alert. It also has no
    # keyboard field, so the session keyboard goes on via edit_reply_markup.
    online_alert = online_text.replace("<b>", "").replace("</b>", "")
    if card is not None:
        await card.answer(online_alert)
        try:
            await card.edit_reply_markup(
                reply_markup=anonymous_chat_menu_kb()
            )
        except Exception:
            pass
    else:
        await _fallback(
            callback, online_text, parse_mode="HTML",
            reply_markup=anonymous_chat_menu_kb(),
        )

    # Enter anonymous session
    await state.set_state(AnonChatStates.in_session)
    await state.update_data(partner_id=sender_id, is_owner=True)


# ──────────────────────────────────────────────────
# 4. Message Router (Hybrid Engine)
# ──────────────────────────────────────────────────

async def _rate_gate(message: Message, user_id: int) -> bool:
    """Anti-flood gate for an inbox message. ``True`` = may proceed.

    A refused send is ONLY ever a rate limit now: the per-message coin/token cost
    is gone, so there is no second refusal reason left to explain to the user.

    A plain helper, NOT a handler: it is awaited from inside
    :func:`anon_message_router`, which owns the ``@router.message`` decorator.
    The decorator once slid onto this function during a refactor, which made
    aiogram run only the gate — the relay handler was never registered, and
    every message in an anonymous session vanished after the rate check.
    """
    auth = await authorize_chat_message(user_id)
    if auth.allowed:
        return True
    if should_warn(user_id):
        try:
            policy = await get_policy()
            await message.answer(
                rate_limit_text(auth.wait, policy.messages_per_minute)
            )
        except Exception as exc:  # noqa: BLE001 — a warning must not crash
            logger.info("Could not render the rate-limit warning: %s", exc)
            await message.answer(rate_limit_text(auth.wait))
    return False


@router.message(AnonChatStates.in_session, F.text)
async def anon_message_router(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Route messages in anonymous session: real-time or async."""
    user_id = message.from_user.id
    data = await state.get_data()
    partner_id = data.get("partner_id")
    is_owner = data.get("is_owner", False)

    if not partner_id:
        await message.answer(
            "⚠️ خطا در پردازش. لطفاً دوباره تلاش کنید.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    # Handle exit button
    if message.text == "❌ پایان چت / بازگشت":
        await _exit_anon_session(message, state, fsm_storage)
        return

    content = message.text.strip()
    if not content:
        return

    # Check if blocked by partner
    if await _check_blocked(partner_id, user_id):
        await message.answer(
            "⛔ شما توسط این کاربر مسدود شده‌اید.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    # Check if partner is online and in session with us
    is_partner_online = await _is_partner_online(
        fsm_storage, message.bot.id, partner_id, user_id
    )

    if is_partner_online:
        # Anti-flood gate (chatting inside a connection is free of charge)
        if not await _rate_gate(message, user_id):
            return
        # Real-time routing: send directly
        try:
            await message.bot.send_message(
                partner_id, f"💬 {escape(content)}"
            )
            # Save as read since delivered instantly
            async with async_session_factory() as session:
                msg = AnonymousMessage(
                    sender_id=user_id,
                    receiver_id=partner_id,
                    content=content,
                    is_read=True,
                    is_owner_replying=is_owner,
                )
                session.add(msg)
                await session.commit()
        except Exception as e:
            logger.warning("Failed to send to partner %s: %s", partner_id, e)
            await message.answer(
                "⚠️ خطا در ارسال پیام. طرف مقابل ممکن است خارج شده باشد.",
                reply_markup=main_menu_kb(),
            )
            await state.clear()
    else:
        # Offline: enforce the unanswered-message cap before storing
        unread_count = await _get_unread_count(user_id, partner_id)

        if unread_count >= MAX_UNREAD_MESSAGES:
            await message.answer(
                "شما ۱۰ پیام بی‌پاسخ ارسال کرده‌اید.\n"
                "لطفاً منتظر پاسخ بمانید.",
                reply_markup=anonymous_chat_menu_kb(),
            )
            return

        if not await _rate_gate(message, user_id):
            return

        # Save to DB
        async with async_session_factory() as session:
            msg = AnonymousMessage(
                sender_id=user_id,
                receiver_id=partner_id,
                content=content,
                is_read=False,
                is_owner_replying=is_owner,
            )
            session.add(msg)
            await session.commit()

        # Send smart notification to partner
        try:
            if is_owner:
                # Owner replying to guest
                notification = "💬 مخاطب شما پاسخ داد! برای مشاهده به ربات سر بزنید."
            else:
                # Guest messaging owner
                notification = "📬 شما یک پیام ناشناس جدید دارید!"

            await message.bot.send_message(
                partner_id,
                notification,
                reply_markup=main_menu_kb(),
            )
        except Exception as e:
            logger.warning("Failed to notify partner %s: %s", partner_id, e)


# ──────────────────────────────────────────────────
# 5. Exit Anonymous Session
# ──────────────────────────────────────────────────

async def _exit_anon_session(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Exit the anonymous chat session and return to main menu."""
    user_id = message.from_user.id
    data = await state.get_data()
    partner_id = data.get("partner_id")

    await state.clear()
    await state.set_state(ChatState.idle)

    await message.answer(
        "❌ چت ناشناس پایان یافت.\nبه منوی اصلی بازگشتید.",
        reply_markup=main_menu_kb(),
    )

    # Notify partner if they're online
    if partner_id:
        is_partner_online = await _is_partner_online(
            fsm_storage, message.bot.id, partner_id, user_id
        )
        if is_partner_online:
            try:
                await message.bot.send_message(
                    partner_id,
                    "🚫 <b>مخاطب شما چت را ترک کرد.</b>",
                    parse_mode="HTML",
                    reply_markup=main_menu_kb(),
                )
                # Clear partner's state
                key = StorageKey(
                    bot_id=message.bot.id, user_id=partner_id, chat_id=partner_id
                )
                await fsm_storage.set_state(key=key, state=None)
                await fsm_storage.set_data(key=key, data={})
            except Exception as e:
                logger.warning("Failed to notify partner %s: %s", partner_id, e)


@router.message(
    F.text == "❌ پایان چت / بازگشت",
    AnonChatStates.in_session,
)
async def exit_anon_session(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Handle exit button press during anonymous session."""
    await _exit_anon_session(message, state, fsm_storage)


# ──────────────────────────────────────────────────
# 6. Re-open inbox (from within session)
# ──────────────────────────────────────────────────

@router.callback_query(F.data == "inbox:open")
async def cb_inbox_open(callback: CallbackQuery) -> None:
    """Re-open the inbox list."""
    user_id = callback.from_user.id
    sender_groups = await _get_sender_groups(user_id)

    if not sender_groups:
        card = _card(callback)
        if card is not None:
            try:
                await card.edit_text(
                    "📭 صندوق شما خالی است.", reply_markup=main_menu_kb()
                )
            except Exception:
                try:
                    await card.edit_reply_markup(reply_markup=main_menu_kb())
                except Exception:
                    pass
            await card.answer("منوی اصلی:")
        else:
            await _fallback(callback, "منوی اصلی:", reply_markup=main_menu_kb())
        return

    total_count = sum(len(msgs) for msgs in sender_groups.values())
    guest_count = len(sender_groups)
    header = (
        f"📬 <b>{total_count} پیام ناشناس خوانده‌نشده از {guest_count} نفر:</b>\n\n"
        "روی یک کاربر کلیک کنید تا پیام‌هایش را ببینید:"
    )
    kb = unread_inbox_kb(sender_groups)
    card = _card(callback)
    if card is None:
        await _fallback(callback, header, parse_mode="HTML", reply_markup=kb)
        return
    try:
        await card.edit_text(header, parse_mode="HTML", reply_markup=kb)
    except Exception:
        # edit_text failed (likely «message is not modified») — the list may be
        # unchanged while the buttons are stale, so refresh just the keyboard.
        try:
            await card.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await card.answer(header, parse_mode="HTML")


@router.callback_query(F.data == "inbox:back")
async def cb_inbox_back(callback: CallbackQuery) -> None:
    """Return to main menu from inbox."""
    card = _card(callback)
    if card is not None:
        try:
            await card.edit_text(
                "✅ بازگشت به منوی اصلی.", reply_markup=main_menu_kb()
            )
        except Exception:
            try:
                await card.edit_reply_markup(reply_markup=main_menu_kb())
            except Exception:
                pass
        await card.answer("منوی اصلی:")
    else:
        await _fallback(callback, "منوی اصلی:", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# 7. Block sender (legacy compatibility)
# ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("anon_block:"))
async def cb_anon_block(callback: CallbackQuery) -> None:
    """Block the anonymous message sender."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("⚠️ خطا در پردازش.", show_alert=True)
        return

    try:
        msg_id = int(parts[1])
        sender_id = int(parts[2])
    except (ValueError, TypeError):
        await callback.answer("⚠️ خطا در شناسه.", show_alert=True)
        return

    user_id = callback.from_user.id

    # All DB work finishes before the first await that touches Telegram: the
    # session must not sit open across a network call (it pins one of the five
    # pooled connections for as long as the API takes to answer).
    refusal: str | None = None
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonymousMessage).where(AnonymousMessage.id == msg_id)
        )
        msg = result.scalar_one_or_none()
        if msg is None or msg.receiver_id != user_id:
            refusal = "⚠️ پیام یافت نشد."
        else:
            existing = await session.execute(
                select(BlockList).where(
                    (BlockList.blocker_id == user_id)
                    & (BlockList.blocked_id == sender_id)
                )
            )
            if existing.scalar_one_or_none() is not None:
                refusal = "ℹ️ این کاربر قبلاً مسدود شده است."
            else:
                session.add(BlockList(blocker_id=user_id, blocked_id=sender_id))
                await session.commit()

    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return

    card = _card(callback)
    if card is not None:
        try:
            await card.edit_text(
                "🚫 <b>کاربر مسدود شد.</b>\n\n"
                "این کاربر دیگر نمی‌تواند به شما پیام بدهد.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await callback.answer("🚫 کاربر مسدود شد.", show_alert=True)
