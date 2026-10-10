"""
/start command, deep link handling, admin dual-panel, and help/rules.

Centralized /start handler:
  - Registers new users / checks bans
  - Deep link payload → FSM state for anonymous message
  - Admin dual-panel (👑 Admin Panel / 👤 User Panel)
  - Regular user main menu
"""

import logging
from html import escape

from aiogram import Bot, Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import settings
from database import async_session_factory, User
from handlers.anon_chat import ANON_DEEP_PARAM, handle_anon_chat_deep_link
from handlers.inline_anon import (
    HELP_START_PARAM,
    inline_help_text,
    join_prompt_text,
)
from keyboards import (
    main_menu_kb,
    admin_dual_panel_kb,
    admin_panel_kb,
    INLINE_HELP_LABEL,
    whisper_join_kb,
    welcome_group_kb,
)
from states import ChatState, AnonChatStates
from utils.economy import (
    get_policy,
    reward_referral,
    grant_invitee_bonus,
    chat_lifetime_text,
    log_coin,
    match_cost_text,
)
from utils.group_commands import private_commands_text
from utils.membership import missing_required_chats
from utils.whisper_config import get_whisper_config

logger = logging.getLogger(__name__)
router = Router()


async def _bot_username(bot: Bot) -> str:
    """Our own username, or ``""`` when it cannot be read.

    Every link that has to name the bot (``t.me/<bot>?admin``,
    ``?startgroup``) needs this, and ``Bot.me()`` is cached by aiogram, so the
    repeated calls cost nothing. Failure is not exceptional enough to raise: the
    caller degrades to a card with no button rather than losing the message.
    """
    try:
        return (await bot.me()).username or ""
    except Exception as exc:  # noqa: BLE001 — a link is a nicety, not the feature
        logger.info("Could not read our own username: %s", exc)
        return ""


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
    # The ban replies below are network calls: they were composed inside the
    # session and delivered after it closed, so a slow Telegram API never pins
    # one of the five pooled connections.
    is_new_user = False
    banned = False
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            policy = await get_policy()
            session.add(
                User(
                    telegram_id=user_id,
                    first_name=message.from_user.first_name,
                    username=message.from_user.username,
                    coins=policy.welcome_coins,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # Lost the registration race: an inline whisper or a button
                # tap ran ensure_user() in the same millisecond and THEIR
                # insert won — including its own atomic welcome-coins +
                # ledger write. Re-read that row (and its ban flag) without
                # granting anything again.
                await session.rollback()
                result = await session.execute(
                    select(User).where(User.telegram_id == user_id)
                )
                user = result.scalar_one_or_none()
                if user is None:  # pragma: no cover - row vanished again
                    raise
                is_new_user = True
                if user.is_banned:
                    banned = True
            else:
                # Receipt for the sign-up bonus: the admin panel's per-user
                # report counts it from here rather than from a guess.
                if policy.welcome_coins:
                    await log_coin(user_id, policy.welcome_coins, "welcome")
                is_new_user = True
        elif user.is_banned:
            banned = True

    if banned:
        await message.answer("شما از ربات محروم شده‌اید.")
        return

    # ── Groups stay silent ──
    # A group is not somewhere the bot may write on its own: no menu, no
    # panels, no command guide. The one thing a group gets is the single card
    # posted when the bot is added (see ``handlers.group_lifecycle``), and
    # every other answer the guide used to print here belongs to the private
    # chat — where the reply keyboard and the deep links actually live.
    if message.chat.type != ChatType.PRIVATE:
        return

    # ── Deep-linked help: start=help ──
    # The button above the inline results opens /start help. Telegram has just
    # STARTED the conversation, so this is the first moment we are allowed to
    # write to this user at all — which is the whole point of routing the guide
    # through a deep link instead of DMing it from the picker.
    if payload == HELP_START_PARAM:
        await _handle_inline_help(message)
        return

    # ── Anonymous-chat deep link: start=anon_chat_{requester_id} ──
    # Someone who pressed «💬 شروع چت ناشناس» on a card in a group without ever
    # having started the bot arrives here, and this is the FIRST moment writing
    # to them is legal — which is exactly what ``handlers.anon_chat`` could not
    # do from the callback.
    if payload and payload.startswith(ANON_DEEP_PARAM):
        requester_id = payload[len(ANON_DEEP_PARAM) :]
        if not requester_id.isdigit():
            await message.answer(
                "⚠️ این لینک معتبر نیست.",
                reply_markup=main_menu_kb(),
            )
            return
        await state.set_state(ChatState.idle)
        if await handle_anon_chat_deep_link(message, int(requester_id)):
            return

    # ── Referral deep link: start=ref{inviter_id} ──
    if payload and payload.startswith("ref"):
        ref_id_text = payload[3:]
        if ref_id_text.isdigit():
            inviter_id = int(ref_id_text)
            if inviter_id != user_id:
                await _handle_referral(message, state, user_id, inviter_id, is_new_user)
                return
        await message.answer(
            "⚠️ لینک دعوت نامعتبر است.",
            reply_markup=main_menu_kb(),
        )
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
            f"سلام {escape(message.from_user.first_name or 'کاربر')}.\n"
            "از پنل زیر انتخاب کنید:",
            reply_markup=admin_dual_panel_kb(),
        )
        return

    # ── Regular user ──
    # The three matching modes and their prices are stated up front: this is the
    # only screen a new user sees, and the 1-coin buttons should never be a
    # surprise discovered at the moment of connecting.
    policy = await get_policy()
    await message.answer(
        f"سلام {escape(message.from_user.first_name or 'کاربر')}.\n"
        f"به ربات چت آنلاین خوش آمدید.\n\n"
        f"💰 <b>هزینهٔ اتصال</b>\n{match_cost_text(policy)}\n\n"
        f"{chat_lifetime_text(policy.chat_lifetime_hours)}\n\n"
        "از منوی زیر استفاده کنید:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Deep-linked inline help — start=help
# ──────────────────────────────────────────────────

async def _handle_inline_help(message: Message) -> None:
    """Answer the «📖 راهنمای ارسال نجوا» button above the inline results.

    Reached only through a deep link, which means Telegram has just opened the
    private chat — so this reply is safe to send in a way a DM from the
    ``inline_query`` handler was not (that path throws for anyone who has not
    started the bot, which is precisely who presses this button).

    The forced-join prompt rides along on the same screen when it applies, so
    the one place a new user lands after asking "how does this work" is also
    the place that tells them what to do before the feature works for them.
    """
    bot = message.bot
    bot_username = (await bot.me()).username or "bot"
    user_id = message.from_user.id

    missing = await missing_required_chats(bot, user_id, force_refresh=True)

    if missing:
        await message.answer(
            inline_help_text(bot_username),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.answer(
            join_prompt_text(missing),
            parse_mode="HTML",
            # Default callback: "whisper:check" already re-checks membership and
            # has a clean "nothing was pending" ending, so the deep-linked help
            # needs no handler of its own.
            reply_markup=whisper_join_kb(missing),
        )
        return

    await message.answer(
        inline_help_text(bot_username),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Referral deep link — start=ref{inviter_id}
# ──────────────────────────────────────────────────

async def _handle_referral(
    message: Message,
    state: FSMContext,
    invitee_id: int,
    inviter_id: int,
    is_new_user: bool,
) -> None:
    """Handle a referral link click.

    Only brand-new users (first /start) count as a valid referral.
    The inviter gets coins + premium days; the invitee is told about the
    welcome bonus and the inviter is notified of the reward.
    """
    await state.set_state(ChatState.idle)

    # Validate the inviter exists
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == inviter_id)
        )
        inviter = result.scalar_one_or_none()

    if inviter is None:
        await message.answer(
            "این لینک دعوت معتبر است، اما دعوت‌کننده هنوز ربات را استارت نکرده است.",
            reply_markup=main_menu_kb(),
        )
        return

    # Mark the referral (only once, only for brand-new users)
    credited = False
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == invitee_id)
        )
        invitee = result.scalar_one_or_none()

        if (
            invitee
            and is_new_user
            and invitee.referred_by is None
            and inviter_id != invitee_id
        ):
            invitee.referred_by = inviter_id
            await session.commit()
            credited = True

    if not credited:
        await _finish_start(message, state, is_admin=int(invitee_id) in settings.admin_ids_list)
        return

    # Grant the reward to the inviter. Both amounts are read live from
    # bot_policy by reward_referral, so the panel's number is the number paid.
    coins, days, total_refs = await reward_referral(inviter_id)

    # Grant the welcome bonus to the invited member (both sides win)
    invitee_bonus = await grant_invitee_bonus(invitee_id)

    bonus_line = (
        f"<b>{invitee_bonus} سکه</b> خوش‌آمد به حساب شما اضافه شد.\n"
        if invitee_bonus
        else ""
    )
    reward_line = (
        f"<b>{coins} سکه</b> + <b>{days} روز اشتراک</b>"
        if days
        else f"<b>{coins} سکه</b>"
    )
    await message.answer(
        "🎉 <b>دعوت موفق</b>\n\n"
        f"{bonus_line}"
        "هر دوستی که با لینک شما بیاید، به شما "
        f"{reward_line} می‌دهد.\n\n"
        "از منوی اصلی:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )

    # Notify the inviter
    try:
        await message.bot.send_message(
            inviter_id,
            "🎁 <b>یک دعوت جدید</b>\n\n"
            "یکی از دوستانتان با لینک شما وارد شد.\n\n"
            f"🪙 پاداش شما: <b>{coins} سکه</b>"
            + (f" + <b>{days} روز اشتراک</b>" if days else "")
            + f"\n🔗 تعداد دعوت‌های شما: <b>{total_refs}</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    except Exception as e:
        logger.warning("Failed to notify inviter %s: %s", inviter_id, e)


# ──────────────────────────────────────────────────
# Shared finish — shows admin panel or main menu
# ──────────────────────────────────────────────────

async def _finish_start(message: Message, state: FSMContext, *, is_admin: bool) -> None:
    """Route to admin dual-panel or the regular user main menu."""
    if is_admin:
        await message.answer(
            f"سلام {escape(message.from_user.first_name or 'کاربر')}.\n\n"
            "از پنل زیر انتخاب کنید:",
            reply_markup=admin_dual_panel_kb(),
        )
        return

    policy = await get_policy()
    await message.answer(
        f"سلام {escape(message.from_user.first_name or 'کاربر')}.\n"
        f"به ربات چت آنلاین خوش آمدید.\n\n"
        f"💰 <b>هزینهٔ اتصال</b>\n{match_cost_text(policy)}\n\n"
        f"{chat_lifetime_text(policy.chat_lifetime_hours)}\n\n"
        "از منوی زیر استفاده کنید:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )

    # The whisper itself needs a group, and the bot needs admin rights in it to
    # build the roster that makes ``@username`` targeting work. Both are one tap
    # away here, so the private welcome is also where the feature gets switched
    # on — see :func:`keyboards.user.welcome_group_kb`.
    card = welcome_group_kb(await _bot_username(message.bot))
    if card is not None:
        try:
            await message.answer(
                "برای ارسال نجوا، ربات را به گروه خود اضافه کنید:",
                reply_markup=card,
            )
        except Exception as exc:
            # A courtesy card; never let it take /start down with it.
            logger.info("Could not post the add-to-group card: %s", exc)


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

@router.message(F.text == "👑 پنل مدیریت", F.chat.type == ChatType.PRIVATE)
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


@router.message(F.text == "👤 پنل کاربری", F.chat.type == ChatType.PRIVATE)
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
    """Verify the user joined ALL required channels after 'عضو شدم'."""
    # The SAME funnel every other gate uses: admin panel rows, the legacy
    # single-chat fields and ``.env``, with the require_join switch honoured.
    # The old code here read only the ``.env`` pair, so «تأیید عضویت» declared
    # success while a panel-configured channel was still unchecked — the user
    # then bounced off the middleware anyway, having just been told they were
    # verified. The force_refresh is what makes the tap after joining stick:
    # without it the 30s cache still says "left".
    try:
        missing = await missing_required_chats(
            callback.bot, callback.from_user.id, force_refresh=True
        )
    except Exception as exc:
        # Fail OPEN, matching utils.membership.is_member: an unverifiable
        # lookup must not tell a possibly-valid member "you are not in the
        # channel", and must not crash the callback into a dead spinner.
        logger.warning("Membership re-check failed — accepting: %s", exc)
        missing = []

    if missing:
        names = ", ".join(f"«{escape(chat.title)}»" for chat in missing)
        await callback.answer(
            f"❌ شما هنوز عضو {names} نیستید!\nلطفاً ابتدا عضو شوید.",
            show_alert=True,
        )
        return

    await callback.answer("✅ عضویت تایید شد!")

    # The join prompt now lives in the PRIVATE chat only — the force-join
    # middleware never writes it into a group — so there is nothing here to
    # clean up in a group any more. The prompt card itself is the BOT's own
    # message and is never deleted: its buttons are simply retired with an
    # edit, which leaves the transcript intact.
    #
    # ``callback.message`` is None whenever the card is older than a few
    # minutes (Telegram then reports the callback with an inaccessible
    # message), so it cannot be assumed here. That missing guard used to turn
    # a *successful* membership check into a crash, and the user was left
    # staring at a spinner having just been told «عضویت تایید شد».
    prompt = callback.message
    in_group = bool(prompt and prompt.chat.type != ChatType.PRIVATE)

    if prompt is not None and not in_group:
        try:
            await prompt.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    if in_group:
        return

    await state.set_state(ChatState.idle)
    notice = "عضویت شما تایید شد.\n\nاز منوی زیر استفاده کنید:"
    if prompt is not None:
        await prompt.answer(notice, reply_markup=main_menu_kb())
        return

    # The card is unreachable (or lives in a channel we cannot post in) —
    # deliver the welcome privately so the user is never left without a way in.
    try:
        await callback.bot.send_message(
            callback.from_user.id, notice, reply_markup=main_menu_kb()
        )
    except Exception as exc:
        logger.warning("Could not deliver the post-join menu: %s", exc)


# ──────────────────────────────────────────────────
# Help / Rules
# ──────────────────────────────────────────────────

@router.message(Command("help"))
@router.message(F.text == "📖 راهنما و قوانین")
async def cmd_help(message: Message) -> None:
    """The command guide — private chat only.

    Its text comes from :mod:`utils.group_commands`, which is also what
    ``set_my_commands`` is built from, so the Telegram menu and this answer
    can never drift apart. A group gets nothing.
    """
    # A group is answered with nothing: the guide there would be a message
    # nobody asked for in a chat the bot is only a member of.
    if message.chat.type != ChatType.PRIVATE:
        return

    config = await get_whisper_config()
    bot_username = (await message.bot.me()).username or "bot"

    # The policy row goes in so the chat length and message cap printed on the
    # card are the ones the bot actually enforces, not a copy that can rot.
    await message.answer(
        private_commands_text(
            bot_username, whisper=config.enabled, policy=await get_policy()
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Inline-mode help card  (main-menu button + /inline)
# ──────────────────────────────────────────────────

@router.message(Command("inline"))
@router.message(F.text == INLINE_HELP_LABEL)
async def cmd_inline_help(message: Message) -> None:
    """Explain ``@bot <target> <message>`` — how to send a private, anonymous
    message from any chat without the bot ever writing into that chat.

    Registered on ``start_router`` (the LAST router) on purpose: live
    sessions own their own text catch-alls. Those catch-alls now refuse to relay
    bot control labels so a menu button is never delivered to the partner as
    chat text — the real-time session (``handlers.anon_chat``) passes this one
    through with SkipHandler so it opens here, while the inbox session
    (``handlers.anonymous``) answers with a "close the chat first" warning.
    """
    # A group gets nothing: this card only makes sense in the private chat
    # where the inline query is actually typed. Checked before ``me()`` so the
    # group path costs no API round trip at all.
    if message.chat.type != ChatType.PRIVATE:
        return

    # ``me()`` is cached by aiogram — no API call per button press.
    bot_username = (await message.bot.me()).username or "bot"

    await message.answer(
        inline_help_text(bot_username),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
