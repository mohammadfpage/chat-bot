"""
Profile setup FSM flow, profile viewing, and blocked-users management.
"""

import logging

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from datetime import timedelta
from html import escape
from sqlalchemy import select

from database import async_session_factory, User, BlockList, CoinTransaction
from keyboards import (
    main_menu_kb,
    age_kb,
    city_kb,
    gender_kb,
    height_kb,
    confirm_profile_kb,
    profile_photo_choice_kb,
    photo_upload_kb,
    set_gender_kb,
    blocked_list_kb,
    wallet_kb,
    wallet_history_kb,
    profile_card_kb,
    profile_edit_kb,
    GENDER_FEMALE_LABEL,
    GENDER_MALE_LABEL,
)
from states import ProfileSetup, ProfileEdit, ChatState
from utils.economy import (
    wallet_info,
    claim_daily_bonus,
    match_cost_text,
    chat_lifetime_text,
    get_policy,
    fmt_coins,
    reason_label,
)

logger = logging.getLogger(__name__)

router = Router()

#: Button label → stored value. The reverse mapping is the only place that knows
#: what a gender is written as, so the database cannot end up with "Male", "male"
#: and "m" in three rows that all fail to match each other.
GENDER_BY_LABEL = {
    GENDER_MALE_LABEL: "male",
    GENDER_FEMALE_LABEL: "female",
}
from utils.helpers import format_user_profile

router = Router()

# Everything here (menus, wizards, panels) belongs to the PRIVATE chat —
# a group must never light up the bot's buttons.
router.message.filter(F.chat.type == ChatType.PRIVATE)


# ──────────────────────────────────────────────────
# Profile card rendering (shared by the menu and every edit)
# ──────────────────────────────────────────────────

async def _profile_photo_file_id(user, bot) -> str | None:
    """The file id to show on the card, or ``None`` for a text-only card.

    Keeps the rule the card has always used: the user must have opted in
    (``show_profile_photo``); a custom upload wins, otherwise the freshest
    Telegram profile photo is fetched live. The fetch is a Bot API call, so it
    must never run inside an open DB session — both callers pass a user that is
    already detached.
    """
    if not user.show_profile_photo:
        return None
    if user.profile_photo:
        return user.profile_photo
    photos = await bot.get_user_profile_photos(user.telegram_id, limit=1)
    if photos and photos.total_count > 0:
        return photos.photos[0][-1].file_id
    return None


async def _send_profile_card(target: Message, user) -> None:
    """Send the profile card with the «✏️ ویرایش پروفایل» button under it.

    Sent as a NEW message rather than edited in place: the card may carry a
    photo, and Telegram refuses to turn a photo message back into text. The
    edit button is inline so it sits directly beneath the card while the
    persistent reply menu stays on screen at the same time.
    """
    caption = format_user_profile(
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        username=user.username,
        age=user.age,
        city=user.city,
        height=user.height,
    )
    photo_file_id = await _profile_photo_file_id(user, target.bot)
    if photo_file_id:
        await target.answer_photo(
            photo=photo_file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=profile_card_kb(),
        )
    else:
        await target.answer(
            caption, parse_mode="HTML", reply_markup=profile_card_kb()
        )


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

    await _send_profile_card(message, user)

    # A profile completed before gender existed cannot be matched by «چت با
    # دختر» / «چت با پسر». The edit menu can set it, but this one-tap offer is
    # kept on the card so the fix stays visible without hunting for it.
    if user.gender is None:
        await message.answer(
            "🚻 برای فعال‌شدن «چت با دختر» / «چت با پسر» جنسیت خود را "
            "انتخاب کنید:",
            reply_markup=set_gender_kb(),
        )


