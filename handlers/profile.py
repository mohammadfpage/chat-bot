"""
Profile setup FSM flow, profile viewing, and blocked-users management.
"""

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from database import async_session_factory, User, BlockList
from keyboards import (
    main_menu_kb,
    age_kb,
    city_kb,
    height_kb,
    confirm_profile_kb,
    blocked_list_kb,
)
from states import ProfileSetup, ChatState
from utils.helpers import format_user_profile

router = Router()


# ──────────────────────────────────────────────────
# Show profile or trigger setup
# ──────────────────────────────────────────────────

@router.message(F.text == "👤 پروفایل من")
async def show_profile(message: Message, state: FSMContext) -> None:
    """Display profile card or start the setup flow."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

    if user is None:
        await message.answer("⚠️ ابتدا /start را بزنید.")
        return

    if not user.is_profile_complete:
        await _start_setup(message, state)
        return

    # ── Build profile card ──
    caption = format_user_profile(
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        username=user.username,
        age=user.age,
        city=user.city,
        height=user.height,
    )

    # ── Fetch profile photo dynamically (never stored) ──
    photos = await message.bot.get_user_profile_photos(message.from_user.id, limit=1)
    if photos and photos.total_count > 0:
        photo_file_id = photos.photos[0][-1].file_id
        await message.answer_photo(
            photo=photo_file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# FSM: step-by-step profile setup
# ──────────────────────────────────────────────────

async def _start_setup(message: Message, state: FSMContext) -> None:
    """Kick off the profile setup wizard."""
    await state.set_state(ProfileSetup.waiting_for_age)
    await message.answer(
        "📝 <b>تکمیل پروفایل</b>\n\n"
        "لطفاً سن خود را وارد کنید:",
        parse_mode="HTML",
        reply_markup=age_kb(),
    )


@router.message(ProfileSetup.waiting_for_age)
async def process_age(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await state.set_state(ChatState.idle)
        await message.answer("❌ تکمیل پروفایل لغو شد.", reply_markup=main_menu_kb())
        return

    if not message.text or not message.text.isdigit() or not (16 <= int(message.text) <= 50):
        await message.answer("⚠️ لطفاً یک عدد بین ۱۶ تا ۵۰ وارد کنید.")
        return

    await state.update_data(age=int(message.text))
    await state.set_state(ProfileSetup.waiting_for_city)
    await message.answer(
        "🏙 شهر خود را انتخاب کنید یا تایپ کنید:",
        reply_markup=city_kb(),
    )


@router.message(ProfileSetup.waiting_for_city)
async def process_city(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await state.set_state(ChatState.idle)
        await message.answer("❌ تکمیل پروفایل لغو شد.", reply_markup=main_menu_kb())
        return

    city = message.text
    if city == "✏️ شهر دیگر":
        await message.answer("📝 نام شهر خود را تایپ کنید:")
        return

    if not city or len(city) < 2:
        await message.answer("⚠️ لطفاً نام شهر معتبر وارد کنید.")
        return

    await state.update_data(city=city)
    await state.set_state(ProfileSetup.waiting_for_height)
    await message.answer(
        "📏 قد خود را انتخاب کنید یا به صورت دستی تایپ کنید:",
        reply_markup=height_kb(),
    )


@router.message(ProfileSetup.waiting_for_height)
async def process_height(message: Message, state: FSMContext) -> None:
    """Accept height — either from keyboard button or manually typed text."""
    if message.text == "❌ انصراف":
        await state.set_state(ChatState.idle)
        await message.answer("❌ تکمیل پروفایل لغو شد.", reply_markup=main_menu_kb())
        return

    if not message.text:
        await message.answer("⚠️ لطفاً قد خود را وارد کنید.")
        return

    # Store the height as-is (e.g. "180 سانتی‌متر" or just "180")
    await state.update_data(height=message.text)

    # CRITICAL: transition to confirm state so next message is NOT caught here
    await state.set_state(ProfileSetup.waiting_for_confirm)

    data = await state.get_data()
    await message.answer(
        "📋 <b>خلاصه پروفایل شما:</b>\n\n"
        f"🎂 سن: {data['age']}\n"
        f"🏙 شهر: {data['city']}\n"
        f"📏 قد: {data['height']}\n\n"
        "آیا تایید می‌کنید؟",
        parse_mode="HTML",
        reply_markup=confirm_profile_kb(),
    )


@router.message(ProfileSetup.waiting_for_confirm, F.text == "✅ تایید")
async def confirm_profile(message: Message, state: FSMContext) -> None:
    """Persist the completed profile after confirmation."""
    data = await state.get_data()
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.age = data["age"]
            user.city = data["city"]
            user.height = data["height"]
            user.is_profile_complete = True
            await session.commit()

    await state.clear()
    await message.answer(
        "✅ پروفایل شما با موفقیت تکمیل شد!\n"
        "حالا می‌توانید شروع به چت کنید.",
        reply_markup=main_menu_kb(),
    )


@router.message(ProfileSetup.waiting_for_confirm, F.text == "✏️ ویرایش")
async def edit_profile(message: Message, state: FSMContext) -> None:
    """Restart the setup wizard from the beginning."""
    await _start_setup(message, state)


@router.message(ProfileSetup.waiting_for_confirm, F.text == "❌ لغو")
async def cancel_confirm(message: Message, state: FSMContext) -> None:
    """Cancel profile setup at the confirmation step."""
    await state.clear()
    await message.answer("❌ تکمیل پروفایل لغو شد.", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# Blocked Users List
# ──────────────────────────────────────────────────

@router.message(F.text == "⛔️ لیست مسدودی‌ها")
async def show_blocked_list(message: Message) -> None:
    """Show the user's blocked-users list as an inline keyboard."""
    user_id = message.from_user.id

    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(BlockList.blocker_id == user_id)
        )
        blocked_entries = result.scalars().all()

    if not blocked_entries:
        await message.answer(
            "✅ لیست مسدودی‌ها خالی است.\nهیچ کاربری را بلاک نکرده‌اید.",
            reply_markup=main_menu_kb(),
        )
        return

    # Build label list: try to resolve names from User table
    entries_for_kb: list[dict] = []
    for entry in blocked_entries:
        label = f"کاربر {entry.blocked_id}"
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == entry.blocked_id)
            )
            target_user = result.scalar_one_or_none()
            if target_user and target_user.first_name:
                label = f"{target_user.first_name}"
                if target_user.username:
                    label += f" (@{target_user.username})"
        entries_for_kb.append({
            "blocked_id": entry.blocked_id,
            "label": label,
        })

    await message.answer(
        "⛔ <b>لیست مسدودی‌های شما:</b>\n\n"
        "روی دکمه ❌ کلیک کنید تا کاربر را رفع بلاک کنید:",
        parse_mode="HTML",
        reply_markup=blocked_list_kb(entries_for_kb),
    )


