"""Phase 5 smoke: handler-bug fixes (registration, escape set, session holds,
callback.message None handling, ban notice, report fan-out, toggle commands)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sys
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: F401,E402  (full import graph)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f" FAIL {name} {detail}")


# ── 1. anon router registration (the displaced-decorator regression) ──────
import handlers.anonymous as ha  # noqa: E402

registered = [h.callback for h in ha.router.message.handlers]
check("anon_message_router IS registered", ha.anon_message_router in registered)
check("_rate_gate is NOT registered (helper only)", ha._rate_gate not in registered)

# ── 2. parked-whisper escape set vs the live menu ────────────────────────
import handlers.whisper as hw  # noqa: E402
from keyboards import main_menu_kb  # noqa: E402

labels = [b.text for row in main_menu_kb().keyboard for b in row]
missing = [l for l in labels if l not in hw._MENU_ESCAPE_TEXTS]
check("every menu label escapes a parked whisper", not missing, missing)

# ── 2b. live sessions refuse to relay bot buttons / commands ──────────────
from keyboards import (  # noqa: E402
    BOT_CONTROL_TEXTS,
    is_bot_control_text,
)

check("every main-menu label is a control text", all(l in BOT_CONTROL_TEXTS for l in labels))
check("a slash command is a control text", is_bot_control_text("/start"))
check("a deep-linked command is a control text", is_bot_control_text("/start@bot x"))
check("ordinary text is NOT a control text", not is_bot_control_text("سلام، چطوری؟"))
check("empty text is NOT a control text", not is_bot_control_text(None))

check(
    "anon_message_router refuses control texts",
    "is_bot_control_text" in inspect.getsource(ha.anon_message_router),
)

import handlers.anon_chat as hc  # noqa: E402

check(
    "relay_to_partner refuses control texts",
    "is_bot_control_text" in inspect.getsource(hc.relay_to_partner),
)

# ── 3. _card / _fallback handle deleted & inaccessible cards ──────────────
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    User,
)

u = User(id=5, is_bot=False, first_name="Test")
chat = Chat(id=5, type="private")
msg = Message(message_id=1, date=datetime.now(), chat=chat, from_user=u, text="hi")
acc = InaccessibleMessage(message_id=2, chat=chat, date=0)


def _cb(message):
    return CallbackQuery(
        id="1", from_user=u, chat_instance="c", data="x", message=message
    )


check("_card keeps a live Message", ha._card(_cb(msg)) is msg)
check("_card rejects None", ha._card(_cb(None)) is None)
check("_card rejects InaccessibleMessage", ha._card(_cb(acc)) is None)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


# ── 4. cb_inbox_back with a deleted card falls back to the PM ────────────
async def test_back():
    fb = FakeBot()
    cb = SimpleNamespace(
        data="inbox:back",
        from_user=u,
        message=None,
        bot=fb,
        answer=_noop,
    )
    await ha.cb_inbox_back(cb)
    check("inbox:back on dead card writes to PM",
          len(fb.sent) == 1 and fb.sent[0][0] == u.id, fb.sent)


async def _noop(*a, **k):
    return None


# ── 5. cb_anon_block: refusal leaves the session, answers, never crashes ──
async def test_block():
    answers = []

    async def answer(*a, **k):
        answers.append((a, k))

    cb = SimpleNamespace(
        data="anon_block:999999999:42",
        from_user=u,
        message=None,
        bot=FakeBot(),
        answer=answer,
    )
    await ha.cb_anon_block(cb)
    check("block refusal answers without a card",
          len(answers) == 1 and "یافت نشد" in answers[0][0][0], answers)


# ── 6. ban notice sent exactly once ──────────────────────────────────────
import middleware.force_join as fj  # noqa: E402


async def test_ban_notice():
    fj._ban_notified.clear()
    fb = FakeBot()

    async def is_admin(uid):
        return False

    async def is_banned(uid):
        return True

    fj._is_admin = is_admin
    fj._is_banned = is_banned

    class DummyMsg:
        def __init__(self):
            self.from_user = u
            self.chat = SimpleNamespace(type="private")
            self.text = "hi"
            self.sent = []

        async def answer(self, *a, **k):
            self.sent.append((a, k))

    async def handler(event, data):
        raise AssertionError("banned user must not reach handlers")

    mw = fj.BlockBannedMiddleware()
    await mw(handler, DummyMsg(), {"bot": fb})
    await mw(handler, DummyMsg(), {"bot": fb})
    check("banned user notified exactly once", len(fb.sent) == 1, fb.sent)
    check("banned user never reaches a handler", True)
    fj.forget_ban_notice(u.id)
    check("forget_ban_notice clears the marker", u.id not in fj._ban_notified)


# ── 7. static guarantees ─────────────────────────────────────────────────
root = Path(__file__).resolve().parents[1]


def src(rel):
    return (root / rel).read_text(encoding="utf-8")


wsrc = src(r"handlers\whisper.py")
check("no nested-<b> callback alerts remain", "<b>{title}</b>" not in wsrc)


def callback_answers_containing_markup(text):
    """Return callback.answer( call spans that embed HTML (never rendered)."""
    bad = []
    for m in re.finditer(r"callback\.answer\(", text):
        i = m.end()
        depth = 1
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if "<" in text[m.start():i]:
            bad.append(text[m.start():i][:80])
    return bad


bad_alerts = callback_answers_containing_markup(wsrc)
check("whisper join alerts are plain text", not bad_alerts, bad_alerts)

# ── HTML-escape + dead-kwarg guards (pass-2 fixes) ───────────────────────
check("stranger-chat relay disables parse mode",
      "message.text, parse_mode=None" in src(r"handlers\chat.py"))

asrc = src(r"handlers\anonymous.py")
check("anonymous inbox escapes stored content",
      "escape(msg.content)" in asrc)
check("anonymous realtime relay escapes content",
      "escape(content)" in asrc)

check("whisper media caption escapes user text",
      "caption=escape(content) if content else None" in wsrc)
check("whisper voice-text escapes user text",
      "user_id, escape(content), parse_mode=" in wsrc)

for rel in (r"handlers\anonymous.py", r"handlers\anon_chat.py"):
    s = src(rel)
    dead = [
        m.group(0).replace("\n", " ")[:90]
        for m in re.finditer(r"(?:callback|card)\.answer\([^)]*reply_markup[^)]*\)", s, re.S)
    ]
    check(f"no dead reply_markup on answerCallbackQuery in {rel}", not dead, dead)

csrc = src(r"handlers\chat.py")
code_lines = [ln for ln in csrc.splitlines() if "admin_ids_list" in ln and not ln.lstrip().startswith("#")]
check("block report loops over every admin",
      "for admin_id in settings.admin_ids_list" in csrc)
check("no first-admin-only shortcut left",
      not any("admin_ids_list[0]" in ln for ln in code_lines), code_lines)

import handlers.admin as hd  # noqa: E402

toggle_src = inspect.getsource(hd.cb_whisper_toggle)
check("whisper toggle re-registers commands", "register_commands" in toggle_src)

check("escape set has the three connect labels",
      {"🔀 اتصال شانسی", "👩 چت با دختر", "👨 چت با پسر"} <= hw._MENU_ESCAPE_TEXTS)

# ── FSM coverage: every state except ChatState.idle must have a handler ───
import states as states_mod  # noqa: E402

fsm_src = src(r"states\fsm.py")
check("dead AnonymousStates class removed", "AnonymousStates" not in fsm_src)
check("broadcast preview escapes admin text", "escape(preview)" in src(r"handlers\admin.py"))

state_keys = []
import re as _re

for gm in _re.finditer(r"class (\w+)\(StatesGroup\):", fsm_src):
    seg = fsm_src[gm.end():].split("class ")[0]
    for sm in _re.finditer(r"^\s{4}(\w+) = State\(\)", seg, _re.M):
        state_keys.append(f"{gm.group(1)}.{sm.group(1)}")

decorators = []
for path in (root / "handlers").glob("*.py"):
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("@") and ".message(" in lines[i]:
            dec = lines[i]
            while dec.count("(") > dec.count(")"):
                i += 1
                if i >= len(lines):
                    break
                dec += " " + lines[i].strip()
            decorators.append(dec)
        i += 1

# confirm_send states are button-driven: their confirm AND cancel callbacks
# both call state.clear() (admin:broadcast:confirm|cancel, admin:gift:confirm,
# admin:cancel_input), so typing text there is ignored by design, not stuck.
ALLOW_NO_HANDLER = {"ChatState.idle", "AdminBroadcast.confirm_send",
                    "AdminGift.confirm_send"}
missing = [k for k in state_keys if k not in ALLOW_NO_HANDLER
           and not any(k in d for d in decorators)]
check("every FSM state has a message handler", not missing, missing)


# ── 8. no Bot API call while a DB session is open ────────────────────────
def session_scan_hits():
    NET = re.compile(
        r"await\s+[\w.]+\.(answer|reply_|send_|edit_|get_chat|get_user_profile|"
        r"delete_message|copy_message|forward_)"
        r"|await\s+(_send_dm|_notify|_edit_or_send|_send_ok|_confirm_to_sender)\("
    )
    SESS = "async with async_session_factory() as session:"
    hits = []
    files = list((root / "handlers").glob("*.py")) + [root / "utils" / "economy.py"]
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        open_at = None
        for i, ln in enumerate(lines, 1):
            s = ln.strip()
            if s == SESS:
                open_at = (i, len(ln) - len(ln.lstrip()))
                continue
            if open_at is None:
                continue
            indent = len(ln) - len(ln.lstrip())
            if s and indent <= open_at[1] and not s.startswith("#"):
                open_at = None
                continue
            if NET.search(ln):
                hits.append(f"{path.name}:{i}")
    return hits


hits = session_scan_hits()
check("no Telegram call inside an open session", not hits, hits)


async def main():
    await test_back()
    await test_block()
    await test_ban_notice()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
