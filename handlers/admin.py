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
import random
from datetime import datetime, timedelta
from html import escape

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select

from config import settings
from scripts.backup_db import run_backup
from database import (
    async_session_factory,
    BotPolicy,
    CoinTransaction,
    RequiredChannel,
    User,
    UserReport,
    Whisper,
    WhisperConfig,
)
from filters import IsAdmin, IsRootAdmin
from handlers.chat import pair_map, search_queue
from keyboards import main_menu_kb
from keyboards.admin import (
    admin_panel_kb,
    admin_stats_kb,
    broadcast_confirm_kb,
    admin_users_kb,
    admin_user_flags_kb,
    admin_manage_admins_kb,
    admin_cancel_kb,
    admin_costs_kb,
    admin_costs_edit_kb,
    admin_limits_kb,
    admin_rewards_kb,
    admin_report_kb,
    admin_gift_kb,
    admin_gift_scope_kb,
    admin_gift_confirm_kb,
    admin_whisper_kb,
    admin_forcejoin_kb,
    admin_forcejoin_channels_kb,
    policy_field_from_suffix,
    policy_field_label,
    policy_field_emoji,
    policy_section_of,
)
from states import (
    AdminBroadcast,
    AdminPIS,
    AdminGift,
    AdminPolicyEdit,
    AdminReport,
    AdminWhisper,
)
from utils.economy import (
    get_policy,
    refresh_policy,
    add_coins,
    add_premium_days,
    bulk_add_coins,
    bulk_add_premium_days,
    fmt_coins,
    parse_amount,
    parse_int,
    round_coins,
    reason_label,
)
from utils.emojis import get_plain_emoji
from utils.group_admin import bot_is_admin
from utils.membership import clear_membership_cache, resolve_chat_ref
from utils.whisper_config import get_whisper_config, refresh_whisper_config

logger = logging.getLogger(__name__)
router = Router()

