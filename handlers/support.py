"""
«🎧 پشتیبانی» — the user's side of the support-ticket system.

A user opens the support screen from the main menu (or ``/support``), writes a
message, and the team answers in the same conversation. The user never sees a
ticket number as anything but a follow-up code; the flow is deliberately two
taps — «نوشتن پیام» then typing — because that is all a support form should be.

The admin side lives in ``handlers/admin.py``; both halves read their copy from
:mod:`utils.support` so a status badge or the reply greeting cannot drift.

Live sessions are respected: like the «منوی اصلی» button, support refuses to
open while a chat or an anonymous session is live, because taking over the FSM
state there would strand the partner on the other side.
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import settings
from database import (
    SupportMessage,
    SupportTicket,
    User,
    async_session_factory,
)
from keyboards import (
    SUPPORT_LABEL,
    anonymous_chat_menu_kb,
    chat_menu_kb,
    main_menu_kb,
    support_history_kb,
    support_menu_kb,
    support_ticket_kb,
)
from states import AnonChatStates, ChatState, SupportStates
from utils.support import (
    STATUS_OPEN,
    status_label,
    support_home_text,
    support_prompt_text,
    support_submitted_text,
    support_reply_text,
)

logger = logging.getLogger(__name__)
router = Router(name="support")

#: How many past messages of one ticket the user's transcript shows.
_TRANSCRIPT_LIMIT = 20


# ──────────────────────────────────────────────────
# Data helpers
# ──────────────────────────────────────────────────
async def _home_view(user_id: int) -> tuple[str, object]:
    """The welcome card text plus the ticket that is still awaiting an answer."""
    async with async_session_factory() as session:
        open_id = (
            await session.execute(
                select(SupportTicket.id)
                .where(
                    SupportTicket.user_id == user_id,
                    SupportTicket.status == STATUS_OPEN,
                )
                .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        total = await session.scalar(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.user_id == user_id
            )
        ) or 0
    return support_home_text(open_id), support_menu_kb(has_history=total > 0)


async def _winner_ticket(session, user_id: int) -> SupportTicket | None:
    """The user's open ticket if there is one, else ``None`` — never a closed one.

    A closed ticket stays closed: the next message starts a fresh record, so the
    panel's «بسته‌شده» bucket is a real archive and not a pile of reopened rows.
    """
    return (
        await session.execute(
            select(SupportTicket)
            .where(
                SupportTicket.user_id == user_id,
                SupportTicket.status == STATUS_OPEN,
            )
            .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _user_ticket_rows(user_id: int) -> list[tuple[int, str]]:
    """``[(ticket_id, label), …]`` for the user's follow-up list."""
    async with async_session_factory() as session:
        tickets = (
            await session.execute(
                select(SupportTicket)
                .where(SupportTicket.user_id == user_id)
                .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
                .limit(10)
            )
        ).scalars().all()
    rows: list[tuple[int, str]] = []
    for t in tickets:
        when = t.updated_at.strftime("%Y-%m-%d")
        rows.append((t.id, f"🎟 #{t.id} · {status_label(t.status)} · {when}"))
    return rows


async def _transcript(ticket: SupportTicket) -> str:
    """Render one ticket's messages as a readable, HTML-safe card body."""
    async with async_session_factory() as session:
        messages = (
            await session.execute(
                select(SupportMessage)
                .where(SupportMessage.ticket_id == ticket.id)
                .order_by(SupportMessage.created_at.asc(), SupportMessage.id.asc())
                .limit(_TRANSCRIPT_LIMIT)
            )
        ).scalars().all()

    lines = [
        f"🎟 <b>پیگیری #{ticket.id}</b>",
        f"وضعیت: {status_label(ticket.status)}",
        "",
    ]
    for msg in messages:
        who = "💬 <b>پشتیبانی:</b>" if msg.is_admin else "🗣 <b>شما:</b>"
        lines.append(who)
        lines.append(escape(msg.content))
        lines.append("")
    if not messages:
        lines.append("<i>هنوز پیامی در این پیگیری ثبت نشده است.</i>")
    return "\n".join(lines).strip()


async def _notify_admins_new_message(
    bot, user_id: int, ticket_id: int, content: str
) -> None:
    """Ping every admin with a button that opens the ticket.

    The button carries the same ``admin:support:open:`` callback the panel list
    uses, so the notification and the inbox share one transcript view. Root
    admins and promoted admins both get it — a report that only reaches
    ``admin_ids_list[0]`` is the exact bug the block-report code already fixed.
    """
    recipients = set(settings.admin_ids_list)
    async with async_session_factory() as session:
        promoted = (
            await session.execute(
                select(User.telegram_id).where(User.is_admin.is_(True))
            )
        ).scalars().all()
    recipients.update(int(uid) for uid in promoted)

    preview = content if len(content) <= 300 else content[:300] + "…"
    text = (
        "🎧 <b>پیام جدید پشتیبانی</b>\n\n"
        f"👤 کاربر: <code>{user_id}</code>\n"
        f"🎫 پیگیری: <code>#{ticket_id}</code>\n\n"
        f"{escape(preview)}"
    )
    from keyboards.admin import admin_support_notify_kb

    kb = admin_support_notify_kb(ticket_id)
    for admin_id in recipients:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:  # a wiped/unreachable admin must not abort
            logger.warning("Support ping to admin %s failed: %s", admin_id, exc)


