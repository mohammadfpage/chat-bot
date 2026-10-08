"""Smoke test for the bulk-gift feature (one / all / random recipients).

Covers: keyboard shapes, FSM states, handler registration ORDER (the
admin:gift:confirm vs admin:gift: prefix race), prompt/confirm texts,
and the real bulk-grant DB logic against a throwaway SQLite file.
"""
import asyncio
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(name)
        print(f" FAIL {name} {extra}")


def balanced(html):
    for tag in ("b", "code", "i", "u"):
        if html.count(f"<{tag}>") != html.count(f"</{tag}>"):
            return False
    # attribute-carrying openings like <a href=...> pair with </a>
    return html.count("<a ") == html.count("</a>")


# ── keyboards ────────────────────────────────────────────────────────────
from keyboards.admin import (
    admin_gift_kb,
    admin_gift_scope_kb,
    admin_gift_confirm_kb,
    admin_panel_kb,
)
import keyboards.admin as kba
import keyboards as kbs

kb = admin_gift_scope_kb("coins", 42)
rows = [[b.text for b in row] for row in kb.inline_keyboard]
cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
check("scope kb: 4 single-button rows", len(rows) == 4 and all(len(r) == 1 for r in rows))
check(
    "scope kb: coins callbacks",
    cbs == [
        "admin:gift:coins:one",
        "admin:gift:coins:all",
        "admin:gift:coins:random",
        "admin:gift",
    ],
    cbs,
)
check("scope kb: live user count on the ALL button", "42 نفر" in rows[1][0])
check("scope kb: RANDOM button labels dice", "🎲" in rows[2][0])

kb2 = admin_gift_scope_kb("premium", 7)
cbs2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
check(
    "scope kb: premium callbacks",
    cbs2 == [
        "admin:gift:premium:one",
        "admin:gift:premium:all",
        "admin:gift:premium:random",
        "admin:gift",
    ],
    cbs2,
)
check("scope kb: premium count", "7 نفر" in kb2.inline_keyboard[1][0].text)

kc = admin_gift_confirm_kb()
ccbs = [b.callback_data for row in kc.inline_keyboard for b in row]
check("confirm kb: confirm + cancel", ccbs == ["admin:gift:confirm", "admin:cancel_input"], ccbs)

gk = admin_gift_kb()
gcbs = [b.callback_data for row in gk.inline_keyboard for b in row]
check("type kb unchanged", gcbs == ["admin:gift:coins", "admin:gift:premium", "admin:panel"] or gcbs[:2] == ["admin:gift:coins", "admin:gift:premium"], gcbs)

pk = admin_panel_kb(is_root=False)
pcbs = [b.callback_data for row in pk.inline_keyboard for b in row]
check("panel still has admin:gift", "admin:gift" in pcbs)

check("scope kb exported (admin module)", "admin_gift_scope_kb" in getattr(kba, "__all__", []))
check("confirm kb exported (admin module)", "admin_gift_confirm_kb" in getattr(kba, "__all__", []))
check("scope kb exported (package)", hasattr(kbs, "admin_gift_scope_kb"))
check("confirm kb exported (package)", hasattr(kbs, "admin_gift_confirm_kb"))

# ── FSM states ───────────────────────────────────────────────────────────
from states.fsm import AdminGift

check("FSM waiting_for_count exists", hasattr(AdminGift, "waiting_for_count"))
check("FSM confirm_send exists", hasattr(AdminGift, "confirm_send"))
check(
    "FSM state names",
    AdminGift.waiting_for_count.state == "AdminGift:waiting_for_count"
    and AdminGift.confirm_send.state == "AdminGift:confirm_send",
)

# ── handler registration order + filter resolution (the race) ───────────
from handlers.admin import router as admin_router

cb_pairs = [(h.callback.__name__, h) for h in admin_router.callback_query.handlers]
names = [n for n, _ in cb_pairs]
for expected in ("cb_gift_menu", "cb_gift_type", "cb_gift_confirm", "cb_gift_scope"):
    check(f"registered: {expected}", expected in names)