# The whole router is admin-only.
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# …and private-chat only: the panel must never be reachable from a group.
router.message.filter(F.chat.type == ChatType.PRIVATE)


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
    """Show the admin panel.

    The welcome line names the three screens admins reach for most — prices,
    rewards, reports — so a first-time admin does not have to open each one to
    find out what it holds.
    """
    is_root = _is_root(message.from_user.id)
    await message.answer(
        "👑 <b>پنل مدیریت</b>\n\n"
        f"{get_plain_emoji('info')} خوش آمدید. از منوی زیر انتخاب کنید:\n"
        f"\n{get_plain_emoji('coins')} هزینهٔ سرویس‌ها، پاداش‌ها و جوایز "
        f"— داینامیک و فوری\n"
        f"{get_plain_emoji('chart')} گزارش اینکه هر کاربر چند سکه گرفته\n"
        + (f"\n⭐ شما ریشه‌ادمین هستید." if is_root else ""),
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
        "👑 <b>پنل مدیریت</b>\n\n"
        f"{get_plain_emoji('info')} از منوی زیر انتخاب کنید:",
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
# 1b. Backup (root only)
# ──────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:backup", IsRootAdmin())
async def cb_backup(callback: CallbackQuery) -> None:
    """Snapshot ``database.db`` and hand the file to the root admin's PM.

    The sqlite3 backup API blocks, so it runs in a thread; the upload goes to
    the *caller's private chat*, never to wherever the panel happens to be
    open — a panel opened in a group must not drop a full user dump there.
    """
    # The whole script is built on sqlite3 against a fixed database.db path.
    # On PostgreSQL it would either fail confusingly — or, far worse, happily
    # copy a STALE database.db left over from the SQLite days and present it
    # as the current backup. Real backups there need pg_dump.
    if settings.database_dialect != "sqlite":
        await callback.answer(
            "ℹ️ پشتیبان‌گیری این دکمه فقط برای SQLite است؛ دیتابیس فعلی "
            "PostgreSQL است. با pg_dump پشتیبان بگیرید.",
            show_alert=True,
        )
        return
    await callback.answer("⏳ در حال ساخت پشتیبان…")
    try:
        path = await asyncio.to_thread(run_backup)
    except Exception as exc:
        logger.warning("Backup failed: %s", exc)
        await callback.answer(
            "❌ پشتیبان‌گیری ناموفق بود؛ لاگ را ببینید.", show_alert=True
        )
        return

    size_kb = path.stat().st_size // 1024
    try:
        await callback.bot.send_document(
            chat_id=callback.from_user.id,
            document=FSInputFile(path),
            caption=(
                f"{get_plain_emoji('database')} <b>پشتیبان دیتابیس</b>\n"
                f"📏 حجم: {size_kb} KiB\n"
                f"🗓 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        # The snapshot exists on disk either way — report where it is rather
        # than crying failure over an upload the bot cannot complete.
        logger.info("Backup created but upload failed: %s", exc)
        await callback.answer(
            f"✅ پشتیبان ساخته شد ({size_kb} KiB)؛ ارسال فایل ممکن نبود.",
            show_alert=True,
        )
        return
    await callback.answer("✅ پشتیبان ساخته و ارسال شد.")


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
        f"——————————————\n{escape(preview)}\n——————————————\n\n"
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

    # A later re-ban must be able to notify again (see BlockBannedMiddleware).
    from middleware.force_join import forget_ban_notice

    forget_ban_notice(target_id)

    await message.answer(
        f"{get_plain_emoji('unban')} بلاک کاربر <code>{target_id}</code> برداشته شد.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)),
    )
    await state.clear()


# ──────────────────────────────────────────────────────────────────────────
# 3b. Special flags (VIP / subscription / exempt) + report inbox
# ──────────────────────────────────────────────────────────────────────────

def _flags_card(user: User) -> str:
    """Status card for one user's paywall flags (see ``User`` model)."""
    def onoff(value: bool) -> str:
        return "فعال" if value else "غیرفعال"

    premium_line = ""
    if user.premium_until:
        # Stored naive UTC; the panel reads Tehran time (+3:30).
        tehran = user.premium_until + timedelta(hours=3, minutes=30)
        premium_line = f"\n📅 تا: <code>{tehran.strftime('%Y-%m-%d %H:%M')}</code>"

    return (
        "⭐ <b>وضعیت ویژهٔ کاربر</b>\n\n"
        f"🆔 <code>{user.telegram_id}</code> — {escape(user.first_name or '—')}\n"
        f"⭐ اشتراک ویژه: <b>{onoff(user.is_vip)}</b>{premium_line}\n"
        f"💳 اشتراک خریداری‌شده: <b>{onoff(user.has_subscription)}</b>\n"
        f"💲 معاف از هزینهٔ اتصال: <b>{onoff(user.is_exempt)}</b>\n"
        f"🪙 موجودی: <b>{fmt_coins(user.coins)}</b>\n\n"
        "<i>تغییرات فوراً اعمال می‌شود.</i>"
    )


@router.callback_query(F.data == "admin:user:flags")
async def cb_flags_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask which user's paywall flags to edit."""
    await state.set_state(AdminPIS.waiting_for_flags_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('crown')} <b>وضعیت ویژهٔ کاربر</b>\n\n"
        "آیدی عددی کاربر را بفرستید:",
        admin_cancel_kb(),
    )


@router.message(AdminPIS.waiting_for_flags_id)
async def msg_flags_target(message: Message, state: FSMContext) -> None:
    """Show the flag card for the requested user (state kept on a miss, for a retry)."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("وضعیت ویژه"))
        return

    user = await _find_user(target_id)
    if user is None:
        await message.answer(
            f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> یافت نشد.",
            parse_mode="HTML",
        )
        return

    await state.clear()
    await message.answer(
        _flags_card(user),
        parse_mode="HTML",
        reply_markup=admin_user_flags_kb(
            target_id,
            is_vip=user.is_vip,
            has_subscription=user.has_subscription,
            is_exempt=user.is_exempt,
        ),
    )


@router.callback_query(F.data.startswith("admin:flags:toggle:"))
async def cb_flags_toggle(callback: CallbackQuery) -> None:
    """Flip one flag on one user and re-render the card.

    The target id travels in the callback data, not in FSM state: these
    buttons keep working after any state clear, and a state-held id could
    retoggle a different user after the admin ran another flow.
    """
    parts = callback.data.split(":")  # admin : flags : toggle : <field> : <id>
    if len(parts) != 5:
        await callback.answer("⚠️ فیلد نامعتبر.", show_alert=True)
        return
    field = parts[3]
    if field not in ("is_vip", "has_subscription", "is_exempt"):
        await callback.answer("⚠️ فیلد نامعتبر.", show_alert=True)
        return
    try:
        target_id = int(parts[4])
    except ValueError:
        await callback.answer("⚠️ شناسه نامعتبر.", show_alert=True)
        return

    target = await _find_user(target_id)
    if target is None:
        await callback.answer("کاربر یافت نشد.", show_alert=True)
        return

    async with async_session_factory() as session:
        target = await session.merge(target)
        setattr(target, field, not getattr(target, field))
        await session.commit()

    await callback.answer("✅ ثبت شد.", show_alert=True)
    await _edit_or_answer(
        callback,
        _flags_card(target),
        admin_user_flags_kb(
            target_id,
            is_vip=target.is_vip,
            has_subscription=target.has_subscription,
            is_exempt=target.is_exempt,
        ),
    )


@router.callback_query(F.data == "admin:reports")
async def cb_reports_inbox(callback: CallbackQuery) -> None:
    """The last ten block reports — persistent, unlike the PM notifications."""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(UserReport)
                .order_by(UserReport.created_at.desc(), UserReport.id.desc())
                .limit(10)
            )
        ).scalars().all()

    if not rows:
        await _edit_or_answer(
            callback,
            "📥 <b>ورودی گزارش‌ها</b>\n\nهنوز گزارشی ثبت نشده است.",
            admin_users_kb(),
        )
        return

    # The ledger stores naive UTC; the panel reads Tehran time (+3:30).
    tehran = timedelta(hours=3, minutes=30)
    lines = ["📥 <b>ورودی گزارش‌ها</b> (۱۰ مورد آخر)\n"]
    for r in rows:
        when = (r.created_at + tehran).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"• <code>{r.reporter_id}</code> طرف <code>{r.reported_id}</code> را "
            f"گزارش کرد · <code>{when}</code>"
        )
    lines.append("\nبرای گزارش کامل سکه، آیدی را در «📈 گزارش سکه» جستجو کنید.")
    await _edit_or_answer(callback, "\n".join(lines), admin_users_kb())


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
            lines.append(
                f"• <code>{u.telegram_id}</code> "
                f"({escape(u.first_name) if u.first_name else '—'})"
            )
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

# ──────────────────────────────────────────────────────────────────────────
# 5. Economy settings — what costs, what pays out, what is limited
# ──────────────────────────────────────────────────────────────────────────
#
# Three screens instead of one wall of numbers. All three write the same
# ``bot_policy`` row, and all of it is live: the next match, the next whisper
# and the next referral read whatever is stored here, with no restart.

def _free_or_price(value) -> str:
    """«رایگان» for 0 — a free service must not render as «0 سکه»."""
    value = value or 0
    return "رایگان" if value <= 0 else f"<b>{fmt_coins(value)} سکه</b>"


def _costs_text(policy: BotPolicy) -> str:
    """The per-service price card — the screen an admin opens to make a
    feature free or paid."""
    return (
        f"{get_plain_emoji('coins')} <b>هزینهٔ سرویس‌ها</b>\n\n"
        "هر سرویس یا رایگان است یا با سکه؛ «رایگان» یعنی عدد صفر.\n"
        "عدد اعشاری هم می‌شود (مثلاً ۲٫۵ سکه).\n"
        "تغییرات <b>فوری</b> اعمال می‌شود.\n\n"
        f"{get_plain_emoji('dice')} اتصال شانسی: {_free_or_price(policy.random_chat_cost)}\n"
        f"{get_plain_emoji('female')} چت با دختر: {_free_or_price(policy.chat_girl_cost)}\n"
        f"{get_plain_emoji('male')} چت با پسر: {_free_or_price(policy.chat_boy_cost)}\n"
        f"{get_plain_emoji('locked')} نجوا: {_free_or_price(policy.whisper_cost)}\n\n"
        f"{get_plain_emoji('info')} اتصال شانسی و چت‌ها فقط هنگام <b>وصل‌شدن</b> "
        "حساب می‌شود؛ نجوا به ازای هر پیام.\n"
        f"{get_plain_emoji('premium')} کاربران اشتراکی و معاف همیشه رایگان‌اند."
    )


def _rewards_text(policy: BotPolicy) -> str:
    """The payout card: every number the bot ever hands to a user."""
    return (
        f"{get_plain_emoji('gift')} <b>پاداش‌ها و جوایز</b>\n\n"
        "هر عددی که اینجا ثبت شود همان است که به کاربر پرداخت می‌شود:\n\n"
        f"{get_plain_emoji('coins')} سکهٔ خوش‌آمد: <b>{policy.welcome_coins}</b> سکه\n"
        f"{get_plain_emoji('calendar')} جایزهٔ روزانه: <b>{policy.daily_bonus_coins}</b> سکه (هر ۲۴ ساعت)\n"
        f"{get_plain_emoji('gift')} پاداش رفرال: <b>{policy.referral_coin_reward}</b> سکه\n"
        f"{get_plain_emoji('premium')} روز اشتراک رفرال: <b>{policy.referral_premium_days}</b> روز\n"
        f"{get_plain_emoji('users')} هدیهٔ دعوت‌شده: <b>{policy.referral_invitee_coins}</b> سکه\n\n"
        f"{get_plain_emoji('chart')} گزارش اینکه هر کاربر چند سکه گرفته، "
        "در «📈 گزارش سکه و رفرال» است."
    )


def _limits_text(policy: BotPolicy) -> str:
    """Anti-spam caps and chat rules — the section that never charges."""
    status = "فعال" if policy.enabled else "غیرفعال"
    return (
        f"{get_plain_emoji('blocked')} <b>ضداسپم و محدودیت‌ها</b>\n\n"
        f"{get_plain_emoji('gear')} وضعیت محدودیت: <b>{status}</b>\n"
        f"{get_plain_emoji('fire')} پیام در دقیقه: <b>{policy.messages_per_minute}</b>\n"
        f"{get_plain_emoji('clock')} پیام در ساعت: <b>{policy.messages_per_hour}</b>\n"
        f"{get_plain_emoji('premium')} ضریب کاربر ویژه: <b>{policy.vip_multiplier}x</b>\n"
        f"{get_plain_emoji('calendar')} مدت مجاز هر چت: <b>{policy.chat_lifetime_hours} ساعت</b>\n\n"
        f"{get_plain_emoji('info')} در این بخش هیچ سکه‌ای کسر نمی‌شود؛\n"
        "پیامِ سریع فقط هشدار می‌گیرد و ارسال نمی‌شود.\n"
        f"هزینهٔ سرویس‌ها از «{get_plain_emoji('coins')} هزینهٔ سرویس‌ها» تنظیم می‌شود."
    )


async def _show_section(callback: CallbackQuery, section: str) -> None:
    """Render one of the three policy screens (the default is anti-spam)."""
    policy = await get_policy()
    if section == "costs":
        await _edit_or_answer(callback, _costs_text(policy), admin_costs_kb(policy))
    elif section == "rewards":
        await _edit_or_answer(callback, _rewards_text(policy), admin_rewards_kb(policy))
    else:
        await _edit_or_answer(callback, _limits_text(policy), admin_limits_kb(policy))


async def _set_policy_field(field_name: str, value: int | float) -> None:
    """Write one ``bot_policy`` column and flush the cache that reads it."""
    async with async_session_factory() as session:
        result = await session.execute(select(BotPolicy).limit(1))
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = BotPolicy()
            session.add(policy)
        setattr(policy, field_name, value)
        await session.commit()
    await refresh_policy()


@router.callback_query(F.data == "admin:costs")
async def cb_costs(callback: CallbackQuery) -> None:
    """Per-service prices: free or paid, and how much."""
    await _show_section(callback, "costs")


@router.callback_query(F.data == "admin:rewards")
async def cb_rewards(callback: CallbackQuery) -> None:
    """What the bot pays out (welcome / daily / referral)."""
    await _show_section(callback, "rewards")


@router.callback_query(F.data == "admin:limits")
async def cb_limits(callback: CallbackQuery) -> None:
    """Anti-spam caps and chat rules."""
    await _show_section(callback, "limits")


@router.callback_query(F.data == "admin:limits:toggle")
async def cb_limits_toggle(callback: CallbackQuery) -> None:
    """Enable/disable rate limiting globally."""
    async with async_session_factory() as session:
        result = await session.execute(select(BotPolicy).limit(1))
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = BotPolicy()
            session.add(policy)
        policy.enabled = not policy.enabled
        await session.commit()

    await refresh_policy()
    await cb_limits(callback)


async def _begin_policy_edit(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    section: str,
    suffix: str,
) -> None:
    """Ask for a new value, remembering which screen to return to."""
    field_name = policy_field_from_suffix(suffix)
    if field_name is None:
        await callback.answer("⚠️ فیلد نامعتبر.", show_alert=True)
        return

    policy = await get_policy()
    current = getattr(policy, field_name, 0)
    await state.set_state(AdminPolicyEdit.waiting_for_value)
    await state.update_data(field=field_name, section=section)

    label = policy_field_label(field_name)
    emoji = get_plain_emoji(policy_field_emoji(field_name))
    if section == "costs":
        hint = (
            "<i>برای رایگان کردن، ۰ بفرستید یا دکمهٔ سبز را بزنید.</i>\n"
            "<i>عدد اعشاری هم می‌شود، مثلاً ۲٫۵ یا 0.5</i>"
        )
        current_text = fmt_coins(current)
    else:
        hint = "<i>عدد صحیح مثبت بفرستید؛ ۰ یعنی غیرفعال.</i>"
        current_text = str(current or 0)
    await _edit_or_answer(
        callback,
        f"{emoji} <b>ویرایش: {label}</b>\n\n"
        f"مقدار فعلی: <b>{current_text}</b>\n\n"
        f"مقدار جدید را بفرستید:\n{hint}",
        admin_costs_edit_kb(suffix) if section == "costs" else admin_cancel_kb(),
    )


@router.callback_query(F.data.startswith("admin:costs:edit:"))
async def cb_cost_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Edit the price of one service (اتصال شانسی / دختر / پسر / نجوا)."""
    await _begin_policy_edit(
        callback, state, section="costs", suffix=callback.data.split(":", 3)[3]
    )


@router.callback_query(F.data.startswith("admin:rewards:edit:"))
async def cb_reward_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Edit one payout number."""
    await _begin_policy_edit(
        callback, state, section="rewards", suffix=callback.data.split(":", 3)[3]
    )


@router.callback_query(F.data.startswith("admin:limits:edit:"))
async def cb_limit_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Edit one rate limit / chat rule."""
    await _begin_policy_edit(
        callback, state, section="limits", suffix=callback.data.split(":", 3)[3]
    )


@router.callback_query(F.data.startswith("admin:costs:free:"))
async def cb_cost_make_free(callback: CallbackQuery, state: FSMContext) -> None:
    """One tap to make a service free — no number to type.

    The same write typing ``0`` performs, because the admin who opened this
    screen to say «رایگان» should not have to know that 0 means it.
    """
    suffix = callback.data.split(":", 3)[3]
    field_name = policy_field_from_suffix(suffix)
    if field_name is None:
        await callback.answer("⚠️ فیلد نامعتبر.", show_alert=True)
        return

    await state.clear()
    await _set_policy_field(field_name, 0)
    label = policy_field_label(field_name)
    await callback.answer(f"✅ «{label}» رایگان شد.", show_alert=True)
    await _show_section(callback, "costs")


@router.message(AdminPolicyEdit.waiting_for_value)
async def msg_policy_value(message: Message, state: FSMContext) -> None:
    """Persist the new policy value and return to its own section."""
    data = await state.get_data()
    field_name = data.get("field")
    section = data.get("section", "limits")
    if not field_name or policy_section_of(field_name) is None:
        await state.clear()
        await message.answer("⚠️ فیلد نامعتبر.", reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)))
        return

    raw = (message.text or "").strip()
    is_cost = policy_section_of(field_name) == "costs"
    if is_cost:
        value = parse_amount(raw)
        if value is None:
            await message.answer(
                "لطفاً یک عدد غیرمنفی وارد کنید (مثلاً ۲ یا ۲٫۵)."
            )
            return
    else:
        value = parse_int(raw)
        if value is None:
            await message.answer("لطفاً یک عدد صحیح وارد کنید.")
            return

    await _set_policy_field(field_name, value)
    await state.clear()

    policy = await get_policy()
    label = policy_field_label(field_name)
    stored = getattr(policy, field_name, 0)
    shown = fmt_coins(stored) if is_cost else str(stored)

    kb = {
        "costs": admin_costs_kb,
        "rewards": admin_rewards_kb,
        "limits": admin_limits_kb,
    }.get(section, admin_limits_kb)(policy)
    text = {
        "costs": _costs_text,
        "rewards": _rewards_text,
        "limits": _limits_text,
    }.get(section, _limits_text)(policy)

    await message.answer(
        f"{get_plain_emoji('check')} <b>{label}</b> = <b>{shown}</b> ثبت شد.",
        parse_mode="HTML",
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ──────────────────────────────────────────────────────────────────────────
# 6. Admin gifting (coins / premium → one user, everyone, or n random)
# ──────────────────────────────────────────────────────────────────────────
_GIFT_LABELS = {
    "coins": "سکه",
    "premium": "روز اشتراک",
}

#: The recipient scopes offered after the type is picked. "one" is the
#: keyed-by-id flow the panel always had; "all"/"random" credit a whole
#: population in ONE transaction (``utils.economy.bulk_add_*``) and therefore
#: require a confirmation step — a mass credit cannot be walked back.
_GIFT_SCOPES = ("one", "all", "random")

#: A bulk gift's recipient notices are delivered one-by-one under Telegram's
#: flood limit, which takes far longer than an admin should wait on a spinner.
#: The task therefore outlives the handler and is kept alive in this set: a
#: bare ``create_task`` result is garbage-collectable mid-flight, and the
#: sweep would silently stop halfway through the user base.
_GIFT_NOTIFY_TASKS: set[asyncio.Task] = set()


def _spawn_gift_notify(coro) -> None:
    """Run a bulk-gift notification coroutine in the background, keeping a reference."""
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        _GIFT_NOTIFY_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("bulk gift notification sweep failed", exc_info=exc)

    task.add_done_callback(_done)
    _GIFT_NOTIFY_TASKS.add(task)


async def _active_user_count() -> int:
    """How many non-banned accounts exist — the population bulk gifts target."""
    async with async_session_factory() as session:
        return (
            await session.scalar(
                select(func.count(User.id)).where(User.is_banned.is_(False))
            )
        ) or 0


async def _notify_gifted(bot, user_ids: list[int], gift_note: str) -> int:
    """Best-effort gift-notice sweep; returns how many users were reached.

    A blocked bot or a deleted account is normal in any sizeable user base and
    must not abort the sweep: the coins are already in the wallet, the notice
    is a courtesy. Same 0.05s pacing as the panel's broadcast.
    """
    text = (
        "<b>هدیه از طرف مدیریت</b>\n\n"
        f"شما <b>{gift_note}</b> دریافت کردید.\n\n"
        "از منوی «🏆 امتیازات و سکه» موجودی خود را ببینید."
    )
    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=main_menu_kb()
            )
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # flood-limit friendly
    return sent