@router.callback_query(F.data == "blocked_list:back")
async def cb_blocked_list_back(callback: CallbackQuery) -> None:
    """Return to main menu from blocked list."""
    await callback.message.edit_text("✅ بازگشت به منوی اصلی.")
    await callback.message.answer(
        "منوی اصلی:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("unblock:"))
async def cb_unblock_user(callback: CallbackQuery) -> None:
    """Remove a block entry and confirm to the user."""
    target_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_id) & (BlockList.blocked_id == target_id)
            )
        )
        entry = result.scalar_one_or_none()
        if entry:
            await session.delete(entry)
            await session.commit()

    await callback.answer(f"✅ کاربر {target_id} رفع بلاک شد.", show_alert=True)

    # Refresh the blocked list inline message
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(BlockList.blocker_id == user_id)
        )
        blocked_entries = result.scalars().all()

    if not blocked_entries:
        try:
            await callback.message.edit_text(
                "✅ لیست مسدودی‌ها خالی است.\nهیچ کاربری را بلاک نکرده‌اید.",
            )
        except Exception:
            pass
        return

    # Rebuild the keyboard
    entries_for_kb: list[dict] = []
    for entry in blocked_entries:
        label = f"کاربر {entry.blocked_id}"
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == entry.blocked_id)
            )
            target_user = result.scalar_one_or_none()
            if target_user and target_user.first_name:
                label = f"{target_user.first_name}"
                if target_user.username:
                    label += f" (@{target_user.username})"
        entries_for_kb.append({
            "blocked_id": entry.blocked_id,
            "label": label,
        })

    try:
        await callback.message.edit_reply_markup(
            reply_markup=blocked_list_kb(entries_for_kb),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────
# Misc menu items
# ──────────────────────────────────────────────────

@router.message(F.text == "📬 لینک ناشناس من")
async def anonymous_link(message: Message) -> None:
    """Show the user their shareable anonymous chat link."""
    bot_username = (await message.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    await message.answer(
        f"📬 <b>لینک ناشناس شما:</b>\n\n"
        f"<code>{link}</code>\n\n"
        "این لینک را برای دیگران بفرستید تا با شما چت کنند.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "🏆 امتیازات و سکه")
async def scores(message: Message) -> None:
    """Placeholder for future coins / points system."""
    await message.answer(
        "🏆 <b>سیستم امتیازات</b>\n\n"
        "این بخش در حال توسعه است.\n"
        "به زودی قابلیت سکه و امتیاز اضافه خواهد شد!",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
