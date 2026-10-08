"""
Emergency command: clear a ``ReplyKeyboardMarkup`` stuck at the bottom of a
group chat.

Telegram clients cache the last reply keyboard **per chat**, and re-adding
the bot does NOT flush that cache — the only thing that clears it is a fresh
message from the bot carrying ``ReplyKeyboardRemove``. This router is the
drop-in answer:

    /fix_keyboard      (alias: /remove_keyboard)

``selective=False`` is the whole point of the exercise. With
``selective=True`` Telegram hides the keyboard only for the users targeted
by the message — @mentioned in the text, or (when the message is a reply)
the sender of the original message — which in a group means "nearly
nobody". ``False`` (the Bot API default, spelled out here so the intent is
unmissable) clears it for **every** member of the chat.

No chat-type, state or permission filter on purpose: the command must work
the moment it is typed, in any chat, in any FSM state. It is registered in
``bot.py`` right after ``navigation_router`` so no state-gated catch-all
below it can swallow the message.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardRemove

router = Router()

_REMOVAL_NOTE = "✅ کیبورد گروه برای همهٔ اعضا حذف شد."


@router.message(Command("fix_keyboard", "remove_keyboard"))
async def cmd_fix_keyboard(message: Message) -> None:
    """Answer in *this* chat with an explicit removal payload.

    The reply lands in the same chat the keyboard is stuck in, so Telegram
    drops the cached keyboard there for everyone — sender and group members
    alike.
    """
    await message.answer(
        _REMOVAL_NOTE,
        reply_markup=ReplyKeyboardRemove(remove_keyboard=True, selective=False),
    )


__all__ = ["router"]
