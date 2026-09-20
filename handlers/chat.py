"""
Core chat logic — 1-on-1 anonymous matching and real-time message relay.

Design notes:
  * A user can chat with ONE partner at a time.
  * pair_map / search_queue are kept in-memory (fast, no DB writes).
  * Messages are ONLY relayed live — nothing is persisted to the database.
  * Videos / video notes / animations are strictly blocked.
  * Blocking is peer-to-peer (BlockList table), NOT global (User.is_banned).
  * Privacy: telegram_id is NEVER sent to peers; only first_name + profile photo.
"""

import asyncio
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import Message
from sqlalchemy import select

from config import settings
from database import async_session_factory, User, BlockList
from keyboards import main_menu_kb, queue_menu_kb, chat_menu_kb
from states import ChatState

logger = logging.getLogger(__name__)
router = Router()

# ──────────────────────────────────────────────────
# In-memory chat state tracking
#   pair_map:      telegram_id -> partner_telegram_id
#   search_queue:  set of user_ids waiting for a partner
# ──────────────────────────────────────────────────
pair_map: dict[int, int] = {}
search_queue: set[int] = set()
chat_lock = asyncio.Lock()


# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

async def get_user(tg_id: int) -> User | None:
    """Fetch a user row from the database (or None)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        return result.scalar_one_or_none()


async def is_profile_complete(tg_id: int) -> bool:
    user = await get_user(tg_id)
    return bool(user and user.is_profile_complete)


async def is_banned(tg_id: int) -> bool:
    user = await get_user(tg_id)
    return bool(user and user.is_banned)


async def are_blocked(user_a: int, user_b: int) -> bool:
    """Return True if user_a blocked user_b OR user_b blocked user_a."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_a) & (BlockList.blocked_id == user_b)
            )
        )
        if result.scalar_one_or_none() is not None:
            return True

        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_b) & (BlockList.blocked_id == user_a)
            )
        )
        return result.scalar_one_or_none() is not None


async def force_set_state(
    storage: BaseStorage, bot_id: int, user_id: int, state
) -> None:
    """Directly set another user's FSM state via shared storage."""
    key = StorageKey(bot_id=bot_id, user_id=user_id, chat_id=user_id)
    await storage.set_state(key=key, state=state)


async def _force_clear_peer_state(
    storage: BaseStorage, bot_id: int, user_id: int
) -> None:
    """Forcefully clear a peer's FSM state AND data (fixes stuck In_Chat)."""
    key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
    await storage.set_state(key=key, state=None)
    await storage.set_data(key=key, data={})


def _format_profile(user: User) -> str:
    """Format a user's profile for the match reveal message.
    NEVER includes telegram_id — only first_name, age, city, height.
    """
    name = user.first_name or "ناشناس"
    return (
        f"👤 نام: {name}\n"
        f"🎂 سن: {user.age or '—'}\n"
        f"🏙 شهر: {user.city or '—'}\n"
        f"📏 قد: {user.height or '—'}"
    )


