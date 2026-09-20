"""
Admin panel: live stats, broadcast, ban/unban users, and promote/demote admins.

Security:
  * A router-wide ``IsAdmin()`` filter makes the entire panel invisible and
    inaccessible to normal users.
  * Root-only capabilities (promote/demote admins) are additionally gated by
    ``IsRootAdmin()``.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import settings
from database import async_session_factory, User
from filters import IsAdmin, IsRootAdmin
from handlers.chat import pair_map, search_queue
from keyboards.admin import (
    admin_panel_kb,
    admin_stats_kb,
    broadcast_confirm_kb,
    admin_users_kb,
    admin_manage_admins_kb,
    admin_cancel_kb,
)
from states import AdminBroadcast, AdminPIS
from utils.emojis import get_plain_emoji

logger = logging.getLogger(__name__)
router = Router()

# The whole router is admin-only.
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _is_root(user_id: int) -> bool:
    """True for Root (Super) Admins from ``settings.admin_ids``."""
    return user_id in settings.admin_ids_list


async def _parse_user_id(text: str) -> int | None:
    """
    Parse a plain numeric Telegram ID (also tolerates a leading @username,
    returning None since we only accept numeric IDs here).

    Args:
        text: Raw user input.

    Returns:
        The integer ID, or ``None`` if invalid.
    """
    text = text.strip()
    return int(text) if text.isdigit() else None


async def _find_user(user_id: int) -> User | None:
    """Fetch a user row by telegram_id (or None)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        return result.scalar_one_or_none()


async def _edit_or_answer(callback: CallbackQuery, text: str, kb=None) -> None:
    """Edit the panel message, falling back to a fresh answer on failure."""
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def _invalid_id_response(what: str) -> str:
    return (
        f"{get_plain_emoji('warning')} <b>ورودی نامعتبر</b>\n\n"
        f"لطفاً یک <b>آیدی عددی</b> معتبر برای {what} وارد کنید.\n"
        "برای انصراف روی دکمه «لغو» بزنید."
    )


# ──────────────────────────────────────────────────────────────────────────
# Entry point — /admin command
# ──────────────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Show the admin panel."""
    is_root = _is_root(message.from_user.id)
    await message.answer(
        "👑 <b>پنل مدیریت</b>\n\n"
        f"{get_plain_emoji('info')} خوش آمدید. از منوی زیر انتخاب کنید:"
        + ("\n\n⭐ شما ریشه‌ادمین هستید." if is_root else ""),
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=is_root),
    )


# ──────────────────────────────────────────────────────────────────────────
# Navigation / back to panel
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:panel")
async def cb_back_to_panel(callback: CallbackQuery) -> None:
    """Return to the main admin panel."""
    is_root = _is_root(callback.from_user.id)
    await _edit_or_answer(
        callback,
        "👑 <b>پنل مدیریت</b>\n\nاز منوی زیر انتخاب کنید:",
        admin_panel_kb(is_root=is_root),
    )


