"""
Universal "get me out of this page" navigation.

A ``ReplyKeyboardMarkup`` *replaces* the previous keyboard, so every wizard
step swaps the main menu for its own buttons. Without a way back, a user who
walks into a sub-page (profile setup, a queued whisper, a pending admin input)
is stuck there: the wizard's own state handler matches *any* text, so even
``/start`` and the main-menu labels get eaten and answered with
"لطفاً یک عدد بین ۱۶ تا ۵۰ وارد کنید".

This router is therefore registered **first** in the dispatcher, ahead of every
other router, and owns exactly three things:

  * the "🏠 منوی اصلی" button on any sub-page,
  * ``/start``, which must always work no matter which state is active,
  * abandoning a parked whisper, so a half-written draft cannot trap a user.

Live sessions (an active chat, an anonymous inbox conversation) are the one
exception: they are ended through their own flow, because leaving them behind
would strand the partner mid-conversation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from handlers.start import cmd_start
from keyboards import (
    BACK_TO_MENU_TEXTS,
    anonymous_chat_menu_kb,
    chat_menu_kb,
    main_menu_kb,
)
from states import AnonChatStates, ChatState

logger = logging.getLogger(__name__)
router = Router()


# ──────────────────────────────────────────────────
# States that must be left through their own button
# ──────────────────────────────────────────────────
async def _blocked_by_live_session(
    message: Message, current: str | None
) -> bool:
    """Refuse the jump while a conversation is live, and show how to end it.

    Returns ``True`` when the message was fully handled here.
    """
    if current == ChatState.in_chat:
        await message.answer(
            "💬 شما در یک <b>چت فعال</b> هستید.\n\n"
            "برای بازگشت به منو ابتدا چت را لغو کنید — این کار به طرف مقابل "
            "هم اطلاع می‌دهد که گفتگو تمام شده است.",
            parse_mode="HTML",
            reply_markup=chat_menu_kb(),
        )
        return True

    if current == AnonChatStates.in_session:
        await message.answer(
            "💬 شما در یک <b>گفتگوی ناشناس فعال</b> هستید.\n\n"
            "برای بازگشت به منو ابتدا گفتگو را پایان دهید.",
            parse_mode="HTML",
            reply_markup=anonymous_chat_menu_kb(),
        )
        return True

    return False


async def _to_main_menu(message: Message, state: FSMContext, note: str) -> None:
    """Reset the FSM and put the main menu back on screen."""
    current = await state.get_state()

    if await _blocked_by_live_session(message, current):
        return

    # Leaving the search queue is harmless and must not leak a stale entry —
    # otherwise the user would still be sitting in ``search_queue`` (and in
    # the ``chat_pairs`` mirror, from which a restart would put them back)
    # and a later match would hand them a partner they already walked away
    # from. ``leave_search_queue`` does both halves.
    if current == ChatState.in_queue:
        from handlers.chat import leave_search_queue

        await leave_search_queue(message.from_user.id)

    await state.clear()
    await state.set_state(ChatState.idle)
    await message.answer(note, reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def nav_start(message: Message, state: FSMContext) -> None:
    """``/start`` always works — even in the middle of a wizard.

    Routed through the real entry point so a banned user still gets the ban
    notice and a brand-new user is still registered.
    """
    if await _blocked_by_live_session(message, await state.get_state()):
        return

    from handlers.chat import leave_search_queue

    await leave_search_queue(message.from_user.id)
    await state.clear()
    await cmd_start(message, state)


@router.message(
    F.text.in_(BACK_TO_MENU_TEXTS),
    F.chat.type == ChatType.PRIVATE,
)
async def nav_back_to_menu(message: Message, state: FSMContext) -> None:
    """The "🏠 منوی اصلی" button — works from every wizard page."""
    await _to_main_menu(message, state, "منوی اصلی:")


@router.message(Command("menu"))
async def nav_menu(message: Message, state: FSMContext) -> None:
    """``/menu`` — the typed twin of the "🏠 منوی اصلی" button.

    Private only: a group has no menu to go back to, so the command is
    answered there with nothing rather than with a guide nobody asked for.
    The group branch still has to exist (this router owns ``/menu``), it just
    ends in silence.
    """
    if message.chat.type != ChatType.PRIVATE:
        return

    if await _blocked_by_live_session(message, await state.get_state()):
        return
    await state.clear()
    await state.set_state(ChatState.idle)
    await message.answer("منوی اصلی:", reply_markup=main_menu_kb())


__all__ = ["router"]