def _gift_amount_prompt(data: dict) -> str:
    """The «enter the amount» text, saying who the amount lands on."""
    gift_type = data.get("gift_type")
    label = _GIFT_LABELS.get(gift_type, "هدیه")
    hint = "عدد صحیح" if gift_type != "coins" else "عدد صحیح یا اعشاری"
    scope = data.get("target_scope")
    if scope == "all":
        who = "\nاین مقدار به <b>هر کاربر فعال</b> داده می‌شود."
    elif scope == "random":
        who = (
            "\nاین مقدار به هر یک از "
            f"<b>{data.get('recipient_count', 0)} کاربر تصادفی</b> داده می‌شود."
        )
    else:
        who = ""
    return f"مقدار <b>{label}</b> را وارد کنید ({hint}):{who}"


async def _gift_confirm_text(data: dict) -> str:
    """The bulk-gift confirmation card: per-recipient amount, audience, total."""
    gift_type = data.get("gift_type")
    amount = data.get("amount")
    if data.get("target_scope") == "all":
        n = await _active_user_count()
        who = f"همهٔ کاربران فعال (<b>{n}</b> نفر)"
    else:
        n = int(data.get("recipient_count") or 0)
        who = f"<b>{n}</b> کاربر تصادفی"

    if gift_type == "coins":
        per = f"{fmt_coins(amount)} سکه"
        total = f"مجموع هدیه: <b>{fmt_coins(round_coins(amount * n))}</b> سکه"
    else:
        per = f"{amount} روز اشتراک"
        total = f"مجموع هدیه: <b>{amount * n}</b> روز اشتراک"

    return (
        f"{get_plain_emoji('warning')} <b>تأیید هدیهٔ گروهی</b>\n\n"
        f"به هر نفر: <b>{per}</b>\n"
        f"گیرنده‌ها: {who}\n"
        f"{total}\n\n"
        "پس از تأیید، امکان بازگشت نیست."
    )