check(
    "ORDER: confirm before generic prefix handler",
    names.index("cb_gift_confirm") < names.index("cb_gift_scope"),
    f"confirm@{names.index('cb_gift_confirm')} scope@{names.index('cb_gift_scope')}",
)
check(
    "ORDER: menu/type before generic prefix handler",
    names.index("cb_gift_type") < names.index("cb_gift_scope"),
)


def first_match(data):
    """First registered callback handler whose magic filter accepts `data`."""
    for name, h in cb_pairs:
        for f in h.filters:
            m = getattr(f, "magic", None)
            if m is None:
                continue
            try:
                if m.resolve(SimpleNamespace(data=data)):
                    return name
            except Exception:
                pass
    return None


check("admin:gift         -> cb_gift_menu", first_match("admin:gift") == "cb_gift_menu", first_match("admin:gift"))
check("admin:gift:coins   -> cb_gift_type", first_match("admin:gift:coins") == "cb_gift_type", first_match("admin:gift:coins"))
check("admin:gift:premium -> cb_gift_type", first_match("admin:gift:premium") == "cb_gift_type", first_match("admin:gift:premium"))
check(
    "admin:gift:confirm  -> cb_gift_confirm (NOT scope)",
    first_match("admin:gift:confirm") == "cb_gift_confirm",
    first_match("admin:gift:confirm"),
)
check("admin:gift:coins:one     -> cb_gift_scope", first_match("admin:gift:coins:one") == "cb_gift_scope", first_match("admin:gift:coins:one"))
check("admin:gift:premium:all   -> cb_gift_scope", first_match("admin:gift:premium:all") == "cb_gift_scope")
check("admin:gift:premium:random-> cb_gift_scope", first_match("admin:gift:premium:random") == "cb_gift_scope")

msg_names = [h.callback.__name__ for h in admin_router.message.handlers]
for expected in ("msg_gift_user", "msg_gift_count", "msg_gift_amount"):
    check(f"registered: {expected}", expected in msg_names)

# ── prompt / confirm texts ───────────────────────────────────────────────
from handlers.admin import (
    _gift_amount_prompt,
    _gift_confirm_text,
    _GIFT_SCOPES,
    _notify_gifted,
    _spawn_gift_notify,
    _GIFT_NOTIFY_TASKS,
    bulk_add_coins,
    bulk_add_premium_days,
)
from utils.economy import fmt_coins

check("scopes = one/all/random", set(_GIFT_SCOPES) == {"one", "all", "random"})

p_one = _gift_amount_prompt({"gift_type": "coins", "target_scope": "one"})
p_all = _gift_amount_prompt({"gift_type": "coins", "target_scope": "all"})
p_rnd = _gift_amount_prompt(
    {"gift_type": "premium", "target_scope": "random", "recipient_count": 10}
)
check("prompt(one) has no audience line", "هر کاربر" not in p_one and "تصادفی" not in p_one)
check("prompt(all) names every active user", "هر کاربر فعال" in p_all, p_all)
check("prompt(random) names the count", "10 کاربر تصادفی" in p_rnd, p_rnd)
check("prompt(random) keeps integer hint", "عدد صحیح)" in p_rnd and "اعشاری" not in p_rnd)
check("prompt texts HTML-balanced", all(balanced(t) for t in (p_one, p_all, p_rnd)))

c_rnd = asyncio.run(
    _gift_confirm_text(
        {"gift_type": "coins", "amount": 2.5, "target_scope": "random", "recipient_count": 10}
    )
)
c_all = asyncio.run(
    _gift_confirm_text({"gift_type": "premium", "amount": 3, "target_scope": "all"})
)
check("confirm(random) shows total", "مجموع هدیه" in c_rnd and fmt_coins(25.0) in c_rnd, c_rnd)
check(
    "confirm(random) shows audience",
    "<b>10</b> کاربر تصادفی" in c_rnd,
    c_rnd,
)
check("confirm(all) counts live users", "همهٔ کاربران فعال" in c_all, c_all)
check("confirm(all) premium total", "روز اشتراک" in c_all and "مجموع هدیه" in c_all)
check("confirm texts HTML-balanced", balanced(c_rnd) and balanced(c_all))