@router.message(
    F.text.in_({GENDER_MALE_LABEL, GENDER_FEMALE_LABEL}),
    # CRITICAL: this handler is registered BEFORE the wizard's ``process_gender``
    # and has no state gate of its own, so without this exclusion it SWALLOWS
    # the «👩 زن» / «👨 مرد» answer during ``ProfileSetup.waiting_for_gender``
    # (both decorators match the same text; aiogram runs them in registration
    # order). The wizard then jumps straight to ``ChatState.idle`` with a
    # «ذخیره شد.» reply, never reaches the height step or the confirmation —
    # and ``is_profile_complete`` is never set, so the profile silently never
    # completes. Excluding the whole group lets ``process_gender`` handle it.
    # ``ProfileEdit`` is excluded for the same reason: its gender step must be
    # answered by ``edit_gender`` (which re-draws the card), not swallowed here.
    ~StateFilter(ProfileSetup, ProfileEdit),
)
async def set_gender_standalone(message: Message, state: FSMContext) -> None:
    """Answer the standalone «set your gender» prompt on an existing profile.

    Separate from :func:`process_gender` because this one must write straight to
    the database and return to the menu — there is no wizard to continue. Only
    reachable OUTSIDE the setup wizard (see the ``~StateFilter`` above).
    """
    gender = GENDER_BY_LABEL.get((message.text or "").strip())
    if gender is None:
        return

    # The reply is a network call, so it happens after the session closes.
    user_missing = False
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user_missing = True
        else:
            user.gender = gender
            await session.commit()

    if user_missing:
        await message.answer("ابتدا /start را بزنید.")
        return

    await state.set_state(ChatState.idle)
    await message.answer("ذخیره شد.", reply_markup=main_menu_kb())


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
    # Column is String(128): past that, SQLite would happily store it and
    # PostgreSQL would reject the row with a DataError at commit time — the
    # user would see a profile that never saves for no visible reason.
    if len(city) > 128:
        await message.answer("⚠️ نام شهر خیلی بلند است؛ حداکثر ۱۲۸ کاراکتر.")
        return

    await state.update_data(city=city)
    await state.set_state(ProfileSetup.waiting_for_gender)
    await message.answer(
        "جنسیت خود را انتخاب کنید:",
        reply_markup=gender_kb(),
    )


