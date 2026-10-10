"""Smoke: the «✏️ ویرایش پروفایل» section (field picker under the profile card).

Covers the FSM states, the two new keyboards, handler registration, the
gender-step collision guard, and a DB round-trip through the shared
``_finish_field_edit`` save path.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP_DIR = Path(tempfile.mkdtemp(prefix="smoke_profile_edit_"))
TMP_DB = TMP_DIR / "profile_edit.db"
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


# ── 1. FSM states ────────────────────────────────────────────────────────
from states import ProfileEdit, ProfileSetup  # noqa: E402

for field in ("age", "city", "gender", "height", "photo"):
    check(
        f"ProfileEdit.waiting_for_{field} exists",
        hasattr(ProfileEdit, f"waiting_for_{field}"),
    )
check(
    "ProfileEdit is its own group (not ProfileSetup)",
    ProfileEdit.waiting_for_age.state.startswith("ProfileEdit:"),
)

# ── 2. keyboards ─────────────────────────────────────────────────────────
from keyboards import profile_card_kb, profile_edit_kb  # noqa: E402


def cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


card = profile_card_kb()
check("profile card carries the edit button", "profile:edit" in cbs(card))
check("edit button reads «ویرایش پروفایل»", any("ویرایش پروفایل" in t for t in texts(card)))

edit = profile_edit_kb()
edit_cbs = set(cbs(edit))
check(
    "edit menu offers every field + back",
    {
        "profile:edit:age",
        "profile:edit:city",
        "profile:edit:gender",
        "profile:edit:height",
        "profile:edit:photo",
        "profile:edit:back",
    }
    <= edit_cbs,
    edit_cbs,
)
check("edit menu back row is present", "profile:edit:back" in edit_cbs)

# ── 3. handler registration ──────────────────────────────────────────────
import handlers.profile as hp  # noqa: E402

msg_handlers = [h.callback for h in hp.router.message.handlers]
for fn in (
    hp.edit_age,
    hp.edit_city,
    hp.edit_gender,
    hp.edit_height,
    hp.edit_photo_last_telegram,
    hp.edit_photo_custom,
    hp.edit_photo_none,
    hp.edit_photo_cancel,
    hp.edit_photo_upload,
    hp.edit_photo_invalid,
):
    check(f"{fn.__name__} registered", fn in msg_handlers)

cb_handlers = [h.callback for h in hp.router.callback_query.handlers]
for fn in (
    hp.cb_profile_edit,
    hp.cb_profile_edit_back,
    hp.cb_edit_age,
    hp.cb_edit_city,
    hp.cb_edit_gender,
    hp.cb_edit_height,
    hp.cb_edit_photo,
):
    check(f"{fn.__name__} registered", fn in cb_handlers)

# ── 4. static guarantees ─────────────────────────────────────────────────
psrc = inspect.getsource(hp)

check(
    "standalone gender handler also excludes ProfileEdit",
    "~StateFilter(ProfileSetup, ProfileEdit)" in psrc,
)
check("show_profile renders through the shared card helper",
      "_send_profile_card(message, user)" in psrc)
check("every field edit re-draws the card",
      psrc.count("_send_profile_card") >= 3)
check("city length guard present in edit", "حداکثر ۱۲۸ کاراکتر" in psrc)
check("height length guard present in edit", "حداکثر ۳۲ کاراکتر" in psrc)


# no Telegram call inside an open session in the new code
def session_hits():
    import re

    NET = re.compile(
        r"await\s+[\w.]+\.(answer|reply_|send_|edit_|get_chat|get_user_profile|"
        r"delete_message|copy_message|forward_)"
    )
    SESS = "async with async_session_factory() as session:"
    hits = []
    lines = psrc.splitlines()
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
            hits.append(f"profile.py:{i}")
    return hits


check("no Telegram call inside a session (profile.py)", not session_hits(),
      session_hits())

# ── 5. DB round-trip through the shared save path ────────────────────────
import bot as bot_mod  # noqa: E402,F401
from database import User, async_session_factory  # noqa: E402
from database.engine import init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402


class _FakeBot:
    async def get_user_profile_photos(self, uid, limit=1):
        return None


class _FakeMessage:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.bot = _FakeBot()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))

    async def answer_photo(self, **kw):
        self.answers.append(("PHOTO", kw))


class _FakeState:
    def __init__(self):
        self._state = None

    async def set_state(self, s):
        self._state = s

    async def get_state(self):
        return self._state


async def db_main():
    await init_db()
    uid = 777001

    async with async_session_factory() as session:
        session.add(
            User(
                telegram_id=uid,
                first_name="کاربر",
                age=20,
                city="تهران",
                gender="male",
                height="180",
                is_profile_complete=True,
            )
        )
        await session.commit()

    # edit age
    msg = _FakeMessage(uid)
    st = _FakeState()
    await hp._finish_field_edit(msg, st, age=33)
    async with async_session_factory() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == uid)
        )).scalar_one()
    check("edited age persisted", user.age == 33)
    check("other fields untouched by the age edit",
          user.city == "تهران" and user.gender == "male" and user.height == "180")
    check("edit returns to idle", st._state is not None)
    check("edit confirms to the user",
          any("ذخیره شد" in a[0] for a in msg.answers if isinstance(a[0], str)))
    check("edit re-draws the card",
          any("پروفایل کاربر" in a[0] for a in msg.answers if isinstance(a[0], str)))

    # edit photo settings
    msg2 = _FakeMessage(uid)
    await hp._finish_field_edit(msg2, _FakeState(), show_profile_photo=True,
                                profile_photo="FILEID")
    async with async_session_factory() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == uid)
        )).scalar_one()
    check("photo opt-in persisted",
          user.show_profile_photo is True and user.profile_photo == "FILEID")

    # cancel restores the menu and does not write
    msg3 = _FakeMessage(uid)
    st3 = _FakeState()
    await hp._cancel_field_edit(msg3, st3)
    check("cancel confirms", any("لغو شد" in a[0] for a in msg3.answers
                                 if isinstance(a[0], str)))
    check("cancel returns to idle", st3._state is not None)


asyncio.run(db_main())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