@router.callback_query(F.data == "admin:gift")
async def cb_gift_menu(callback: CallbackQuery) -> None:
    """Show the gift-type menu."""
    await _edit_or_answer(
        callback,
        "<b>هدیه دادن</b>\n\n"
        "نوع هدیه را انتخاب کنید؛ در مرحلهٔ بعد گیرنده را مشخص می‌کنید:\n"
        "یک کاربر مشخص، همهٔ کاربران یا تعدادی تصادفی.",
        admin_gift_kb(),
    )


@router.callback_query(F.data.in_({"admin:gift:coins", "admin:gift:premium"}))
async def cb_gift_type(callback: CallbackQuery, state: FSMContext) -> None:
    """Show the WHO screen for the chosen gift type.

    Exact-match filter on purpose: ``cb_gift_scope`` below owns every deeper
    ``admin:gift:*`` path, and the two must never race for the same update.
    """
    gift_type = callback.data.split(":", 2)[2]
    user_count = await _active_user_count()
    if user_count == 0:
        await callback.answer("هنوز کاربری ثبت نشده است.", show_alert=True)
        return

    # Wipe whatever a previous attempt left parked (scope, count, amount):
    # re-picking the type must start the flow from a known-clean slate.
    await state.clear()
    await state.update_data(gift_type=gift_type)
    await _edit_or_answer(
        callback,
        f"<b>هدیه {_GIFT_LABELS[gift_type]}</b>\n\n"
        f"{get_plain_emoji('users')} کاربران فعال: <b>{user_count}</b> نفر\n\n"
        "گیرنده را انتخاب کنید:",
        admin_gift_scope_kb(gift_type, user_count),
    )


@router.callback_query(F.data == "admin:gift:confirm")
async def cb_gift_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Execute a confirmed bulk gift: credit first, notify in the background.

    Registered BEFORE the ``admin:gift:`` prefix handler on purpose — that
    handler validates 4-part paths and would swallow this 3-part one.
    """
    data = await state.get_data()
    gift_type = data.get("gift_type")
    scope = data.get("target_scope")
    amount = data.get("amount")

    if (
        gift_type not in _GIFT_LABELS
        or scope not in ("all", "random")
        or amount is None
    ):
        await callback.answer(
            "⚠️ اطلاعات هدیه ناقص است؛ دوباره از منو انتخاب کنید.",
            show_alert=True,
        )
        await state.clear()
        return
    await state.clear()
    await callback.answer("🎁 در حال اهداء…")

    # "random" samples HERE, so the granted set and the notified set are the
    # same ids; min() re-caps the count against a pool that may have shrunk
    # since the admin typed it.
    user_ids: list[int] | None = None
    if scope == "random":
        async with async_session_factory() as session:
            rows = await session.execute(
                select(User.telegram_id).where(User.is_banned.is_(False))
            )
            pool = [row[0] for row in rows.all()]
        k = min(int(data.get("recipient_count") or 0), len(pool))
        user_ids = random.sample(pool, k) if k > 0 else []

    if gift_type == "coins":
        granted = await bulk_add_coins(amount, user_ids=user_ids)
        per = f"{fmt_coins(amount)} سکه"
    else:
        granted = await bulk_add_premium_days(int(amount), user_ids=user_ids)
        per = f"{int(amount)} روز اشتراک"

    if not granted:
        await callback.message.answer(
            f"{get_plain_emoji('warning')} کاربر فعالی برای هدیه وجود ندارد.",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(is_root=_is_root(callback.from_user.id)),
        )
        return

    # The money is already in every wallet; the notices are a courtesy and
    # take ~0.05s each, so the admin gets their panel back right away.
    _spawn_gift_notify(_notify_gifted(callback.bot, granted, per))

    await callback.message.answer(
        f"{get_plain_emoji('check')} <b>هدیه انجام شد</b>\n\n"
        f"{per} به <b>{len(granted)} کاربر</b> اضافه شد.\n"
        f"{get_plain_emoji('send')} در حال اطلاع‌رسانی به گیرندگان…",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=_is_root(callback.from_user.id)),
    )


@router.callback_query(F.data.startswith("admin:gift:"))
async def cb_gift_scope(callback: CallbackQuery, state: FSMContext) -> None:
    """Route the chosen recipient scope (one / all / random) into its FSM step."""
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[2] not in _GIFT_LABELS or parts[3] not in _GIFT_SCOPES:
        await callback.answer("نوع نامعتبر.", show_alert=True)
        return
    gift_type, scope = parts[2], parts[3]

    user_count = await _active_user_count()
    if user_count == 0:
        await callback.answer("هنوز کاربری ثبت نشده است.", show_alert=True)
        return

    await state.update_data(gift_type=gift_type, target_scope=scope)

    if scope == "one":
        await state.set_state(AdminGift.waiting_for_user_id)
        await _edit_or_answer(
            callback,
            f"<b>هدیه {_GIFT_LABELS[gift_type]} به یک کاربر</b>\n\n"
            "آیدی عددی تلگرام گیرنده را بفرستید:",
            admin_cancel_kb(),
        )
    elif scope == "random":
        await state.set_state(AdminGift.waiting_for_count)
        await _edit_or_answer(
            callback,
            f"<b>هدیه {_GIFT_LABELS[gift_type]} به n کاربر تصادفی</b>\n\n"
            f"تعداد کاربران را وارد کنید (حداکثر <b>{user_count}</b>):",
            admin_cancel_kb(),
        )
    else:  # "all"
        await state.set_state(AdminGift.waiting_for_amount)
        data = await state.get_data()
        await _edit_or_answer(callback, _gift_amount_prompt(data), admin_cancel_kb())


@router.message(AdminGift.waiting_for_user_id)
async def msg_gift_user(message: Message, state: FSMContext) -> None:
    """Capture the one-user gift's recipient, then ask for the amount."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("هدیه"))
        return

    await state.update_data(target_id=target_id)
    await state.set_state(AdminGift.waiting_for_amount)

    data = await state.get_data()
    await message.answer(
        _gift_amount_prompt(data),
        parse_mode="HTML",
        reply_markup=admin_cancel_kb(),
    )