@router.message(ProfileSetup.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext) -> None:
    """Record the gender, which the targeted matching modes match against."""
    if message.text == "❌ انصراف":
        await state.set_state(ChatState.idle)
        await message.answer("لغو شد.", reply_markup=main_menu_kb())
        return

    gender = GENDER_BY_LABEL.get((message.text or "").strip())
    if gender is None:
        await message.answer("لطفاً یکی از گزینه‌ها را انتخاب کنید.",
                             reply_markup=gender_kb())
        return

    await state.update_data(gender=gender)
    await state.set_state(ProfileSetup.waiting_for_height)
    await message.answer(
        "قد خود را انتخاب کنید یا تایپ کنید:",
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
    # Column is String(32) — see the note in process_city; same DataError
    # story on PostgreSQL if a pasted paragraph gets in.
    if len(message.text) > 32:
        await message.answer("⚠️ قد خیلی بلند است؛ حداکثر ۳۲ کاراکتر وارد کنید.")
        return

    # Store the height as-is (e.g. "180 سانتی‌متر" or just "180")
    await state.update_data(height=message.text)

    # CRITICAL: transition to confirm state so next message is NOT caught here
    await state.set_state(ProfileSetup.waiting_for_confirm)

    data = await state.get_data()
    gender_text = "زن" if data.get("gender") == "female" else "مرد"
    # City/height are free text the user typed and this card is sent with the
    # bot's HTML default — an unescaped "<" in a city name makes Telegram
    # reject the whole message (hard rule: escape every user string).
    await message.answer(
        "<b>خلاصه پروفایل</b>\n\n"
        f"سن: {data['age']}\n"
        f"شهر: {escape(str(data['city']))}\n"
        f"جنسیت: {gender_text}\n"
        f"قد: {escape(str(data['height']))}\n\n"
        "تایید می‌کنید؟",
        parse_mode="HTML",
        reply_markup=confirm_profile_kb(),
    )


@router.message(ProfileSetup.waiting_for_confirm, F.text == "✅ تایید")
async def confirm_profile(message: Message, state: FSMContext) -> None:
    """Persist the completed profile after confirmation, then ask about the photo.

    This is the ONE moment the wizard's answers (which live only in FSM state)
    reach the database, so it has to be both complete and honest about failure:

    * An answer missing from the state — a Redis/Memory hiccup, or a state key
      written by an older build — must not become a ``KeyError`` that kills the
      handler with no message to the user. It is detected up front and the user
      is asked to redo the setup.
    * ``/start`` normally creates the ``users`` row, but if it is absent
      (registration race, a database that predates the row) the profile is
      written as ``INSERT`` rather than silently skipped by an ``if user:``.
    * A failed commit is logged with the real exception and reported to the
      user, and the confirmation keyboard is kept so «✅ تایید» can be tapped
      again — instead of the old behaviour where a commit error vanished into
      the logs while the user saw nothing.
    """
    data = await state.get_data()

    missing = [k for k in ("age", "city", "gender", "height") if data.get(k) in (None, "")]
    if missing:
        logger.warning(
            "Profile confirm: incomplete FSM data for %s (missing %s)",
            message.from_user.id,
            ", ".join(missing),
        )
        await state.set_state(ChatState.idle)
        await message.answer(
            "⚠️ اطلاعات پروفایل ناقص است. لطفاً دوباره «👤 پروفایل من» را بزنید "
            "و مراحل را از ابتدا کامل کنید.",
            reply_markup=main_menu_kb(),
        )
        return

    try:
        # The commit happens INSIDE the ``async with`` block — the session is
        # closed only after the transaction is durable, never before.
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()
            if user is None:
                # The row should already exist; recreate it rather than throw
                # the whole completed wizard away on a registration race.
                user = User(
                    telegram_id=message.from_user.id,
                    first_name=message.from_user.first_name,
                    username=message.from_user.username,
                )
                session.add(user)
            user.age = data["age"]
            user.city = data["city"]
            user.height = data["height"]
            user.gender = data.get("gender")
            user.is_profile_complete = True
            await session.commit()
    except Exception:
        logger.exception(
            "Profile commit failed for telegram_id=%s", message.from_user.id
        )
        await message.answer(
            "⚠️ ذخیرهٔ پروفایل با خطا مواجه شد. لطفاً چند لحظه بعد دوباره "
            "«✅ تایید» را بزنید.",
            reply_markup=confirm_profile_kb(),
        )
        return

    logger.info("Profile completed for telegram_id=%s", message.from_user.id)

    # ── Final question: show profile photo or not (separate step, not nested) ──
    await state.set_state(ProfileSetup.waiting_for_photo_choice)
    await message.answer(
        "🖼 <b>نمایش عکس پروفایل</b>\n\n"
        "آیا مایلید عکس پروفایل شما در کارت پروفایلتان نمایش داده شود؟\n"
        "می‌توانید از آخرین عکس پروفایل تلگرام خود استفاده کنید "
        "یا یک عکس دلخواه بفرستید.",
        parse_mode="HTML",
        reply_markup=profile_photo_choice_kb(),
    )


# ──────────────────────────────────────────────────
# FSM: profile photo choice  (last Telegram photo / custom / none)
# ──────────────────────────────────────────────────

async def _finish_profile(message: Message, state: FSMContext) -> None:
    """Finish setup: clear FSM and return to the main menu."""
    await state.clear()
    await message.answer(
        "✅ پروفایل شما با موفقیت تکمیل شد!\n"
        "حالا می‌توانید شروع به چت کنید.",
        reply_markup=main_menu_kb(),
    )


@router.message(
    ProfileSetup.waiting_for_photo_choice,
    F.text == "🖼 آخرین عکس پروفایل تلگرام",
)
async def use_last_telegram_photo(message: Message, state: FSMContext) -> None:
    """Show the user's latest Telegram profile photo on the profile card."""
    photos = await message.bot.get_user_profile_photos(message.from_user.id, limit=1)
    if not photos or photos.total_count == 0:
        await message.answer(
            "⚠️ شما عکس پروفایلی در تلگرام ندارید.\n"
            "می‌توانید «📷 ارسال عکس دلخواه» را بزنید "
            "یا «🙈 بدون عکس» را انتخاب کنید."
        )
        return

    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.show_profile_photo = True
            user.profile_photo = None  # None = always use the latest TG photo
            await session.commit()

    await _finish_profile(message, state)


@router.message(
    ProfileSetup.waiting_for_photo_choice,
    F.text == "📷 ارسال عکس دلخواه",
)
async def ask_custom_photo(message: Message, state: FSMContext) -> None:
    """Move to the custom-photo upload step (separate state — no nesting)."""
    await state.set_state(ProfileSetup.waiting_for_photo)
    await message.answer(
        "📷 <b>عکس دلخواه خود را بفرستید:</b>\n"
        "این عکس به عنوان عکس پروفایل شما ذخیره می‌شود.",
        parse_mode="HTML",
        reply_markup=photo_upload_kb(),
    )


@router.message(ProfileSetup.waiting_for_photo_choice, F.text == "🙈 بدون عکس")
async def no_profile_photo(message: Message, state: FSMContext) -> None:
    """User chose not to show any photo on their profile card."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.show_profile_photo = False
            user.profile_photo = None
            await session.commit()

    await _finish_profile(message, state)


@router.message(ProfileSetup.waiting_for_photo_choice)
async def invalid_photo_choice(message: Message) -> None:
    """Any other text on the photo-choice step → keep the same keyboard."""
    await message.answer("⚠️ لطفاً یکی از گزینه‌های زیر را انتخاب کنید:")


@router.message(ProfileSetup.waiting_for_photo, F.text == "❌ انصراف")
async def cancel_profile_photo(message: Message, state: FSMContext) -> None:
    """Go back to the photo-choice question (single clean step)."""
    await state.set_state(ProfileSetup.waiting_for_photo_choice)
    await message.answer(
        "🖼 <b>نمایش عکس پروفایل</b>\n\n"
        "آیا مایلید عکس پروفایل شما در کارت پروفایلتان نمایش داده شود؟",
        parse_mode="HTML",
        reply_markup=profile_photo_choice_kb(),
    )


@router.message(ProfileSetup.waiting_for_photo)
async def process_profile_photo(message: Message, state: FSMContext) -> None:
    """Save the user-sent photo as their profile photo."""
    if not message.photo:
        await message.answer(
            "⚠️ لطفاً یک عکس بفرستید یا روی «❌ انصراف» کلیک کنید."
        )
        return

    file_id = message.photo[-1].file_id
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.show_profile_photo = True
            user.profile_photo = file_id
            await session.commit()

    await _finish_profile(message, state)


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
# Profile Edit  («✏️ ویرایش پروفایل» — one field at a time)
# ──────────────────────────────────────────────────

async def _finish_field_edit(message: Message, state: FSMContext, **values) -> None:
    """Persist the edited field(s), restore the menu and re-draw the card.

    The DB write happens inside the ``async with`` block; every Telegram call
    (the "saved" note, the re-rendered card) happens after it closes — the same
    no-network-under-a-session rule the rest of this file follows. The returned
    ``user`` stays usable after the session closes because the factory is built
    with ``expire_on_commit=False``.
    """
    user = None
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            for field, value in values.items():
                setattr(user, field, value)
            await session.commit()

    await state.set_state(ChatState.idle)
    await message.answer("✅ تغییرات ذخیره شد.", reply_markup=main_menu_kb())

    if user is not None:
        await _send_profile_card(message, user)


async def _cancel_field_edit(message: Message, state: FSMContext) -> None:
    """«❌ انصراف» on any edit prompt: leave the edit, restore the menu."""
    await state.set_state(ChatState.idle)
    await message.answer("❌ ویرایش لغو شد.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "profile:edit")
async def cb_profile_edit(callback: CallbackQuery) -> None:
    """Open the field picker under the profile card."""
    text = "⚙️ <b>ویرایش پروفایل</b>\n\nکدام بخش را می‌خواهید تغییر دهید؟"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=profile_edit_kb()
        )
    except Exception:
        # A photo card cannot be edited into text — send the picker instead.
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=profile_edit_kb()
        )
    await callback.answer()


@router.callback_query(F.data == "profile:edit:back")
async def cb_profile_edit_back(callback: CallbackQuery) -> None:
    """Re-draw the profile card — the picker's back button."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

    await callback.answer()
    if user is not None:
        await _send_profile_card(callback.message, user)


@router.callback_query(F.data == "profile:edit:age")
async def cb_edit_age(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.waiting_for_age)
    await callback.message.answer(
        "🎂 <b>ویرایش سن</b>\n\nسن جدید خود را انتخاب یا تایپ کنید:",
        parse_mode="HTML",
        reply_markup=age_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit:city")
async def cb_edit_city(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.waiting_for_city)
    await callback.message.answer(
        "🏙 <b>ویرایش شهر</b>\n\nشهر خود را انتخاب یا تایپ کنید:",
        parse_mode="HTML",
        reply_markup=city_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit:gender")
async def cb_edit_gender(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.waiting_for_gender)
    await callback.message.answer(
        "🚻 <b>ویرایش جنسیت</b>\n\nجنسیت خود را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=gender_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit:height")
async def cb_edit_height(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.waiting_for_height)
    await callback.message.answer(
        "📏 <b>ویرایش قد</b>\n\nقد خود را انتخاب یا تایپ کنید:",
        parse_mode="HTML",
        reply_markup=height_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit:photo")
async def cb_edit_photo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.waiting_for_photo)
    await callback.message.answer(
        "🖼 <b>ویرایش عکس پروفایل</b>\n\n"
        "آیا مایلید عکس پروفایل شما نمایش داده شود؟",
        parse_mode="HTML",
        reply_markup=profile_photo_choice_kb(),
    )
    await callback.answer()


# ── one message handler per editable field ──

@router.message(ProfileEdit.waiting_for_age)
async def edit_age(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await _cancel_field_edit(message, state)
        return

    if (
        not message.text
        or not message.text.isdigit()
        or not (16 <= int(message.text) <= 50)
    ):
        await message.answer("⚠️ لطفاً یک عدد بین ۱۶ تا ۵۰ وارد کنید.")
        return

    await _finish_field_edit(message, state, age=int(message.text))


@router.message(ProfileEdit.waiting_for_city)
async def edit_city(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await _cancel_field_edit(message, state)
        return

    city = message.text
    if city == "✏️ شهر دیگر":
        await message.answer("📝 نام شهر خود را تایپ کنید:")
        return

    if not city or len(city) < 2:
        await message.answer("⚠️ لطفاً نام شهر معتبر وارد کنید.")
        return
    # See ``process_city``: past the column width (String(128)) PostgreSQL
    # rejects the row at commit, so the length is guarded before the write.
    if len(city) > 128:
        await message.answer("⚠️ نام شهر خیلی بلند است؛ حداکثر ۱۲۸ کاراکتر.")
        return

    await _finish_field_edit(message, state, city=city)


@router.message(ProfileEdit.waiting_for_gender)
async def edit_gender(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await _cancel_field_edit(message, state)
        return

    gender = GENDER_BY_LABEL.get((message.text or "").strip())
    if gender is None:
        await message.answer(
            "لطفاً یکی از گزینه‌ها را انتخاب کنید.", reply_markup=gender_kb()
        )
        return

    await _finish_field_edit(message, state, gender=gender)


@router.message(ProfileEdit.waiting_for_height)
async def edit_height(message: Message, state: FSMContext) -> None:
    if message.text == "❌ انصراف":
        await _cancel_field_edit(message, state)
        return

    if not message.text:
        await message.answer("⚠️ لطفاً قد خود را وارد کنید.")
        return
    # String(32) column — same DataError story as the city field.
    if len(message.text) > 32:
        await message.answer("⚠️ قد خیلی بلند است؛ حداکثر ۳۲ کاراکتر وارد کنید.")
        return

    await _finish_field_edit(message, state, height=message.text)


@router.message(ProfileEdit.waiting_for_photo, F.text == "🖼 آخرین عکس پروفایل تلگرام")
async def edit_photo_last_telegram(message: Message, state: FSMContext) -> None:
    photos = await message.bot.get_user_profile_photos(message.from_user.id, limit=1)
    if not photos or photos.total_count == 0:
        await message.answer(
            "⚠️ شما عکس پروفایلی در تلگرام ندارید.\n"
            "می‌توانید «📷 ارسال عکس دلخواه» را بزنید "
            "یا «🙈 بدون عکس» را انتخاب کنید."
        )
        return

    # ``profile_photo=None`` means "always use the latest Telegram photo".
    await _finish_field_edit(
        message, state, show_profile_photo=True, profile_photo=None
    )


@router.message(ProfileEdit.waiting_for_photo, F.text == "📷 ارسال عکس دلخواه")
async def edit_photo_custom(message: Message, state: FSMContext) -> None:
    await message.answer(
        "📷 <b>عکس دلخواه خود را بفرستید:</b>",
        parse_mode="HTML",
        reply_markup=photo_upload_kb(),
    )


@router.message(ProfileEdit.waiting_for_photo, F.text == "🙈 بدون عکس")
async def edit_photo_none(message: Message, state: FSMContext) -> None:
    await _finish_field_edit(
        message, state, show_profile_photo=False, profile_photo=None
    )


@router.message(ProfileEdit.waiting_for_photo, F.text == "❌ انصراف")
async def edit_photo_cancel(message: Message, state: FSMContext) -> None:
    await _cancel_field_edit(message, state)


@router.message(ProfileEdit.waiting_for_photo, F.photo)
async def edit_photo_upload(message: Message, state: FSMContext) -> None:
    await _finish_field_edit(
        message,
        state,
        show_profile_photo=True,
        profile_photo=message.photo[-1].file_id,
    )


@router.message(ProfileEdit.waiting_for_photo)
async def edit_photo_invalid(message: Message, state: FSMContext) -> None:
    """Any other text on the photo step → re-show the choices, stay in state."""
    await message.answer(
        "⚠️ لطفاً یک عکس بفرستید یا یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=profile_photo_choice_kb(),
    )


# ──────────────────────────────────────────────────
# Blocked Users List
# ──────────────────────────────────────────────────

async def _blocked_label(blocked_id: int) -> str:
    """Display label of one blocked user — never the numeric id, never a handle.

    This list is a screenshot-able surface, and the anonymity promise of the
    bot covers it too: the old default was ``کاربر {id}``, which printed the
    identifier straight onto the screen. The first name is kept because the
    blocker pulled this person out of a conversation the two of them were in;
    the @username is dropped because a handle is searchable across Telegram
    far beyond that conversation.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == blocked_id)
        )
        target_user = result.scalar_one_or_none()
    if target_user and target_user.first_name:
        return target_user.first_name
    return "کاربر بلاک‌شده"


async def _blocked_entries_for_kb(user_id: int) -> list[dict]:
    """The caller's blocks as ``{blocked_id, label}`` rows for the keyboard.

    One builder for the first render AND every unblock refresh — the two
    copies used to be written out twice, which is exactly how they drift
    apart (and how each one grew its own copy of the leaking label).
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(BlockList.blocker_id == user_id)
        )
        blocked_entries = result.scalars().all()

    return [
        {
            "blocked_id": entry.blocked_id,
            "label": await _blocked_label(entry.blocked_id),
        }
        for entry in blocked_entries
    ]


@router.message(F.text == "⛔️ لیست مسدودی‌ها")
async def show_blocked_list(message: Message) -> None:
    """Show the user's blocked-users list as an inline keyboard."""
    entries_for_kb = await _blocked_entries_for_kb(message.from_user.id)

    if not entries_for_kb:
        await message.answer(
            "✅ لیست مسدودی‌ها خالی است.\nهیچ کاربری را بلاک نکرده‌اید.",
            reply_markup=main_menu_kb(),
        )
        return

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
    try:
        target_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ شناسهٔ نامعتبر.", show_alert=True)
        return
    user_id = callback.from_user.id

    # Resolved BEFORE the delete — afterwards the row (and any name it could
    # be labelled with) is gone. The alert carries the name, never the id.
    label = await _blocked_label(target_id)

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

    await callback.answer(f"✅ {label} رفع بلاک شد.", show_alert=True)

    # Refresh the blocked list inline message
    entries_for_kb = await _blocked_entries_for_kb(user_id)

    if not entries_for_kb:
        try:
            await callback.message.edit_text(
                "✅ لیست مسدودی‌ها خالی است.\nهیچ کاربری را بلاک نکرده‌اید.",
            )
        except Exception:
            pass
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=blocked_list_kb(entries_for_kb),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────
# Misc menu items
# ──────────────────────────────────────────────────

@router.message(F.text == "🔗 لینک ناشناس من")
async def anonymous_link(message: Message) -> None:
    """Show the user their shareable anonymous chat link."""
    bot_username = (await message.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    await message.answer(
        "<b>لینک ناشناس شما</b>\n\n"
        f"<code>{link}</code>\n\n"
        "این لینک را بفرستید تا کسی بتواند به شما پیام ناشناس بدهد.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


def _price(value) -> str:
    """Render one service price: «رایگان» instead of a bare «0 سکه».

    Every number on the wallet card is read live from ``bot_policy``, so a
    service the admin just made free reads as free here on the next open.
    """
    value = value or 0
    return "رایگان" if value <= 0 else f"<b>{fmt_coins(value)} سکه</b>"


def _format_wallet(info: dict) -> str:
    """Build the wallet card text (shared by the menu and callbacks)."""
    if info["daily_claimable"]:
        daily_line = "جایزهٔ روزانه: <b>در دسترس</b>"
    else:
        wait = info["daily_wait_seconds"]
        daily_line = (
            f"جایزهٔ روزانه: <b>{wait // 3600} ساعت و "
            f"{wait % 3600 // 60} دقیقه</b> دیگر"
        )

    return (
        "<b>امتیازات و سکه</b>\n\n"
        f"🪙 سکه: <b>{fmt_coins(info['coins'])}</b>\n"
        f"📅 {daily_line} (+{info['daily_bonus_coins']} سکه)\n"
        f"💎 اشتراک ویژه: {'فعال' if info['premium'] else '—'}\n"
        f"🎁 دعوت‌های موفق: <b>{info['referral_count']}</b>\n\n"
        "<b>هزینهٔ سرویس‌ها</b>\n"
        f"• اتصال شانسی: {_price(info['random_chat_cost'])}\n"
        f"• چت با دختر: {_price(info['chat_girl_cost'])}\n"
        f"• چت با پسر: {_price(info['chat_boy_cost'])}\n"
        f"• نجوا: {_price(info['whisper_cost'])}\n\n"
        f"{chat_lifetime_text(info['chat_lifetime_hours'])}\n"
        "پیام دادن در طول چت رایگان است."
    )


@router.message(F.text == "🏆 امتیازات و سکه")
async def scores_coinage(message: Message) -> None:
    """Show the user's wallet: coins, daily bonus, referral count."""
    info = await wallet_info(message.from_user.id)
    if info is None:
        await message.answer("ابتدا /start را بزنید.", reply_markup=main_menu_kb())
        return

    await message.answer(
        _format_wallet(info),
        parse_mode="HTML",
        reply_markup=wallet_kb(daily_claimable=info["daily_claimable"]),
    )


@router.callback_query(F.data == "wallet:daily")
async def wallet_daily_bonus(callback: CallbackQuery) -> None:
    """Claim the once-per-day coin bonus and refresh the wallet screen."""
    ok, coins, wait = await claim_daily_bonus(callback.from_user.id)
    if not ok:
        if wait > 0:
            h, m = divmod(wait // 60, 60)
            await callback.answer(
                f"جایزهٔ روزانه تا {h} ساعت و {m} دقیقه دیگر آزاد می‌شود.",
                show_alert=True,
            )
        return

    await callback.answer(
        f"{coins} سکه دریافت شد.",
        show_alert=True,
    )

    info = await wallet_info(callback.from_user.id)
    if info is None:
        return
    try:
        await callback.message.edit_text(
            _format_wallet(info),
            parse_mode="HTML",
            reply_markup=wallet_kb(daily_claimable=info["daily_claimable"]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "wallet:info")
async def wallet_info_pressed(callback: CallbackQuery) -> None:
    """The «اطلاعات» row, shown when there is nothing to claim yet."""
    await callback.answer(
        "سکه کافی دارید. جایزهٔ روزانه هر ۲۴ ساعت یک‌بار آزاد می‌شود.",
        show_alert=True,
    )


@router.callback_query(F.data == "wallet:back")
async def wallet_back(callback: CallbackQuery) -> None:
    """Return to the main menu from the wallet screen."""
    await callback.message.answer(
        "از منوی اصلی:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "wallet:history")
async def wallet_history(callback: CallbackQuery) -> None:
    """The caller's ten most recent ledger movements, newest first.

    Read-only over ``coin_ledger`` (the same table the admin report uses), so
    the user always sees what the system actually recorded. The session is
    closed before the message is edited — no Telegram call while a DB session
    is open.
    """
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CoinTransaction)
                .where(CoinTransaction.user_id == callback.from_user.id)
                .order_by(
                    CoinTransaction.created_at.desc(), CoinTransaction.id.desc()
                )
                .limit(10)
            )
        ).scalars().all()

    if not rows:
        await callback.answer(
            "هنوز حرکتی در کیف پول شما ثبت نشده است.", show_alert=True
        )
        return

    # The ledger stores naive UTC; the audience reads Tehran time (+3:30).
    tehran = timedelta(hours=3, minutes=30)
    lines = ["📜 <b>آخرین حرکت‌های سکه</b> (۱۰ مورد آخر)\n"]
    for r in rows:
        sign = "+" if (r.amount or 0) >= 0 else "-"
        when = (r.created_at + tehran).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{sign}{fmt_coins(abs(r.amount or 0))} — {escape(reason_label(r.reason))}"
            f" · <code>{when}</code>"
        )

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=wallet_history_kb(),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "wallet:open")
async def wallet_open(callback: CallbackQuery) -> None:
    """Re-render the wallet card — the back button of the history drill-down."""
    info = await wallet_info(callback.from_user.id)
    if info is None:
        await callback.answer("ابتدا /start را بزنید.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            _format_wallet(info),
            parse_mode="HTML",
            reply_markup=wallet_kb(daily_claimable=info["daily_claimable"]),
        )
    except Exception:
        pass
    await callback.answer()


@router.message(F.text == "🎁 دعوت دوستان")
async def referral(message: Message) -> None:
    """Show the user's referral link and what each invite pays them.

    Every number here comes from ``bot_info``, which reads ``bot_policy`` — so
    the card can never promise a reward different from the one
    :func:`utils.economy.reward_referral` actually pays.
    """
    info = await wallet_info(message.from_user.id)
    if info is None:
        await message.answer("ابتدا /start را بزنید.", reply_markup=main_menu_kb())
        return

    bot_username = (await message.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref{message.from_user.id}"

    invitee = info["referral_invitee_coins"]
    invitee_line = (
        f"دوست شما هم <b>{invitee} سکه</b> هدیه می‌گیرد.\n"
        if invitee
        else ""
    )

    await message.answer(
        "🎁 <b>دعوت دوستان</b>\n\n"
        f"🔗 این لینک را بفرستید:\n"
        f"<code>{link}</code>\n\n"
        f"🏆 <b>پاداش هر دعوت موفق</b>\n"
        f"• شما: <b>{info['referral_coin_reward']} سکه</b>"
        + (
            f" + <b>{info['referral_premium_days']} روز اشتراک</b>"
            if info["referral_premium_days"]
            else ""
        )
        + "\n"
        + invitee_line
        + f"\n🪙 دعوت‌های موفق شما: <b>{info['referral_count']}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
