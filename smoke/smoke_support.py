"""Smoke: the «🎧 پشتیبانی» support-ticket system (user flow, admin inbox,
DB round-trip, templates, and the main-menu wiring)."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP_DIR = Path(tempfile.mkdtemp(prefix="smoke_support_"))
TMP_DB = TMP_DIR / "support.db"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(TMP_DB)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name} {detail}")


# ── 1. templates + status helpers (pure) ─────────────────────────────────
from utils.support import (  # noqa: E402
    STATUS_ANSWERED,
    STATUS_CLOSED,
    STATUS_OPEN,
    status_from_filter,
    status_label,
    support_home_text,
    support_prompt_text,
    support_reply_text,
    support_submitted_text,
)

check("status labels mapped", status_label(STATUS_OPEN) != STATUS_OPEN
      and status_label(STATUS_ANSWERED) != STATUS_ANSWERED
      and status_label(STATUS_CLOSED) != STATUS_CLOSED)
check("status_from_filter maps known + all",
      status_from_filter("open") == STATUS_OPEN
      and status_from_filter("answered") == STATUS_ANSWERED
      and status_from_filter("all") is None
      and status_from_filter("bogus") is None)

home = support_home_text()
check("home card greets", "سلام" in home and "پشتیبانی" in home)
home_open = support_home_text(42)
check("home card echoes the open ticket id", "#42" in home_open)
check("prompt tells the user what to type", "پیام خود را بنویسید" in support_prompt_text())
check("submitted card carries the ticket id", "#7" in support_submitted_text(7))

reply = support_reply_text("خطا برطرف شد <b>")
check("reply greets and thanks", "سلام" in reply and "سپاسگزاریم" in reply)
check("reply keeps the admin text", "خطا برطرف شد" in reply)
check("reply escapes admin HTML", "<b>" not in reply and "&lt;b&gt;" in reply)

# ── 2. user keyboards ────────────────────────────────────────────────────
from keyboards import (  # noqa: E402
    BOT_CONTROL_TEXTS,
    SUPPORT_LABEL,
    main_menu_kb,
    support_history_kb,
    support_menu_kb,
    support_ticket_kb,
)


def cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def reply_labels(kb):
    return [b.text for row in kb.keyboard for b in row]


check("main menu carries the support button", SUPPORT_LABEL in reply_labels(main_menu_kb()))
check("support label is a bot control text", SUPPORT_LABEL in BOT_CONTROL_TEXTS)

m = support_menu_kb()
check("support menu has write + back", {"support:write", "support:back"} <= set(cbs(m)))
check("support menu hides history when empty", "support:history" not in cbs(m))
m_hist = support_menu_kb(has_history=True)
check("support menu shows history when present", "support:history" in cbs(m_hist))

h = support_history_kb([(1, "one"), (2, "two")])
check("history lists each ticket", {"support:view:1", "support:view:2"} <= set(cbs(h)))

t = support_ticket_kb()
check("ticket view offers follow-up + home",
      {"support:write", "support:home", "support:history"} <= set(cbs(t)))

# ── 3. admin keyboards ───────────────────────────────────────────────────
from keyboards import (  # noqa: E402
    admin_panel_kb,
    admin_support_kb,
    admin_support_list_kb,
    admin_support_notify_kb,
    admin_support_ticket_kb,
)

panel = cbs(admin_panel_kb(is_root=False))
check("panel exposes admin:support", "admin:support" in panel)
check("panel is 11 buttons (support added)", len(panel) == 11, panel)

ask = admin_support_kb(open_count=3, answered_count=2, total=5)
check("inbox has three filters",
      {"admin:support:list:open:1", "admin:support:list:answered:1",
       "admin:support:list:all:1"} <= set(cbs(ask)))

lst = admin_support_list_kb([(9, "x")], status="open", page=2, has_next=True)
lc = cbs(lst)
check("list opens a ticket", "admin:support:open:9" in lc)
check("list pager links both ways",
      "admin:support:list:open:1" in lc and "admin:support:list:open:3" in lc)

tk = cbs(admin_support_ticket_kb(5, closed=False))
check("open ticket offers reply + close",
      "admin:support:reply:5" in tk and "admin:support:close:5" in tk)
tk_closed = cbs(admin_support_ticket_kb(5, closed=True))
check("closed ticket offers reopen",
      "admin:support:reply:5" in tk_closed and "admin:support:reopen:5" in tk_closed
      and "admin:support:close:5" not in tk_closed)

check("notify button opens the ticket",
      "admin:support:open:5" in cbs(admin_support_notify_kb(5)))

# ── 4. wiring: menu escape set, router registration ──────────────────────
import handlers.whisper as hw  # noqa: E402
import handlers.support as hs  # noqa: E402
import handlers.admin as ha  # noqa: E402

check("support label escapes a parked whisper", SUPPORT_LABEL in hw._MENU_ESCAPE_TEXTS)

support_msg_handlers = [h.callback for h in hs.router.message.handlers]
check("open_support registered", hs.open_support in support_msg_handlers)
check("submit_support_message registered", hs.submit_support_message in support_msg_handlers)

support_cb_handlers = [h.callback for h in hs.router.callback_query.handlers]
check("user support callbacks registered",
      all(fn in support_cb_handlers for fn in (
          hs.cb_support_home, hs.cb_support_write, hs.cb_support_history,
          hs.cb_support_view, hs.cb_support_back)))

admin_cb_handlers = [h.callback for h in ha.router.callback_query.handlers]
check("admin support callbacks registered",
      all(fn in admin_cb_handlers for fn in (
          ha.cb_support_inbox, ha.cb_support_list, ha.cb_support_open,
          ha.cb_support_reply, ha.cb_support_close, ha.cb_support_reopen)))
admin_msg_handlers = [h.callback for h in ha.router.message.handlers]
check("admin reply handler registered", ha.admin_support_send_reply in admin_msg_handlers)

check("reply delivery reuses the template",
      "support_reply_text" in inspect.getsource(hs.deliver_team_reply))

from handlers import support_router  # noqa: E402

check("support_router exported", support_router is hs.router)

# ── 5. DB round-trip ─────────────────────────────────────────────────────
import bot as bot_mod  # noqa: E402,F401
from database import (  # noqa: E402
    SupportMessage,
    SupportTicket,
    User,
    async_session_factory,
)
from database.engine import init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def db_main():
    await init_db()

    uid = 555001
    async with async_session_factory() as session:
        session.add(User(telegram_id=uid, first_name="کاربر", coins=0))
        session.add(
            SupportTicket(user_id=uid, status=STATUS_OPEN)
        )
        await session.commit()

    # a submitted message: create-or-append picks the open row
    async with async_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.user_id == uid)
        )).scalar_one()
        session.add(SupportMessage(ticket_id=ticket.id, sender_id=uid,
                                   is_admin=False, content="مشکل دارم", is_read=False))
        await session.commit()
        ticket_id = ticket.id
    check("ticket row created", isinstance(ticket_id, int) and ticket_id > 0)

    home_text, _ = await hs._home_view(uid)
    check("home view sees the open ticket", f"#{ticket_id}" in home_text)

    async with async_session_factory() as session:
        ticket = await session.get(SupportTicket, ticket_id)
    transcript = await hs._transcript(ticket)
    check("user transcript shows their message", "مشکل دارم" in transcript)

    # admin opens the ticket: the user's message flips to read
    text, kb, found = await ha._render_admin_ticket(ticket_id)
    check("admin render finds the ticket", found and f"#{ticket_id}" in text)
    async with async_session_factory() as session:
        msg = (await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket_id)
        )).scalar_one()
    check("admin view marks the user's message read", msg.is_read is True)

    # admin reply flips status to answered and unread marker stays clear
    async with async_session_factory() as session:
        session.add(SupportMessage(ticket_id=ticket_id, sender_id=999, is_admin=True,
                                   content="پاسخ", is_read=True))
        ticket = await session.get(SupportTicket, ticket_id)
        ticket.status = STATUS_ANSWERED
        await session.commit()
    check("answered status persisted",
          (await ha._render_admin_ticket(ticket_id, mark_read=False))[0].find("پاسخ داده") >= 0)

    # an answered ticket is NOT a follow-up target — the user's next message is
    # a fresh record until the team replies again (status flips on reply).
    async with async_session_factory() as session:
        answered = await hs._winner_ticket(session, uid)
    check("answered ticket is not a follow-up target", answered is None)

    # reopen it: now the same row is picked up again as the open winner
    async with async_session_factory() as session:
        t = await session.get(SupportTicket, ticket_id)
        t.status = STATUS_OPEN
        await session.commit()
    async with async_session_factory() as session:
        again = await hs._winner_ticket(session, uid)
    check("reopened ticket is the winner again", again is not None and again.id == ticket_id)

    # a closed ticket is never the winner (a new message starts a fresh row)
    async with async_session_factory() as session:
        t = await session.get(SupportTicket, ticket_id)
        t.status = STATUS_CLOSED
        await session.commit()
    async with async_session_factory() as session:
        none_winner = await hs._winner_ticket(session, uid)
    check("closed ticket is not a follow-up target", none_winner is None)


asyncio.run(db_main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