@router.message(AdminGift.waiting_for_count)
async def msg_gift_count(message: Message, state: FSMContext) -> None:
    """Capture how many users a random gift should reach."""
    count = parse_int((message.text or "").strip())
    if count is None or count <= 0:
        await message.answer("لطفاً یک عدد مثبت وارد کنید.")
        return

    pool_size = await _active_user_count()
    if count > pool_size:
        await message.answer(
            f"{get_plain_emoji('warning')} حداکثر تعداد کاربران فعال "
            f"<b>{pool_size}</b> نفر است.",
            parse_mode="HTML",
        )
        return

    await state.update_data(recipient_count=count)
    await state.set_state(AdminGift.waiting_for_amount)
    data = await state.get_data()
    await message.answer(
        _gift_amount_prompt(data),
        parse_mode="HTML",
        reply_markup=admin_cancel_kb(),
    )


@router.message(AdminGift.waiting_for_amount)
async def msg_gift_amount(message: Message, state: FSMContext) -> None:
    """Validate the amount: pay a single recipient now, or ask bulk to confirm."""
    data = await state.get_data()
    gift_type = data.get("gift_type")
    scope = data.get("target_scope", "one")

    raw = (message.text or "").strip()
    # A coin gift may be fractional for the same reason a price may be: the
    # admin has to be able to top up exactly the 0.5 the next send costs.
    # Premium days are a calendar count and stay whole.
    if gift_type == "coins":
        amount: int | float | None = parse_amount(raw)
    else:
        amount = parse_int(raw)
    if amount is None or amount <= 0:
        await message.answer("لطفاً یک عدد مثبت وارد کنید.")
        return
    if gift_type == "coins":
        amount = round_coins(amount)

    if scope in ("all", "random"):
        # Money about to move for a whole population: park the amount, show
        # the total and wait for an explicit tap on «تأیید و اهداء».
        await state.update_data(amount=amount)
        await state.set_state(AdminGift.confirm_send)
        await message.answer(
            await _gift_confirm_text({**data, "amount": amount}),
            parse_mode="HTML",
            reply_markup=admin_gift_confirm_kb(),
        )
        return

    target_id = data.get("target_id")

    ok = False
    result_text = ""
    if gift_type == "coins":
        ok = await add_coins(target_id, amount)
        result_text = (
            f"{fmt_coins(amount)} سکه به کاربر <code>{target_id}</code> اضافه شد."
        )
    elif gift_type == "premium":
        ok = await add_premium_days(target_id, amount)
        result_text = (
            f"{amount} روز اشتراک به کاربر <code>{target_id}</code> اضافه شد."
        )

    if not ok:
        await message.answer(
            f"کاربر <code>{target_id}</code> یافت نشد.",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)),
        )
        await state.clear()
        return

    await state.clear()

    # Notify the recipient
    try:
        gift_note = {
            "coins": f"{fmt_coins(amount)} سکه",
            "premium": f"{amount} روز اشتراک",
        }[gift_type]
        await message.bot.send_message(
            target_id,
            "<b>هدیه از طرف مدیریت</b>\n\n"
            f"شما <b>{gift_note}</b> دریافت کردید.\n\n"
            "از منوی «🏆 امتیازات و سکه» موجودی خود را ببینید.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        pass

    await message.answer(
        result_text,
        parse_mode="HTML",
        reply_markup=admin_panel_kb(is_root=_is_root(message.from_user.id)),
    )


# ─────────────────────────────────────────────────────────────
# 7. Whisper (نجوا) settings — feature switch + price + stats
# ─────────────────────────────────────────────────────────────

async def _mutate_whisper_config(**changes) -> None:
    """Apply a partial update to the singleton whisper config row."""
    async with async_session_factory() as session:
        result = await session.execute(select(WhisperConfig).limit(1))
        config = result.scalar_one_or_none()
        if config is None:
            config = WhisperConfig()
            session.add(config)
        for key, value in changes.items():
            setattr(config, key, value)
        await session.commit()

    await refresh_whisper_config()
    clear_membership_cache()


async def _whisper_config_text(config) -> str:
    if config.enabled:
        status = f"{get_plain_emoji('check')} <b>فعال</b>"
    else:
        status = f"{get_plain_emoji('cross')} <b>غیرفعال</b>"

    policy = await get_policy()
    whisper_price = (
        "رایگان"
        if (policy.whisper_cost or 0) <= 0
        else f"{fmt_coins(policy.whisper_cost)} سکه"
    )

    lines = [
        f"🔇 <b>تنظیمات نجوا</b>\n\n"
        "نجوا یعنی ارسال پیام خصوصی در گروه؛ فقط گیرنده می‌تواند آن را "
        "ببیند و بقیهٔ اعضا به محتوا دسترسی ندارند.\n\n"
        f"{get_plain_emoji('gear')} وضعیت قابلیت: {status}\n"
        f"{get_plain_emoji('coins')} هزینهٔ هر نجوا: <b>{whisper_price}</b> "
        f"(کاربران اشتراکی و معاف رایگان)\n"
        f"{get_plain_emoji('edit')} حداکثر طول متن: <b>{config.max_length}</b> کاراکتر"
        # Breadcrumb, not a button: عضویت اجباری is a global gate and used to
        # hide on this screen. Old muscle memory must still find it.
        f"\n\n{get_plain_emoji('broadcast')} عضویت اجباری از بخش "
        "«📢 عضویت اجباری» در منوی مدیریت تنظیم می‌شود.",
    ]

    return "\n".join(lines)


@router.callback_query(F.data == "admin:whisper")
async def cb_whisper_menu(callback: CallbackQuery) -> None:
    """Show the whisper settings screen."""
    config = await get_whisper_config()
    await _edit_or_answer(
        callback, await _whisper_config_text(config), admin_whisper_kb(config)
    )


@router.callback_query(F.data == "admin:whisper:toggle")
async def cb_whisper_toggle(callback: CallbackQuery) -> None:
    """Enable / disable the whole whisper feature."""
    config = await get_whisper_config()
    await _mutate_whisper_config(enabled=not config.enabled)
    whisper_on = not config.enabled
    # The Telegram /-menu has to follow the toggle: private_command_rows drops
    # the whisper commands while the feature is off, and the menu published at
    # startup keeps showing them until this runs again. Import inside the
    # function: bot.py imports the handlers package, so a module-level import
    # here would be circular.
    from bot import register_commands

    await register_commands(
        callback.bot, whisper_on=whisper_on, admin_ids=settings.admin_ids_list
    )
    await callback.answer(
        "✅ نجوا فعال شد." if whisper_on else "⛔ نجوا غیرفعال شد.",
        show_alert=True,
    )
    await cb_whisper_menu(callback)


@router.callback_query(F.data == "admin:whisper:maxlen")
async def cb_whisper_maxlen_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for a new character limit for a single whisper."""
    config = await get_whisper_config()
    await state.set_state(AdminWhisper.waiting_for_max_length)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('edit')} <b>حداکثر طول متن نجوا</b>\n\n"
        f"مقدار فعلی: <b>{config.max_length}</b> کاراکتر\n\n"
        "عددی بین <b>۱۰۰</b> و <b>۴۰۹۶</b> بفرستید:",
        admin_cancel_kb(),
    )


@router.message(AdminWhisper.waiting_for_max_length)
async def msg_whisper_maxlen(message: Message, state: FSMContext) -> None:
    """Persist the new limit and return to the whisper screen."""
    value = parse_int((message.text or "").strip())
    if value is None or not (100 <= value <= 4096):
        await message.answer("لطفاً عدد صحیحی بین ۱۰۰ و ۴۰۹۶ وارد کنید.")
        return

    await _mutate_whisper_config(max_length=value)
    await state.clear()

    await message.answer(
        f"{get_plain_emoji('check')} حداکثر طول متن = <b>{value}</b> کاراکتر ثبت شد.",
        parse_mode="HTML",
    )
    config = await get_whisper_config()
    await message.answer(
        await _whisper_config_text(config),
        parse_mode="HTML",
        reply_markup=admin_whisper_kb(config),
    )


# ─────────────────────────────────────────────────────────────
# 7b. Force-join (عضویت اجباری) — its own section
# ─────────────────────────────────────────────────────────────
#
# Forced membership used to be four buttons buried inside «تنظیمات نجوا»,
# which was both wrong and confusing: the requirement gates EVERY feature
# (starting the bot, sending a whisper, reading a whisper), not just نجوا.
# It now lives on the main admin panel as «📢 عضویت اجباری» with its own
# screen, and nothing on the whisper screen changes it anymore.

async def _migrate_legacy_join_chat() -> None:
    """Fold the legacy single forced-join chat into ``required_channels``.

    Before the multi-channel list existed, the admin panel stored ONE chat on
    the whisper config row (``required_chat_id``) — and «تعیین کانال/گروه
    عضویت» wrote it directly. That UI is gone, so an old configuration would
    otherwise become invisible: still enforced by ``required_chats()`` (which
    unions both sources), but uneditable. Moving the row into the table keeps
    behaviour identical — ``required_chats()`` de-duplicates by API ref — and
    leaves the admin with exactly one list to manage.

    Failure is non-fatal and non-destructive: the legacy fields are cleared
    only after the table write committed, so a DB hiccup is retried on the
    next open instead of silently dropping the requirement.
    """
    config = await get_whisper_config()
    legacy_id = config.required_chat_id
    if not legacy_id:
        return

    try:
        async with async_session_factory() as session:
            existing = await session.scalar(
                select(RequiredChannel).where(RequiredChannel.chat_id == legacy_id)
            )
            if existing is None:
                last = await session.scalar(select(func.max(RequiredChannel.position)))
                session.add(
                    RequiredChannel(
                        chat_id=legacy_id,
                        username=config.required_chat_username or None,
                        title=config.required_chat_title or None,
                        position=(last or 0) + 1,
                        is_active=True,
                    )
                )
            elif not existing.is_active:
                existing.is_active = True
            await session.commit()
    except Exception as exc:
        logger.warning("Could not migrate legacy forced-join chat: %s", exc)
        return

    await _mutate_whisper_config(
        required_chat_id=None,
        required_chat_username=None,
        required_chat_title=None,
    )


async def _unchecked_channel_ids(bot, channels) -> set[int]:
    """Chat ids where the bot currently cannot verify membership.

    ``getChatMember`` only answers authoritatively for an administrator, and
    outside the chat it fails outright — either way the forced-join gate would
    silently answer WRONG for every user. One lookup per row (this is an
    admin-only screen and N is small) turns that silent failure into a visible
    ⚠️ on the row plus a warning line in the text, instead of a broken gate
    nobody can diagnose.
    """
    bad: set[int] = set()
    for row in channels:
        if not await bot_is_admin(bot, row.chat_id):
            bad.add(row.chat_id)
    return bad


async def _required_chat_rights_error(bot, chat_id: int) -> str | None:
    """A Persian, actionable message when the bot cannot police ``chat_id``.

    Returns ``None`` when the bot is an administrator there. Every other
    outcome — not a member, no access at all, plain member with unreliable
    answers — would make the join check reject real members forever, so the
    add flow refuses the channel up front and says exactly how to fix it,
    rather than crashing (the old code) or letting the admin discover the
    breakage through confused users.
    """
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except Exception as exc:
        logger.warning("Rights check failed for forced-join chat %s: %s", chat_id, exc)
        return (
            f"{get_plain_emoji('cross')} <b>ربات به این کانال/گروه دسترسی ندارد.</b>\n\n"
            "ابتدا ربات را در آن عضو کنید و ادمین (مدیر) نمایید، "
            "سپس همین کار را دوباره انجام دهید."
        )

    if me.status in ("left", "kicked"):
        return (
            f"{get_plain_emoji('cross')} <b>ربات در این کانال/گروه عضو نیست.</b>\n\n"
            "ربات را عضو و سپس ادمین کنید، بعد دوباره تلاش کنید."
        )

    if me.status != "administrator":
        return (
            f"{get_plain_emoji('warning')} <b>ربات در این کانال/گروه ادمین نیست.</b>\n\n"
            "برای بررسی درست عضویت کاربران ربات باید ادمین باشد:\n"
            "تنظیمات گروه ← مدیران ← افزودن ربات ← «ادمین»."
        )

    return None


async def _list_required_channels() -> list[RequiredChannel]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(RequiredChannel)
            .where(RequiredChannel.is_active.is_(True))
            .order_by(RequiredChannel.position, RequiredChannel.id)
        )
        return list(result.scalars().all())


def _forcejoin_text(channels: list[RequiredChannel], config, unchecked: set[int]) -> str:
    """Body of the force-join screen — status, count, and the list itself.

    Shared by the section menu and the manage-list screen so the admin never
    sees two different answers to "which channels are required?".
    """
    status = (
        f"{get_plain_emoji('check')} <b>فعال</b>"
        if config.require_join
        else f"{get_plain_emoji('cross')} <b>خاموش</b>"
    )
    lines = [
        "📢 <b>عضویت اجباری</b>\n\n"
        "کاربر برای استفاده از ربات (ارسال و خواندن نجوا) باید پیش از هر کاری "
        "در <b>همهٔ</b> کانال‌های زیر عضو باشد؛ ربات عضویت او را بررسی می‌کند.\n\n"
        f"{get_plain_emoji('gear')} وضعیت: {status}\n"
        f"{get_plain_emoji('list')} تعداد کانال‌ها: <b>{len(channels)}</b>",
    ]

    if not channels:
        lines.append(
            f"\n{get_plain_emoji('cross')} هنوز کانالی ثبت نشده است؛ با دکمهٔ "
            "«افزودن کانال» اولین کانال را اضافه کنید."
        )
    else:
        lines.append("")
        for i, row in enumerate(channels, 1):
            name = escape(row.title or row.username or str(row.chat_id))
            mark = " ⚠️" if row.chat_id in unchecked else ""
            lines.append(f"{i}. {name}{mark}\n   <code>{row.chat_id}</code>")
        lines.append(
            "\nکاربر باید در همهٔ این کانال‌ها عضو باشد؛ با «افزودن کانال» "
            "مورد جدید اضافه کنید."
        )

    if not config.require_join:
        lines.append(
            f"\n{get_plain_emoji('warning')} این قابلیت فعلاً <b>خاموش</b> است؛ "
            "برای اعمال، ابتدا آن را روشن کنید."
        )
    if unchecked:
        lines.append(
            f"\n{get_plain_emoji('warning')} <b>ربات در {len(unchecked)} مورد "
            "ادمین نیست</b> و نمی‌تواند عضویت را بررسی کند؛ ربات را در آن کانال‌ها "
            "ادمین کنید."
        )
    return "\n".join(lines)


async def _render_forcejoin(callback: CallbackQuery, *, manage: bool) -> None:
    """Draw the force-join screen: ``manage=False`` the menu, ``True`` the list.

    The legacy single-chat field is migrated here — at the one entry point
    every path funnels through — so it happens exactly once, no matter which
    button the admin arrives on.
    """
    await _migrate_legacy_join_chat()
    config = await get_whisper_config()
    channels = await _list_required_channels()
    unchecked = await _unchecked_channel_ids(callback.bot, channels)
    kb = (
        admin_forcejoin_channels_kb(channels, unchecked)
        if manage
        else admin_forcejoin_kb(config, channels)
    )
    await _edit_or_answer(callback, _forcejoin_text(channels, config, unchecked), kb)


@router.callback_query(F.data == "admin:forcejoin")
async def cb_forcejoin_menu(callback: CallbackQuery) -> None:
    """The force-join (عضویت اجباری) section, opened from the main panel."""
    await _render_forcejoin(callback, manage=False)


@router.callback_query(F.data == "admin:forcejoin:toggle")
async def cb_forcejoin_toggle(callback: CallbackQuery) -> None:
    """Enable / disable the forced-join requirement globally."""
    config = await get_whisper_config()
    await _mutate_whisper_config(require_join=not config.require_join)
    await callback.answer(
        "✅ عضویت اجباری فعال شد." if not config.require_join
        else "⛔ عضویت اجباری غیرفعال شد.",
        show_alert=True,
    )
    await _render_forcejoin(callback, manage=False)


@router.callback_query(F.data == "admin:forcejoin:channels")
async def cb_forcejoin_channels(callback: CallbackQuery) -> None:
    """List the required channels with a delete button on each."""
    await _render_forcejoin(callback, manage=True)


@router.callback_query(F.data == "admin:forcejoin:channels:new")
async def cb_forcejoin_channels_new(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask the admin for the next channel to append to the list."""
    await state.set_state(AdminWhisper.waiting_for_channel)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('add')} <b>افزودن کانال/گروه عضویت اجباری</b>\n\n"
        "ربات باید در آن کانال یا گروه <b>ادمین</b> باشد تا بتواند اعضا را "
        "بررسی کند. یکی از موارد زیر را بفرستید:\n\n"
        "• آیدی عددی، مثل <code>-1001234567890</code>\n"
        "• یوزرنیم، مثل <code>@mychannel</code>\n"
        "• لینک دعوت <code>https://t.me/...</code>\n"
        "• یا پیامی را از همان کانال/گروه <b>فاروارد</b> کنید",
        admin_cancel_kb(),
    )


