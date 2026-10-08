"""Phase 4 smoke: force-join middleware + membership fail-open + verify button."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── log capture (warnings from utils.membership) ──────────────────────────
records: list[logging.LogRecord] = []


class _H(logging.Handler):
    def emit(self, r):  # noqa: ANN001
        records.append(r)


_mlog = logging.getLogger("utils.membership")
_mlog.addHandler(_H())
_mlog.setLevel(logging.DEBUG)

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


# ── fake aiogram event classes (isinstance-patched into the module) ───────
class DummyMsg:
    def __init__(self, chat_type="private", text=None, user_id=5):
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = object()
        self.sent: list = []

    async def answer(self, *a, **k):
        self.sent.append((a, k))


class DummyCB(DummyMsg):
    def __init__(self, data, chat_type="private", user_id=5, message=None):
        super().__init__(chat_type=chat_type, text=None, user_id=user_id)
        self.data = data
        self.message = message if message is not None else DummyMsg(chat_type)
        self.answers: list = []

    async def answer(self, *a, **k):
        self.answers.append((a, k))


class DummyIQ:
    def __init__(self, user_id=5):
        self.from_user = SimpleNamespace(id=user_id)
        self.results: list = []

    async def answer(self, *a, **k):
        self.results.append((a, k))


import middleware.force_join as fj  # noqa: E402
from utils.membership import ChatRef  # noqa: E402

fj.Message = DummyMsg
fj.CallbackQuery = DummyCB
fj.InlineQuery = DummyIQ

CHAN = ChatRef(ref=-100111, title="MyChan", username="mychan", source="table")
GATE = {"missing": [CHAN], "raise": False}
ADMIN = {"on": False}


async def fake_missing(bot, user_id, *, force_refresh=False):
    if GATE["raise"]:
        raise RuntimeError("db down")
    return list(GATE["missing"])


async def fake_admin(uid):
    return ADMIN["on"]


fj.missing_required_chats = fake_missing
fj._is_admin = fake_admin

called: list = []


async def handler(event, data):
    called.append(event)


def cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def main():
    global passed, failed

    # 1. private message, gated → intercepted, prompt with join kb delivered
    called.clear()
    m = DummyMsg(text="سلام")
    await fj.ForceJoinMiddleware()(handler, m, {"bot": object()})
    check("gated message does not reach handler", not called)
    check("gated message got prompt", len(m.sent) == 1, m.sent)
    if m.sent:
        args, kw = m.sent[0]
        markup = kw.get("reply_markup")
        check("prompt carries check_membership button",
              markup is not None and "check_membership" in cbs(markup))
        check("prompt has join url button",
              markup is not None and any(
                  getattr(b, "url", None) for row in markup.inline_keyboard for b in row))
        check("prompt lists channel title", "MyChan" in args[0], args[0])

    # 2. gate open → handler runs
    called.clear()
    GATE["missing"] = []
    m = DummyMsg(text="سلام")
    await fj.ForceJoinMiddleware()(handler, m, {"bot": object()})
    check("open gate reaches handler", called == [m])

    # 3. /start bypasses a closed gate
    called.clear()
    GATE["missing"] = [CHAN]
    m = DummyMsg(text="/start")
    await fj.ForceJoinMiddleware()(handler, m, {"bot": object()})
    check("/start bypasses gate", called == [m])

    # 4. whitelisted callback reaches its own gate
    called.clear()
    cb = DummyCB("whisper:read:abc")
    await fj.ForceJoinMiddleware()(handler, cb, {"bot": object()})
    check("whisper:* callback bypasses gate", called == [cb])
    import inspect as _inspect

    src = _inspect.getsource(fj.ForceJoinMiddleware.__call__)
    check("middleware no longer wipes the membership cache",
          "clear_membership_cache" not in src)

    # 5. private callback (non-whitelisted) → toast + real prompt in PM
    called.clear()
    card = DummyMsg(chat_type="private")
    cb = DummyCB("menu:profile", message=card)
    await fj.ForceJoinMiddleware()(handler, cb, {"bot": object()})
    check("private callback does not reach handler", not called)
    check("private callback toasted", len(cb.answers) == 1, cb.answers)
    if cb.answers:
        a, k = cb.answers[0]
        check("toast is not the group alert", "گفتگوی خصوصی" not in a[0], a[0])
        check("toast is not an alert popup", k.get("show_alert") is not True, k)
    check("private callback got prompt message", len(card.sent) == 1, card.sent)
    if card.sent:
        args, kw = card.sent[0]
        markup = kw.get("reply_markup")
        check("PM prompt has verify button",
              markup is not None and "check_membership" in cbs(markup))

    # 6. group callback → group alert only, nothing posted in the chat
    called.clear()
    gcard = DummyMsg(chat_type="group")
    cb = DummyCB("menu:profile", chat_type="group", message=gcard)
    await fj.ForceJoinMiddleware()(handler, cb, {"bot": object()})
    check("group callback does not reach handler", not called)
    check("group callback got alert", len(cb.answers) == 1, cb.answers)
    if cb.answers:
        a, k = cb.answers[0]
        check("group alert is the PM pointer", "گفتگوی خصوصی" in a[0], a[0])
        check("group alert is a popup", k.get("show_alert") is True, k)
    check("nothing posted in the group", not gcard.sent, gcard.sent)

    # 7. group message silently dropped (no answer)
    called.clear()
    gm = DummyMsg(chat_type="group", text="hello")
    await fj.ForceJoinMiddleware()(handler, gm, {"bot": object()})
    check("group message dropped silently", not called and not gm.sent,
          (called, gm.sent))

    # 8. inline query answered with exactly one PM-pointer result
    iq = DummyIQ()
    await fj.ForceJoinMiddleware()(handler, iq, {"bot": object()})
    check("inline query answered once", len(iq.results) == 1, iq.results)
    if iq.results:
        a, k = iq.results[0]
        res = k.get("results") or (a[0] if a else [])
        check("inline has one result", len(res) == 1)
        check("inline is personal/no-cache",
              k.get("is_personal") is True and k.get("cache_time") == 0, k)

    # 9. admin bypasses
    called.clear()
    ADMIN["on"] = True
    m = DummyMsg(text="hello")
    await fj.ForceJoinMiddleware()(handler, m, {"bot": object()})
    check("admin bypasses gate", called == [m])
    ADMIN["on"] = False

    # 10. crash in the gate fails open
    called.clear()
    GATE["raise"] = True
    m = DummyMsg(text="hello")
    await fj.ForceJoinMiddleware()(handler, m, {"bot": object()})
    check("gate crash fails open", called == [m])
    GATE["raise"] = False

    # ── membership.py fail-open semantics ─────────────────────────────────
    import utils.membership as um

    um._cache.clear()
    um._warned_channels.clear()
    records.clear()

    class MB:
        def __init__(self):
            self.calls = 0
            self.status = "member"
            self.broken = False

        async def get_chat_member(self, chat_id, user_id):
            self.calls += 1
            if self.broken:
                raise RuntimeError("network down")
            return SimpleNamespace(status=self.status)

    mb = MB()
    mb.broken = True
    r1 = await um.is_member(mb, -100222, 7, force_refresh=True)
    warns1 = sum(1 for r in records if r.levelno == logging.WARNING)
    r2 = await um.is_member(mb, -100222, 7, force_refresh=True)
    warns2 = sum(1 for r in records if r.levelno == logging.WARNING)
    check("failed lookup fails open", r1 is True and r2 is True, (r1, r2))
    check("warning logged once per channel", warns1 == 1 and warns2 == 1,
          (warns1, warns2))
    check("failed lookup cached as pass", mb.calls == 2, mb.calls)

    um._warned_channels.discard(str(-100222))
    mb.broken = False
    mb.status = "left"
    um._cache.clear()
    r3 = await um.is_member(mb, -100222, 7, force_refresh=True)
    check("real 'left' is not a member", r3 is False, r3)
    check("channel leaves the warn set on success",
          str(-100222) not in um._warned_channels)

    mb.broken = True
    records.clear()
    await um.is_member(mb, -100222, 7, force_refresh=True)
    warns3 = sum(1 for r in records if r.levelno == logging.WARNING)
    check("recovery then failure warns again", warns3 == 1, warns3)

    mb.broken = False
    mb.status = "member"
    um._cache.clear()
    before = mb.calls
    await um.is_member(mb, -100222, 7)
    after_cached = mb.calls
    await um.is_member(mb, -100222, 7)
    check("TTL cache avoids repeat API calls",
          after_cached == before + 1 and mb.calls == after_cached,
          (before, after_cached, mb.calls))

    # ── check_membership (start.py verify button) ─────────────────────────
    import handlers.start as st

    st_missing: list = []
    st_raise = False

    async def fake_st(bot, user_id, *, force_refresh=False):
        if st_raise:
            raise RuntimeError("api down")
        return list(st_missing)

    st.missing_required_chats = fake_st
    state_calls: list = []

    class FakeState:
        async def set_state(self, s):
            state_calls.append(s)

    cb = DummyCB("check_membership")
    st_missing = [CHAN]
    await st.check_membership(cb, FakeState())
    check("verify refuses while missing", len(cb.answers) == 1, cb.answers)
    if cb.answers:
        a, k = cb.answers[0]
        check("refusal names the channel", "MyChan" in a[0], a[0])
        check("refusal is an alert", k.get("show_alert") is True, k)

    cb = DummyCB("check_membership")
    st_missing = []
    await st.check_membership(cb, FakeState())
    check("verify succeeds when joined",
          any("تایید شد" in (a[0] if a else "") for a, _ in cb.answers), cb.answers)
    check("success sets idle state", bool(state_calls))

    cb = DummyCB("check_membership")
    st_raise = True
    state_calls.clear()
    await st.check_membership(cb, FakeState())
    check("verify fails open on crash",
          any("تایید شد" in (a[0] if a else "") for a, _ in cb.answers), cb.answers)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
