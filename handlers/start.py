"""
/start command, deep link handling, admin dual-panel, and help/rules.

Centralized /start handler:
  - Registers new users / checks bans
  - Deep link payload → FSM state for anonymous message
  - Admin dual-panel (👑 Admin Panel / 👤 User Panel)
  - Regular user main menu
"""

import logging

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import settings
from database import async_session_factory, User
from keyboards import main_menu_kb, admin_dual_panel_kb, admin_panel_kb
from states import ChatState, AnonymousStates, AnonChatStates

logger = logging.getLogger(__name__)
router = Router()


# ──────────────────────────────────────────────────
# /start command — centralized entry point
# ──────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Register new users, handle deep links, show admin dual-panel or main menu."""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else None

    # ── Register / check banned ──
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            session.add(
                User(
                    telegram_id=user_id,
                    first_name=message.from_user.first_name,
                    username=message.from_user.username,
                )
            )
            await session.commit()
        elif user.is_banned:
            await message.answer("⛔ شما از ربات محروم شده‌اید.")
            return

    # ── Deep link handling (takes priority) ──
    if payload:
        try:
            owner_id = int(payload)
        except (ValueError, TypeError):
            owner_id = None

        if owner_id and owner_id != user_id:
            await _handle_deep_link(message, state, user_id, owner_id)
            return
        elif owner_id and owner_id == user_id:
            await message.answer(
                "⚠️ شما نمی‌توانید به خودتان پیام ناشناس بفرستید!",
                reply_markup=main_menu_kb(),
            )
            return

    await state.set_state(ChatState.idle)

    # ── Admin dual-panel ──
    if int(user_id) in settings.admin_ids_list:
        await message.answer(
            f"سلام {message.from_user.first_name}! 👋\n"
            "شما به عنوان مدیر وارد شدید.\n\n"
            "از پنل زیر انتخاب کنید:",
            reply_markup=admin_dual_panel_kb(),
        )
        return

    # ── Regular user ──
    await message.answer(
        f"سلام {message.from_user.first_name}! 👋\n"
        "به ربات چت ناشناس خوش آمدید.\n\n"
        "از منوی زیر استفاده کنید:",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Deep link handler — set FSM state for message
# ──────────────────────────────────────────────────

async def _handle_deep_link(
    message: Message, state: FSMContext, guest_id: int, owner_id: int
) -> None:
    """Validate the deep link and enter hybrid anonymous chat session.
    
    The guest enters AnonChatStates.in_session connected to the owner.
    """
    # Verify the link owner exists
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == owner_id)
        )
        owner = result.scalar_one_or_none()

    if owner is None:
        await message.answer(
            "⚠️ صاحب لینک هنوز ربات را استارت نکرده است.\n"
            "لطفاً بعداً دوباره تلاش کنید.",
            reply_markup=main_menu_kb(),
        )
        return

    # Import here to avoid circular imports
    from handlers.anonymous import handle_deep_link_anon
    await handle_deep_link_anon(message, state, guest_id, owner_id)


# ──────────────────────────────────────────────────
# Admin dual-panel button handlers
# ──────────────────────────────────────────────────

@router.message(F.text == "👑 پنل مدیریت")
async def cb_admin_panel_entry(message: Message) -> None:
    """Show admin panel when admin clicks '👑 پنل مدیریت'."""
    if int(message.from_user.id) not in settings.admin_ids_list:
        return
    is_root = int(message.from_user.id) in settings.admin_ids_list
    await message.answer(
        "👑 <b>پنل مدیریت</b>\n\n"
        "خوش آمدید. از منوی زیر انتخاب کنید:"
        + ("\n\n⭐ شما ریشه‌ادمین هستید." if is_root else ""),
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=is_root),
    )


@router.message(F.text == "👤 پنل کاربری")
async def cb_user_panel_entry(message: Message, state: FSMContext) -> None:
    """Show regular main menu when admin clicks '👤 پنل کاربری'."""
    await state.set_state(ChatState.idle)
    await message.answer(
        "منوی اصلی:",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Forced-join membership verification (callback)
# ──────────────────────────────────────────────────

@router.callback_query(F.data == "check_membership")
async def check_membership(callback: CallbackQuery, state: FSMContext) -> None:
    """Verify the user joined the channel after tapping 'عضو شدم'."""
    channel_username = settings.channel_username.strip().lstrip("@")
    channel_id = settings.channel_id
    if not channel_username and not channel_id:
        await callback.answer("ℹ️ عضویت بررسی نمی‌شود.", show_alert=True)
        return

    chat_ref: str | int = channel_id if channel_id else f"@{channel_username}"
    try:
        member = await callback.bot.get_chat_member(
            chat_id=chat_ref,
            user_id=callback.from_user.id,
        )
        is_member = member.status not in ("left", "kicked")
    except Exception:
        is_member = False

    if not is_member:
        await callback.answer(
            "❌ شما هنوز عضو کانال نیستید!\nلطفاً ابتدا عضو شوید.",
            show_alert=True,
        )
        return

    await callback.answer("✅ عضویت تایید شد!")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.set_state(ChatState.idle)
    await callback.message.answer(
        "✅ عضویت شما تأیید شد!\n"
        "خوش آمدید! 👋\n\n"
        "از منوی زیر استفاده کنید:",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Help / Rules
# ──────────────────────────────────────────────────

@router.message(F.text == "📋 راهنما و قوانین")
async def cmd_help(message: Message) -> None:
    """Display rules and instructions."""
    await message.answer(
        "📖 <b>قوانین و راهنمای ربات چت ناشناس</b>\n\n"
        "1️⃣ قبل از شروع چت، پروفایل خود را تکمیل کنید.\n"
        "2️⃣ فقط با یک نفر همزمان می‌توانید چت کنید.\n"
        "3️⃣ ارسال فیلم و ویدیو مجاز نیست 🚫\n"
        "4️⃣ متن، صدا و عکس مجاز هستند.\n"
        "5️⃣ در صورت تخلف، کاربر گزارش و بلاک می‌شود.\n"
        "6️⃣ هویت شما ناشناس باقی می‌ماند.\n"
        "7️⃣ از لینک ناشناس برای ارسال پیام ناشناس استفاده کنید.\n\n"
        "از چت لذت ببرید! 😊",
        parse_mode="HTML",
    )