@router.message(AdminWhisper.waiting_for_channel)
async def msg_forcejoin_channel(message: Message, state: FSMContext) -> None:
    """Resolve the admin's input and append it to the required-channel list."""
    chat_ref = message.chat.id if message.forward_origin else (message.text or "")

    chat_id, username, title = await resolve_chat_ref(message.bot, str(chat_ref))
    if not chat_id:
        await message.answer(
            f"{get_plain_emoji('cross')} <b>این کانال/گروه پیدا نشد.</b>\n\n"
            "آیدی عددی، یوزرنیم یا لینک درست را بفرستید، "
            "یا یک پیام از همان کانال/گروه فاروارد کنید.",
            reply_markup=admin_cancel_kb(),
        )
        return

    # Refuse — with an actionable message — what the bot could not police
    # anyway. Storing such a channel would freeze every user at the join
    # prompt forever, because the membership lookup can never succeed there.
    rights_error = await _required_chat_rights_error(message.bot, chat_id)
    if rights_error:
        # The state is deliberately KEPT: after promoting the bot the admin
        # can resend the very same input without pressing «افزودن کانال» again.
        await message.answer(rights_error, reply_markup=admin_cancel_kb())
        return

    # The confirmation is a message send — a network call — so it is composed
    # inside the session but delivered only after the session closed.
    added_text: str | None = None
    async with async_session_factory() as session:
        existing = await session.scalar(
            select(RequiredChannel).where(RequiredChannel.chat_id == chat_id)
        )
        if existing is not None:
            # Re-adding a channel is almost always a slip; resurrect it and
            # refresh the title rather than failing on the unique constraint.
            existing.is_active = True
            if title:
                existing.title = title
            if username:
                existing.username = username
            await session.commit()
            # NOTE: clear_membership_cache() is SYNC — awaiting it is the
            # TypeError that used to crash this handler mid-flow.
            clear_membership_cache()
            added_text = (
                f"{get_plain_emoji('check')} این کانال از قبل در فهرست بود؛ "
                "دوباره فعال شد."
            )
        else:
            # Append at the end so the admin controls the order the reader
            # sees the join buttons in.
            last = await session.scalar(
                select(func.max(RequiredChannel.position))
            )
            session.add(
                RequiredChannel(
                    chat_id=chat_id,
                    username=username or None,
                    title=title or None,
                    position=(last or 0) + 1,
                    is_active=True,
                )
            )
            await session.commit()
            clear_membership_cache()
            added_text = (
                f"{get_plain_emoji('check')} <b>کانال اضافه شد.</b>\n\n"
                f"{escape(title or str(chat_id))}"
            )

    if added_text:
        await message.answer(added_text)

    await state.clear()
    config = await get_whisper_config()
    channels = await _list_required_channels()
    unchecked = await _unchecked_channel_ids(message.bot, channels)
    await message.answer(
        _forcejoin_text(channels, config, unchecked),
        parse_mode="HTML",
        reply_markup=admin_forcejoin_channels_kb(channels, unchecked),
    )


