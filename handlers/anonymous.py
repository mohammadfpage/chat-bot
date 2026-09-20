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

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func as sql_func

from database import async_session_factory, AnonymousMessage, BlockList
from keyboards import main_menu_kb, anonymous_chat_menu_kb, unread_inbox_kb
from states import AnonChatStates, ChatState

logger = logging.getLogger(__name__)
router = Router()

MAX_UNREAD_MESSAGES = 10


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

@router.message(F.text == "📬 پیام‌های ناشناس من")
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
        try:
            await callback.message.edit_text("📭 صندوق شما خالی است.")
        except Exception:
            pass
        await callback.message.answer(
            "منوی اصلی:",
            reply_markup=main_menu_kb(),
        )
        return

    # Mark all messages as read
    async with async_session_factory() as session:
        for msg in messages:
            msg.is_read = True
        await session.commit()

    # Send all unread messages to the owner
    await callback.message.edit_text(
        f"📩 <b>پیام‌های ناشناس از کاربر {sender_id}:</b>\n\n"
        "——————————————",
        parse_mode="HTML",
    )

    for msg in messages:
        await callback.message.answer(f"💬 {msg.content}")

    # Check if guest is currently online (in session with us)
    is_guest_online = await _is_partner_online(
        fsm_storage, callback.bot.id, sender_id, user_id
    )

    if is_guest_online:
        await callback.message.answer(
            "🟢 <b>مخاطب شما آنلاین است! پیام‌های شما مستقیماً ارسال می‌شوند.</b>",
            parse_mode="HTML",
            reply_markup=anonymous_chat_menu_kb(),
        )
    else:
        await callback.message.answer(
            "🔴 <b>مخاطب شما آفلاین است.</b>\n"
            "پیام‌های شما ذخیره شده و مخاطب پس از بازدید پاسخ خواهد داد.",
            parse_mode="HTML",
            reply_markup=anonymous_chat_menu_kb(),
        )

    # Enter anonymous session
    await state.set_state(AnonChatStates.in_session)
    await state.update_data(partner_id=sender_id, is_owner=True)


# ──────────────────────────────────────────────────
# 4. Message Router (Hybrid Engine)
# ──────────────────────────────────────────────────

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
        # Real-time routing: send directly
        try:
            await message.bot.send_message(partner_id, f"💬 {content}")
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
        # Offline: check unread count limit
        unread_count = await _get_unread_count(user_id, partner_id)

        if unread_count >= MAX_UNREAD_MESSAGES:
            await message.answer(
                "⚠️ شما ۱۰ پیام بی‌پاسخ ارسال کرده‌اید.\n"
                "لطفاً منتظر پاسخ بمانید.",
                reply_markup=anonymous_chat_menu_kb(),
            )
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
                    "🔴 <b>مخاطب شما چت را ترک کرد.</b>",
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
        try:
            await callback.message.edit_text("📭 صندوق شما خالی است.")
        except Exception:
            pass
        await callback.message.answer(
            "منوی اصلی:",
            reply_markup=main_menu_kb(),
        )
        return

    total_count = sum(len(msgs) for msgs in sender_groups.values())
    guest_count = len(sender_groups)
    try:
        await callback.message.edit_text(
            f"📬 <b>{total_count} پیام ناشناس خوانده‌نشده از {guest_count} نفر:</b>\n\n"
            "روی یک کاربر کلیک کنید تا پیام‌هایش را ببینید:",
            parse_mode="HTML",
            reply_markup=unread_inbox_kb(sender_groups),
        )
    except Exception:
        await callback.message.answer(
            f"📬 <b>{total_count} پیام ناشناس خوانده‌نشده:</b>",
            parse_mode="HTML",
            reply_markup=unread_inbox_kb(sender_groups),
        )


@router.callback_query(F.data == "inbox:back")
async def cb_inbox_back(callback: CallbackQuery) -> None:
    """Return to main menu from inbox."""
    try:
        await callback.message.edit_text("✅ بازگشت به منوی اصلی.")
    except Exception:
        pass
    await callback.message.answer(
        "منوی اصلی:",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# 7. Block sender (legacy compatibility)
# ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("anon_block:"))
async def cb_anon_block(callback: CallbackQuery) -> None:
    """Block the anonymous message sender."""
    from database import BlockList

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

    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonymousMessage).where(AnonymousMessage.id == msg_id)
        )
        msg = result.scalar_one_or_none()
        if msg is None or msg.receiver_id != user_id:
            await callback.answer("⚠️ پیام یافت نشد.", show_alert=True)
            return

        existing = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_id)
                & (BlockList.blocked_id == sender_id)
            )
        )
        if existing.scalar_one_or_none() is not None:
            await callback.answer("ℹ️ این کاربر قبلاً مسدود شده است.", show_alert=True)
            return

        session.add(BlockList(blocker_id=user_id, blocked_id=sender_id))
        await session.commit()

    try:
        await callback.message.edit_text(
            "🚫 <b>کاربر مسدود شد.</b>\n\n"
            "این کاربر دیگر نمی‌تواند به شما پیام بدهد.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.answer("🚫 کاربر مسدود شد.", show_alert=True)