@router.callback_query(F.data == "admin:cancel_input")
async def cb_cancel_input(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel any active admin input flow."""
    await state.clear()
    is_root = _is_root(callback.from_user.id)
    await _edit_or_answer(
        callback,
        "❌ عملیات لغو شد.\n\n👑 <b>پنل مدیریت</b>",
        admin_panel_kb(is_root=is_root),
    )


# ──────────────────────────────────────────────────────────────────────────
# 1. Live Stats
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery) -> None:
    """Show live bot statistics."""
    async with async_session_factory() as session:
        total_users = await session.scalar(select(func.count(User.id))) or 0
        banned_users = await session.scalar(
            select(func.count(User.id)).where(User.is_banned.is_(True))
        ) or 0
        profile_complete = await session.scalar(
            select(func.count(User.id)).where(User.is_profile_complete.is_(True))
        ) or 0
        db_admins = await session.scalar(
            select(func.count(User.id)).where(User.is_admin.is_(True))
        ) or 0

    total_admins = db_admins + len(settings.admin_ids_list)
    active_chats = len(pair_map) // 2
    queue_size = len(search_queue)

    text = (
        "📊 <b>آمار زنده ربات</b>\n\n"
        f"{get_plain_emoji('users')} کل کاربران: <b>{total_users}</b>\n"
        f"{get_plain_emoji('check')} پروفایل کامل: <b>{profile_complete}</b>\n"
        f"{get_plain_emoji('ban')} بلاک‌شده: <b>{banned_users}</b>\n"
        f"{get_plain_emoji('crown')} ادمین‌ها: <b>{total_admins}</b>\n"
        f"{get_plain_emoji('chat')} چت‌های فعال: <b>{active_chats}</b>\n"
        f"{get_plain_emoji('queue')} در صف جستجو: <b>{queue_size}</b>"
    )
    await _edit_or_answer(callback, text, admin_stats_kb())


# ──────────────────────────────────────────────────────────────────────────
# 2. Broadcast (with confirmation)
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask the admin to send the message content."""
    await state.set_state(AdminBroadcast.waiting_for_message)
    await state.set_data({})
    await _edit_or_answer(
        callback,
        "📢 <b>ارسال پیام همگانی</b>\n\n"
        "اکنون پیام مورد نظر را ارسال کنید:\n"
        "(متن، عکس یا ویدیو)\n\n"
        "پس از آن، یک مرحله <b>تأیید</b> نمایش داده می‌شود.",
        admin_cancel_kb(),
    )


@router.callback_query(F.data == "admin:broadcast:confirm")
async def cb_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Execute the broadcast after confirmation."""
    data = await state.get_data()
    from_chat_id = data.get("from_chat_id")
    message_id = data.get("message_id")

    if not from_chat_id or not message_id:
        await callback.answer("⚠️ پیامی برای ارسال وجود ندارد.", show_alert=True)
        await state.clear()
        return

    await state.clear()
    await callback.answer("📤 در حال ارسال...")

    async with async_session_factory() as session:
        result = await session.execute(
            select(User.telegram_id).where(User.is_banned.is_(False))
        )
        user_ids = [row[0] for row in result.all()]

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await callback.bot.copy_message(
                chat_id=uid,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # flood-limit friendly

    await callback.message.answer(
        f"📢 <b>گزارش ارسال همگانی</b>\n\n"
        f"{get_plain_emoji('check')} موفق: <b>{sent}</b>\n"
        f"{get_plain_emoji('cross')} ناموفق: <b>{failed}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:broadcast:cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel the broadcast."""
    await state.clear()
    is_root = _is_root(callback.from_user.id)
    await _edit_or_answer(
        callback,
        "❌ ارسال همگانی لغو شد.\n\n👑 <b>پنل مدیریت</b>",
        admin_panel_kb(is_root=is_root),
    )


@router.message(AdminBroadcast.waiting_for_message)
async def msg_broadcast_capture(message: Message, state: FSMContext) -> None:
    """
    Capture the broadcast content (text/photo/video) and ask for confirmation.
    """
    if message.content_type not in ("text", "photo", "video"):
        await message.answer(
            f"{get_plain_emoji('warning')} فقط متن، عکس یا ویدیو پشتیبانی می‌شود.",
            parse_mode="HTML",
        )
        return

    await state.update_data(
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    await state.set_state(AdminBroadcast.confirm_send)

    preview = message.caption or message.text or "پیام شما"
    await message.answer(
        f"📢 <b>تأیید ارسال همگانی</b>\n\n"
        f"این پیام به <b>همه کاربران</b> ارسال می‌شود:\n"
        f"——————————————\n{preview}\n——————————————\n\n"
        "آیا مطمئن هستید؟",
        parse_mode="HTML",
        reply_markup=broadcast_confirm_kb(),
    )


# ──────────────────────────────────────────────────────────────────────────
# 3. User management (ban / unban)
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users")
async def cb_users_menu(callback: CallbackQuery) -> None:
    """Show ban/unban sub-menu."""
    await _edit_or_answer(
        callback,
        "👥 <b>مدیریت کاربران</b>\n\n"
        "آیدی عددی تلگرام کاربر را وارد کنید.",
        admin_users_kb(),
    )


@router.callback_query(F.data == "admin:user:ban")
async def cb_ban_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the ID to ban."""
    await state.set_state(AdminPIS.waiting_for_ban_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('ban')} <b>بلاک‌کردن کاربر</b>\n\n"
        "آیدی عددی تلگرام کاربر را ارسال کنید:",
        admin_cancel_kb(),
    )


@router.callback_query(F.data == "admin:user:unban")
async def cb_unban_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the ID to unban."""
    await state.set_state(AdminPIS.waiting_for_unban_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('unban')} <b>رفع بلاک کاربر</b>\n\n"
        "آیدی عددی تلگرام کاربر را ارسال کنید:",
        admin_cancel_kb(),
    )


@router.message(AdminPIS.waiting_for_ban_id)
async def msg_ban_user(message: Message, state: FSMContext) -> None:
    """Ban the user whose numeric ID was provided."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("بلاک‌کردن"))
        return

    if target_id in settings.admin_ids_list:
        await message.answer("⛔ ریشه‌ادمین قابل بلاک نیست.")
        await state.clear()
        return

    target = await _find_user(target_id)
    if target is None:
        await message.answer(f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> یافت نشد.")
        await state.clear()
        return

    if target.is_admin:
        await message.answer("⛔ این کاربر ادمین است؛ ابتدا برکناری را بررسی کنید.")
        await state.clear()
        return

    async with async_session_factory() as session:
        target = await session.merge(target)
        target.is_banned = True
        await session.commit()

    await message.answer(
        f"{get_plain_emoji('ban')} کاربر <code>{target_id}</code> بلاک شد.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)),
    )
    await state.clear()


@router.message(AdminPIS.waiting_for_unban_id)
async def msg_unban_user(message: Message, state: FSMContext) -> None:
    """Unban the user whose numeric ID was provided."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("رفع بلاک"))
        return

    target = await _find_user(target_id)
    if target is None:
        await message.answer(f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> یافت نشد.")
        await state.clear()
        return

    async with async_session_factory() as session:
        target = await session.merge(target)
        target.is_banned = False
        await session.commit()

    await message.answer(
        f"{get_plain_emoji('unban')} بلاک کاربر <code>{target_id}</code> برداشته شد.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)),
    )
    await state.clear()


# ──────────────────────────────────────────────────────────────────────────
# 4. Root-only: manage admins
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:admins", IsRootAdmin())
async def cb_manage_admins(callback: CallbackQuery) -> None:
    """Root-only menu to promote/demote admins."""
    await _edit_or_answer(
        callback,
        "👑 <b>مدیریت ادمین‌ها</b>\n\n"
        "می‌توانید یک کاربر را ارتقا یا برکنار کنید.",
        admin_manage_admins_kb(),
    )


@router.callback_query(F.data == "admin:admins:list", IsRootAdmin())
async def cb_admin_list(callback: CallbackQuery) -> None:
    """List all admins (root + promoted)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.is_admin.is_(True))
        )
        db_admins = result.scalars().all()

    lines = [
        f"{get_plain_emoji('crown')} <b>ریشه‌ادمین‌ها:</b>",
        *(f"• <code>{aid}</code>" for aid in settings.admin_ids_list),
        "",
        f"{get_plain_emoji('users')} <b>ادمین‌های ارتقاییافته:</b>",
    ]
    if db_admins:
        for u in db_admins:
            lines.append(f"• <code>{u.telegram_id}</code> ({u.first_name or '—'})")
    else:
        lines.append("• (هیچ‌کدام)")

    await _edit_or_answer(callback, "\n".join(lines), admin_manage_admins_kb())


@router.callback_query(F.data == "admin:admins:promote", IsRootAdmin())
async def cb_promote_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the ID to promote."""
    await state.set_state(AdminPIS.waiting_for_promote_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('check')} <b>ارتقا به ادمین</b>\n\n"
        f"آیدی عددی تلگرام کاربر را ارسال کنید:",
        admin_cancel_kb(),
    )


@router.callback_query(F.data == "admin:admins:demote", IsRootAdmin())
async def cb_demote_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the ID to demote."""
    await state.set_state(AdminPIS.waiting_for_demote_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('cross')} <b>برکناری ادمین</b>\n\n"
        f"آیدی عددی تلگرام ادمین موردنظر را ارسال کنید:",
        admin_cancel_kb(),
    )


@router.message(AdminPIS.waiting_for_promote_id, IsRootAdmin())
async def msg_promote(message: Message, state: FSMContext) -> None:
    """Promote the user to admin."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("ارتقا"))
        return

    if target_id in settings.admin_ids_list:
        await message.answer("⭐ این کاربر از قبل ریشه‌ادمین است.")
        await state.clear()
        return

    target = await _find_user(target_id)
    if target is None:
        await message.answer(
            f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> هنوز ربات را استارت نزده است.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    async with async_session_factory() as session:
        target = await session.merge(target)
        target.is_admin = True
        await session.commit()

    await message.answer(
        f"{get_plain_emoji('success')} کاربر <code>{target_id}</code> ادمین شد.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=True),
    )
    await state.clear()


@router.message(AdminPIS.waiting_for_demote_id, IsRootAdmin())
async def msg_demote(message: Message, state: FSMContext) -> None:
    """Demote an admin (root admins cannot be demoted)."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("برکناری"))
        return

    if target_id in settings.admin_ids_list:
        await message.answer("⛔ ریشه‌ادمین را نمی‌توان برکنار کرد.")
        await state.clear()
        return

    target = await _find_user(target_id)
    if target is None or not target.is_admin:
        await message.answer(
            f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> ادمین نیست.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    async with async_session_factory() as session:
        target = await session.merge(target)
        target.is_admin = False
        await session.commit()

    await message.answer(
        f"{get_plain_emoji('success')} ادمین <code>{target_id}</code> برکنار شد.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=True),
    )
    await state.clear()