# ──────────────────────────────────────────────────
# Entry — main-menu button and /support
# ──────────────────────────────────────────────────
async def _open_support(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == ChatState.in_chat:
        await message.answer(
            "💬 شما در یک <b>چت فعال</b> هستید.\n\n"
            "برای رفتن به پشتیبانی ابتدا چت را لغو کنید.",
            parse_mode="HTML",
            reply_markup=chat_menu_kb(),
        )
        return
    if current == AnonChatStates.in_session:
        await message.answer(
            "💬 شما در یک <b>گفتگوی ناشناس فعال</b> هستید.\n\n"
            "برای رفتن به پشتیبانی ابتدا گفتگو را پایان دهید.",
            parse_mode="HTML",
            reply_markup=anonymous_chat_menu_kb(),
        )
        return

    # Any other wizard is abandoned, exactly like the «منوی اصلی» button: the
    # support screen is a destination, not a step inside another flow.
    await state.set_state(ChatState.idle)
    text, kb = await _home_view(message.from_user.id)
    await message.answer(
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
    )


@router.message(Command("support"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == SUPPORT_LABEL, F.chat.type == ChatType.PRIVATE)
async def open_support(message: Message, state: FSMContext) -> None:
    """Open «🎧 پشتیبانی» from the main menu or ``/support``."""
    await _open_support(message, state)


# ──────────────────────────────────────────────────
# Callbacks
# ──────────────────────────────────────────────────
async def _edit_or_answer(callback: CallbackQuery, text: str, kb=None) -> None:
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
    except Exception:
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )


@router.callback_query(F.data == "support:home")
async def cb_support_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChatState.idle)
    text, kb = await _home_view(callback.from_user.id)
    await _edit_or_answer(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "support:write")
async def cb_support_write(callback: CallbackQuery, state: FSMContext) -> None:
    """Arm the next text message as a ticket message.

    The prompt is sent with the main-menu keyboard so «🏠 منوی اصلی» — handled
    by ``navigation_router`` — is a real cancel that also clears this state.
    """
    await state.set_state(SupportStates.waiting_for_message)
    await callback.message.answer(
        support_prompt_text(), parse_mode="HTML", reply_markup=main_menu_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "support:history")
async def cb_support_history(callback: CallbackQuery) -> None:
    rows = await _user_ticket_rows(callback.from_user.id)
    if not rows:
        await _edit_or_answer(
            callback,
            "🗂 <b>پیگیری‌های من</b>\n\nهنوز پیگیری‌ای ثبت نکرده‌اید.",
            support_menu_kb(),
        )
        await callback.answer()
        return
    await _edit_or_answer(
        callback,
        "🗂 <b>پیگیری‌های من</b>\n\nبرای دیدن متن گفتگو یکی را انتخاب کنید.",
        support_history_kb(rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("support:view:"))
async def cb_support_view(callback: CallbackQuery) -> None:
    raw = (callback.data or "").rsplit(":", 1)[-1]
    try:
        ticket_id = int(raw)
    except ValueError:
        await callback.answer("تیکت نامعتبر است.", show_alert=True)
        return

    async with async_session_factory() as session:
        ticket = (
            await session.execute(
                select(SupportTicket).where(
                    SupportTicket.id == ticket_id,
                    SupportTicket.user_id == callback.from_user.id,
                )
            )
        ).scalar_one_or_none()
    if ticket is None:
        await callback.answer("تیکت پیدا نشد.", show_alert=True)
        return

    await _edit_or_answer(callback, await _transcript(ticket), support_ticket_kb())
    await callback.answer()


@router.callback_query(F.data == "support:back")
async def cb_support_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Leave the support card and restore the persistent main menu."""
    await state.set_state(ChatState.idle)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("منوی اصلی:", reply_markup=main_menu_kb())
    await callback.answer()


# ──────────────────────────────────────────────────
# The message itself
# ──────────────────────────────────────────────────
@router.message(SupportStates.waiting_for_message, F.chat.type == ChatType.PRIVATE)
async def submit_support_message(message: Message, state: FSMContext) -> None:
    """Store the user's text as a new ticket or a follow-up to the open one."""
    content = (message.text or "").strip()
    if not content:
        await message.answer(
            "لطفاً پیام خود را به‌صورت <b>متن</b> بنویسید.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return

    user_id = message.from_user.id
    async with async_session_factory() as session:
        ticket = await _winner_ticket(session, user_id)
        followup = ticket is not None
        if ticket is None:
            ticket = SupportTicket(user_id=user_id, status=STATUS_OPEN)
            session.add(ticket)
            await session.flush()
        session.add(
            SupportMessage(
                ticket_id=ticket.id,
                sender_id=user_id,
                is_admin=False,
                content=content,
                is_read=False,
            )
        )
        ticket.status = STATUS_OPEN
        ticket.updated_at = func.now()
        ticket_id = ticket.id
        await session.commit()

    await state.set_state(ChatState.idle)
    await message.answer(
        support_submitted_text(ticket_id, followup=followup),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )

    await _notify_admins_new_message(message.bot, user_id, ticket_id, content)


# ──────────────────────────────────────────────────
# The team's answer, delivered to the user (called from the admin panel)
# ──────────────────────────────────────────────────
async def deliver_team_reply(bot, user_id: int, admin_text: str) -> bool:
    """DM one admin answer to the user, menu keyboard attached.

    Shared with the admin panel so the delivery path (and the greeting/thanks
    wrapped around the admin's words) has a single implementation.
    """
    try:
        await bot.send_message(
            user_id,
            support_reply_text(admin_text),
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return True
    except Exception as exc:
        logger.warning("Could not deliver support reply to %s: %s", user_id, exc)
        return False


__all__ = ["router", "deliver_team_reply"]