check("notify task set exists", isinstance(_GIFT_NOTIFY_TASKS, set))
check("bulk grants imported into admin", callable(bulk_add_coins) and callable(bulk_add_premium_days))

# ── bulk-grant DB logic on a throwaway database ─────────────────────────
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from database.models import Base, User, CoinTransaction
import utils.economy as eco


async def bulk_test():
    path = os.path.join(tempfile.gettempdir(), "smoke_gift.db")
    if os.path.exists(path):
        os.remove(path)
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    old = eco.async_session_factory
    eco.async_session_factory = factory
    try:
        async with factory() as s:
            s.add_all(
                [
                    User(telegram_id=101, coins=1.5),
                    User(telegram_id=102, coins=2),
                    User(telegram_id=103, coins=0, is_banned=True),
                ]
            )
            await s.commit()

        # all non-banned users
        granted = await eco.bulk_add_coins(2.5)
        check("bulk all: granted ids", sorted(granted) == [101, 102], granted)

        async with factory() as s:
            rows = {
                u.telegram_id: u.coins
                for u in (await s.execute(select(User))).scalars()
            }
            ledger = (
                (await s.execute(select(CoinTransaction))).scalars().all()
            )
        check(
            "bulk all: balances credited",
            rows.get(101) == 4.0 and rows.get(102) == 4.5,
            rows,
        )
        check("bulk all: banned untouched", rows.get(103) == 0, rows.get(103))
        check(
            "bulk all: one ledger row per recipient",
            len(ledger) == 2
            and all(l.reason == "gift" and l.amount == 2.5 for l in ledger),
            [(l.user_id, l.amount, l.reason) for l in ledger],
        )

        # explicit id list (the random path)
        granted2 = await eco.bulk_add_coins(1, user_ids=[102])
        check("bulk ids: only that user", granted2 == [102], granted2)
        async with factory() as s:
            u101 = (
                await s.execute(select(User).where(User.telegram_id == 101))
            ).scalar_one()
            u102 = (
                await s.execute(select(User).where(User.telegram_id == 102))
            ).scalar_one()
        check("bulk ids: others untouched", u101.coins == 4.0 and u102.coins == 5.5, (u101.coins, u102.coins))

        # premium bulk
        gp = await eco.bulk_add_premium_days(7)
        check("bulk premium: granted ids", sorted(gp) == [101, 102], gp)
        async with factory() as s:
            banned = (
                await s.execute(select(User).where(User.telegram_id == 103))
            ).scalar_one()
            u101 = (
                await s.execute(select(User).where(User.telegram_id == 101))
            ).scalar_one()
        check(
            "bulk premium: active extended, banned not",
            u101.premium_until is not None and banned.premium_until is None,
            (u101.premium_until, banned.premium_until),
        )

        # guards
        check("guard: zero coins -> []", await eco.bulk_add_coins(0) == [])
        check("guard: negative coins -> []", await eco.bulk_add_coins(-5) == [])
        check("guard: zero premium -> []", await eco.bulk_add_premium_days(0) == [])
        check("guard: unknown ids -> []", await eco.bulk_add_coins(1, user_ids=[999]) == [])

        # extending a live subscription must not shorten it
        before = u101.premium_until
        gp2 = await eco.bulk_add_premium_days(1, user_ids=[101])
        async with factory() as s:
            u101b = (
                await s.execute(select(User).where(User.telegram_id == 101))
            ).scalar_one()
        check(
            "bulk premium: extension never shortens",
            gp2 == [101] and u101b.premium_until is not None and before is not None
            and u101b.premium_until >= before,
            (before, u101b.premium_until),
        )
    finally:
        eco.async_session_factory = old
        await eng.dispose()
        if os.path.exists(path):
            os.remove(path)


asyncio.run(bulk_test())

# ── wiring ───────────────────────────────────────────────────────────────
import bot  # noqa: F401

check("import bot", True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL GIFT SMOKE CHECKS PASSED")