async def _send_profile_with_photo(
    bot, target_id: int, text: str, kb, source_user_id: int
) -> None:
    """Send a match notification with the source user's profile photo."""
    photos = await bot.get_user_profile_photos(source_user_id, limit=1)
    try:
        if photos and photos.total_count > 0:
            photo_file_id = photos.photos[0][-1].file_id
            await bot.send_photo(
                target_id,
                photo=photo_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                target_id,
                text,
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as e:
        logger.warning("Failed to send profile to %s: %s", target_id, e)


# ──────────────────────────────────────────────────
# Start search / find partner
# ──────────────────────────────────────────────────

@router.message(F.text == "🔗 اتصال به ناشناس")
async def start_search(message: Message, state: FSMContext, fsm_storage: BaseStorage) -> None:
    """User wants to find an anonymous chat partner."""
    user_id = message.from_user.id

    if await is_banned(user_id):
        await message.answer("⛔ شما از ربات محروم شده‌اید.")
        return

    if not await is_profile_complete(user_id):
        await message.answer(
            "⚠️ ابتدا پروفایل خود را تکمیل کنید.\n"
            "روی «👤 پروفایل من» کلیک کنید.",
            reply_markup=main_menu_kb(),
        )
        return

    if user_id in pair_map:
        await message.answer(
            "⚠️ شما در حال حاضر در یک چت هستید.\n"
            "ابتدا چت فعلی را لغو کنید.",
            reply_markup=chat_menu_kb(),
        )
        return

    current_state = await state.get_state()
    if current_state == ChatState.in_queue:
        await message.answer(
            "⏳ در حال جستجو هستید. لطفاً صبر کنید.",
            reply_markup=queue_menu_kb(),
        )
        return

    # ── Try to match with a queued partner ──
    partner_id: Optional[int] = None
    async with chat_lock:
        # Remove stale entries (users who left meanwhile)
        stale = {uid for uid in search_queue if uid in pair_map or uid == user_id}
        search_queue.difference_update(stale)

        # Try to find a compatible partner (no mutual blocks)
        if search_queue:
            candidates = list(search_queue)
            for candidate in candidates:
                if not await are_blocked(user_id, candidate):
                    partner_id = candidate
                    search_queue.discard(candidate)
                    break

        if partner_id is not None:
            pair_map[user_id] = partner_id
            pair_map[partner_id] = user_id
        else:
            search_queue.add(user_id)

    if partner_id is not None:
        # Connected! Update BOTH users' FSM states.
        await state.set_state(ChatState.in_chat)
        await force_set_state(fsm_storage, message.bot.id, partner_id, ChatState.in_chat)

        # ── Profile reveal: fetch both users and send profiles WITH photos ──
        me = await get_user(user_id)
        partner = await get_user(partner_id)

        my_profile = _format_profile(me) if me else "—"
        partner_profile = _format_profile(partner) if partner else "—"

        match_intro = "🟢 <b>هم‌صحبت پیدا شد!</b>\n\n"
        match_footer = (
            "\n\nپیام‌های شما مستقیماً برای طرف مقابل ارسال می‌شوند.\n"
            "برای لغو چت، روی «🔴 لغو چت» بزنید."
        )

        # Send partner's profile (with photo) to the current user
        await _send_profile_with_photo(
            message.bot,
            user_id,
            f"{match_intro}مشخصات طرف مقابل:\n{partner_profile}{match_footer}",
            chat_menu_kb(),
            partner_id,
        )

        # Send current user's profile (with photo) to the partner
        await _send_profile_with_photo(
            message.bot,
            partner_id,
            f"{match_intro}مشخصات طرف مقابل:\n{my_profile}{match_footer}",
            chat_menu_kb(),
            user_id,
        )
    else:
        # Added to queue
        await state.set_state(ChatState.in_queue)
        await message.answer(
            "🔍 <b>در حال جستجو... </b>\n\n"
            "لطفاً صبر کنید تا یک کاربر ناشناس پیدا شود.",
            parse_mode="HTML",
            reply_markup=queue_menu_kb(),
        )


# ──────────────────────────────────────────────────
# Cancel search
# ──────────────────────────────────────────────────

@router.message(F.text == "❌ لغو جستجو", ChatState.in_queue)
async def cancel_search(message: Message, state: FSMContext) -> None:
    """Cancel the search queue."""
    search_queue.discard(message.from_user.id)
    await state.set_state(ChatState.idle)
    await message.answer("❌ جستجو لغو شد.", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# End active chat  (all variants)
# ──────────────────────────────────────────────────

@router.message(
    F.text.in_({"🔴 لغو چت", "🔴 پایان چت", "↩️ بازگشت به منو"}),
    ChatState.in_chat,
)
async def end_chat(message: Message, state: FSMContext, fsm_storage: BaseStorage) -> None:
    """End the current chat and free both users."""
    user_id = message.from_user.id
    partner_id = pair_map.pop(user_id, None)

    if partner_id:
        pair_map.pop(partner_id, None)
        await force_set_state(fsm_storage, message.bot.id, partner_id, ChatState.idle)
        try:
            await message.bot.send_message(
                partner_id,
                "🔴 <b>کاربر ناشناس چت را ترک کرد.</b>\n\nبه منوی اصلی بازگشتید.",
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        except Exception as e:
            logger.warning("Failed to notify partner %s: %s", partner_id, e)

    await state.set_state(ChatState.idle)
    await message.answer("🔴 چت پایان یافت.", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# Block (peer-to-peer) — with StorageKey state sync
# ──────────────────────────────────────────────────

@router.message(F.text == "🛑 گزارش کاربر / بلاک", ChatState.in_chat)
async def report_user(message: Message, state: FSMContext, fsm_storage: BaseStorage) -> None:
    """Block the chat partner (peer-to-peer) and end the chat.

    This inserts a row into BlockList — it does NOT set the global
    ``User.is_banned`` flag, which is reserved for admin-only bans.

    FIX: Forcefully clears the peer's FSM state AND data using
    StorageKey to prevent them from being stuck in In_Chat.
    """
    user_id = message.from_user.id
    partner_id = pair_map.pop(user_id, None)

    if partner_id:
        pair_map.pop(partner_id, None)

        # ── FIX: Forcefully clear peer state via StorageKey ──
        await _force_clear_peer_state(fsm_storage, message.bot.id, partner_id)

        # ── Peer-to-peer block: insert into BlockList ──
        async with async_session_factory() as session:
            existing = await session.execute(
                select(BlockList).where(
                    (BlockList.blocker_id == user_id) & (BlockList.blocked_id == partner_id)
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(BlockList(blocker_id=user_id, blocked_id=partner_id))
                await session.commit()

        # ── Notify the blocked peer: disconnected + main menu ──
        try:
            await message.bot.send_message(
                partner_id,
                "⛔ <b>شما از چت جدا شدید.</b>\n"
                "اتصال شما قطع شد.\n\nبه منوی اصلی بازگشتید.",
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        except Exception as e:
            logger.warning("Failed to notify blocked user %s: %s", partner_id, e)

        admin_ids_list = settings.admin_ids_list
        announce_chat = admin_ids_list[0] if admin_ids_list else None
        if announce_chat:
            try:
                await message.bot.send_message(
                    announce_chat,
                    "🛑 <b>گزارش بلاک</b>\n\n"
                    f"کاربر <code>{user_id}</code> کاربر <code>{partner_id}</code> را بلاک کرد.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to notify admin: %s", e)

    await state.set_state(ChatState.idle)
    await message.answer(
        "🛑 کاربر بلاک شد و چت پایان یافت.",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Message forwarding (text, voice, photo — LIVE only)
# ──────────────────────────────────────────────────

async def _forward_to_partner(user_id: int, bot, send_func) -> bool:
    """Send content to the partner if the user is currently paired."""
    partner_id = pair_map.get(user_id)
    if not partner_id:
        return False
    try:
        await send_func(partner_id)
        return True
    except Exception as e:
        logger.warning("Failed to forward to %s: %s", partner_id, e)
        return False


@router.message(ChatState.in_chat, F.text)
async def forward_text(message: Message) -> None:
    """Relay text instantly — never saved to DB."""
    ok = await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_message(pid, message.text),
    )
    if not ok and message.from_user.id not in pair_map:
        await message.answer("⚠️ چت فعالی وجود ندارد.", reply_markup=main_menu_kb())


@router.message(ChatState.in_chat, F.voice)
async def forward_voice(message: Message) -> None:
    """Relay voice messages instantly."""
    await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_voice(pid, message.voice.file_id),
    )


@router.message(ChatState.in_chat, F.photo)
async def forward_photo(message: Message) -> None:
    """Relay photos instantly (largest size)."""
    await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_photo(pid, message.photo[-1].file_id),
    )


@router.message(ChatState.in_chat, F.video | F.video_note | F.animation)
async def block_video(message: Message) -> None:
    """Strictly forbid videos / video notes / animations."""
    await message.answer("ارسال فیلم در این ربات مجاز نیست 🚫")


@router.message(ChatState.in_chat)
async def unsupported_media(message: Message) -> None:
    """Catch-all for any other content type during chat."""
    await message.answer("⚠️ این نوع محتوا پشتیبانی نمی‌شود.")