@router.callback_query(F.data.regexp(r"^admin:forcejoin:channels:del:\d+$"))
async def cb_forcejoin_channel_del(callback: CallbackQuery) -> None:
    """Remove one channel from the required list."""
    raw_id = callback.data.rsplit(":", 1)[1]
    try:
        row_id = int(raw_id)
    except (TypeError, ValueError):
        await callback.answer("⚠️ شناسهٔ نامعتبر.", show_alert=True)
        return

    async with async_session_factory() as session:
        row = await session.get(RequiredChannel, row_id)
        if row is not None:
            await session.delete(row)
            await session.commit()

    # Cached "is a member" answers were computed against the old channel set.
    # ``clear_membership_cache`` is SYNC — this line used to be ``await``ed and
    # raised TypeError, killing the handler before the confirmation showed.
    clear_membership_cache()
    await callback.answer("🗑 کانال از فهرست عضویت اجباری حذف شد.", show_alert=True)
    await cb_forcejoin_channels(callback)


@router.callback_query(F.data.startswith("admin:whisper:join"))
@router.callback_query(F.data.startswith("admin:whisper:channels"))
async def cb_forcejoin_legacy_redirect(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Route pre-upgrade force-join buttons to the new section.

    A panel message rendered before this refactor still carries the old
    ``admin:whisper:join:*`` / ``admin:whisper:channels*`` callbacks under its
    buttons. Unhandled, each tap would show Telegram's dead-button spinner;
    redirecting keeps every historical message a working door into the section
    that now owns the feature.
    """
    await state.clear()
    await _render_forcejoin(callback, manage="channels" in callback.data)


@router.callback_query(F.data == "admin:whisper:stats")
async def cb_whisper_stats(callback: CallbackQuery) -> None:
    """Usage statistics for the whisper feature."""
    since = datetime.utcnow() - timedelta(hours=24)

    async with async_session_factory() as session:
        total = await session.scalar(select(func.count(Whisper.id))) or 0
        viewed = await session.scalar(
            select(func.count(Whisper.id)).where(Whisper.is_viewed.is_(True))
        ) or 0
        last_24h = await session.scalar(
            select(func.count(Whisper.id)).where(Whisper.created_at >= since)
        ) or 0
        senders = await session.scalar(
            select(func.count(func.distinct(Whisper.sender_id)))
        ) or 0
        groups = await session.scalar(
            select(func.count(func.distinct(Whisper.chat_id)))
        ) or 0
        top = (
            await session.execute(
                select(Whisper.chat_title, func.count(Whisper.id).label("total"))
                .group_by(Whisper.chat_id, Whisper.chat_title)
                .order_by(func.count(Whisper.id).desc())
                .limit(5)
            )
        ).all()

    read_rate = f"{round(viewed * 100 / total)}٪" if total else "—"

    lines = [
        "📊 <b>آمار نجوا</b>\n",
        f"{get_plain_emoji('chat')} کل نجواهای ارسال‌شده: <b>{total}</b>",
        f"{get_plain_emoji('eye')} خوانده‌شده: <b>{viewed}</b> ({read_rate})",
        f"{get_plain_emoji('clock')} ۲۴ ساعت اخیر: <b>{last_24h}</b>",
        f"{get_plain_emoji('users')} فرستنده‌های یکتا: <b>{senders}</b>",
        f"{get_plain_emoji('blocks')} گروه‌های فعال: <b>{groups}</b>",
    ]
    if top:
        lines.append("")
        lines.append(f"{get_plain_emoji('crown')} <b>فعال‌ترین گروه‌ها:</b>")
        lines.extend(
            f"• {title or '—'}: <b>{count}</b>" for title, count in top
        )

    await _edit_or_answer(callback, "\n".join(lines), admin_whisper_kb(await get_whisper_config()))


# ──────────────────────────────────────────────────────────────────────────
# 8. Coin & referral report — who received what
# ──────────────────────────────────────────────────────────────────────────
#
# Built on ``coin_ledger``, which records every movement in the same
# transaction as the balance change. The report never recomputes history from
# today's prices: a user paid under an older referral reward shows what they
# were actually paid, which is the whole reason the ledger exists.

#: Ledger reason → report name lives in ``utils.economy.REASON_LABELS`` so
#: the user's own history and this report translate a reason the same way.

async def _report_text() -> str:
    """The aggregate coin report: 24h flow, all-time totals, top inviters."""
    since = datetime.utcnow() - timedelta(hours=24)
    async with async_session_factory() as session:
        granted_24 = await session.scalar(
            select(func.coalesce(func.sum(CoinTransaction.amount), 0)).where(
                CoinTransaction.amount > 0,
                CoinTransaction.created_at >= since,
            )
        ) or 0
        spent_24 = await session.scalar(
            select(func.coalesce(func.sum(CoinTransaction.amount), 0)).where(
                CoinTransaction.amount < 0,
                CoinTransaction.created_at >= since,
            )
        ) or 0
        granted_total = await session.scalar(
            select(func.coalesce(func.sum(CoinTransaction.amount), 0)).where(
                CoinTransaction.amount > 0
            )
        ) or 0
        spent_total = await session.scalar(
            select(func.coalesce(func.sum(CoinTransaction.amount), 0)).where(
                CoinTransaction.amount < 0
            )
        ) or 0

        # Referrals are their own line: this is the number the admin asked for
        # when they asked "how many coins did users earn by inviting".
        referral_rows = (
            await session.execute(
                select(
                    CoinTransaction.user_id,
                    func.sum(CoinTransaction.amount),
                    func.count(CoinTransaction.id),
                )
                .where(CoinTransaction.reason == "referral")
                .group_by(CoinTransaction.user_id)
            )
        ).all()

        top = (
            await session.execute(
                select(
                    User.telegram_id,
                    User.first_name,
                    User.username,
                    User.referral_count,
                )
                .where(User.referral_count > 0)
                .order_by(User.referral_count.desc())
                .limit(8)
            )
        ).all()

    ref_by_user = {row[0]: (row[1] or 0, row[2] or 0) for row in referral_rows}

    lines = [
        f"{get_plain_emoji('chart')} <b>گزارش سکه و رفرال</b>\n",
        f"{get_plain_emoji('clock')} <b>۲۴ ساعت اخیر</b>",
        f"  {get_plain_emoji('coins')} دریافتی کاربران: <b>{fmt_coins(granted_24)}</b> سکه",
        f"  {get_plain_emoji('sell')} هزینه‌شده: <b>{fmt_coins(abs(spent_24))}</b> سکه",
        "",
        f"{get_plain_emoji('balance')} <b>جمع کل</b>",
        f"  دریافتی: <b>{fmt_coins(granted_total)}</b> سکه",
        f"  هزینه‌شده: <b>{fmt_coins(abs(spent_total))}</b> سکه",
        f"  باقی‌مانده نزد کاربران: <b>{fmt_coins(granted_total + spent_total)}</b> سکه",
        "",
        f"{get_plain_emoji('gift')} <b>رفرال</b>",
        f"  پرداخت‌شده: <b>{fmt_coins(sum(v[0] for v in ref_by_user.values()))}</b> سکه "
        f"به <b>{len(ref_by_user)}</b> کاربر",
    ]

    if top:
        lines.append("")
        lines.append(f"{get_plain_emoji('crown')} <b>برترین دعوت‌کنندگان:</b>")
        for i, (uid, name, username, count) in enumerate(top, 1):
            coins, _ = ref_by_user.get(uid, (0, 0))
            label = escape(name or username or str(uid))
            lines.append(
                f"{i}. {label} — <b>{count}</b> دعوت · <b>{fmt_coins(coins)}</b> سکه"
            )
    else:
        lines.append("")
        lines.append(f"{get_plain_emoji('info')} هنوز دعوتی ثبت نشده است.")

    lines.append(
        f"\n{get_plain_emoji('search')} برای دیدن دریافتیِ هر کاربر، "
        "«جستجوی کاربر» را بزنید."
    )
    return "\n".join(lines)


async def _user_report_text(tg_id: int) -> str | None:
    """One user's ledger card: balance, referrals, and every coin received.

    Returns ``None`` when no such user exists, which the caller turns into a
    plain "not found" — there is nothing to report on a row that was never
    written.
    """
    async with async_session_factory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == tg_id)
        )
        if user is None:
            return None
        rows = (
            await session.execute(
                select(
                    CoinTransaction.reason,
                    func.sum(CoinTransaction.amount),
                    func.count(CoinTransaction.id),
                )
                .where(CoinTransaction.user_id == tg_id)
                .group_by(CoinTransaction.reason)
            )
        ).all()

    received_rows = [(r, s or 0, c or 0) for r, s, c in rows if (s or 0) > 0]
    spent_rows = [(r, abs(s or 0), c or 0) for r, s, c in rows if (s or 0) < 0]
    received_rows.sort(key=lambda x: x[1], reverse=True)
    spent_rows.sort(key=lambda x: x[1], reverse=True)

    received = sum(r[1] for r in received_rows)
    spent = sum(r[1] for r in spent_rows)

    name = escape(user.first_name or user.username or str(tg_id))
    lines = [
        f"{get_plain_emoji('profile')} <b>گزارش سکهٔ کاربر</b>\n",
        f"👤 نام: {name}",
        f"🆔 آیدی: <code>{tg_id}</code>",
        f"{get_plain_emoji('coins')} موجودی فعلی: <b>{fmt_coins(user.coins)}</b> سکه",
        f"{get_plain_emoji('gift')} دعوت‌های موفق: <b>{user.referral_count or 0}</b>",
        "",
        f"{get_plain_emoji('check')} <b>دریافتی:</b>",
    ]
    if received_rows:
        lines.extend(
            f"  • {reason_label(reason)}: <b>{fmt_coins(amount)}</b> سکه ({count} بار)"
            for reason, amount, count in received_rows
        )
    else:
        lines.append("  (هنوز سکه‌ای دریافت نکرده)")
    lines.append(f"  <b>جمع دریافتی: {fmt_coins(received)} سکه</b>")

    lines.append("")
    lines.append(f"{get_plain_emoji('sell')} <b>هزینه‌شده:</b>")
    if spent_rows:
        lines.extend(
            f"  • {reason_label(reason)}: <b>{fmt_coins(amount)}</b> سکه ({count} بار)"
            for reason, amount, count in spent_rows
        )
        lines.append(f"  <b>جمع هزینه: {fmt_coins(spent)} سکه</b>")
    else:
        lines.append("  (چیزی هزینه نکرده)")

    return "\n".join(lines)


@router.callback_query(F.data == "admin:report")
@router.callback_query(F.data == "admin:report:refresh")
async def cb_report(callback: CallbackQuery) -> None:
    """The aggregate coin / referral report."""
    await _edit_or_answer(callback, await _report_text(), admin_report_kb())


@router.callback_query(F.data == "admin:report:user")
async def cb_report_user_search(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the ID whose ledger card the admin wants."""
    await state.set_state(AdminReport.waiting_for_user_id)
    await _edit_or_answer(
        callback,
        f"{get_plain_emoji('search')} <b>گزارش سکهٔ کاربر</b>\n\n"
        "آیدی عددی تلگرام کاربر را بفرستید:",
        admin_cancel_kb(),
    )


@router.message(AdminReport.waiting_for_user_id)
async def msg_report_user(message: Message, state: FSMContext) -> None:
    """Render the ledger card for the ID that was typed."""
    target_id = await _parse_user_id(message.text or "")
    if target_id is None:
        await message.answer(_invalid_id_response("گزارش"))
        return

    text = await _user_report_text(target_id)
    if text is None:
        await message.answer(
            f"{get_plain_emoji('cross')} کاربر <code>{target_id}</code> یافت نشد.",
            parse_mode="HTML",
            reply_markup=admin_cancel_kb(),
        )
        return

    await state.clear()
    await message.answer(text, parse_mode="HTML", reply_markup=admin_report_kb())
