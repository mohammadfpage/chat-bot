"""Phase-3 economy smoke: atomic charges, exact refunds, rate-window ordering,
singleton policy, concurrent claims."""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

TMP_DB = r"C:\Users\Amin\AppData\Local\Temp\opencode\phase3_economy.db"
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(TMP_DB + suffix)
    except FileNotFoundError:
        pass

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + TMP_DB
os.environ["LOG_FILE"] = ""
os.environ["LOG_LEVEL"] = "WARNING"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

checks = []
def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

from sqlalchemy import select, update as sa_update

from database import User, async_session_factory, init_db
from database.engine import engine
from utils import economy as eco

U1, U2, U3 = 111, 222, 333

async def set_coins(tg, n):
    async with async_session_factory() as s:
        await s.execute(sa_update(User).where(User.telegram_id == tg).values(coins=n))
        await s.commit()

async def get_coins(tg):
    async with async_session_factory() as s:
        return await s.scalar(select(User.coins).where(User.telegram_id == tg))

async def set_policy(**kw):
    async with async_session_factory() as s:
        await s.execute(sa_update(eco.BotPolicy).values(**kw))
        await s.commit()
    await eco.refresh_policy()

async def main():
    await init_db()
    await eco.get_policy()
    await set_policy(whisper_cost=1, welcome_coins=0,
                     messages_per_minute=5, messages_per_hour=1, vip_multiplier=1,
                     enabled=True, daily_bonus_coins=25, referral_coin_reward=50,
                     referral_premium_days=0, referral_invitee_coins=15)

    # users
    await eco.ensure_user(U1); await eco.ensure_user(U2); await eco.ensure_user(U3)
    await set_coins(U1, 5); await set_coins(U2, 0.5); await set_coins(U3, 5)

    # 1. normal charge
    c = await eco.check_and_deduct_balance(U1)
    check(f"charge returns 1.0 (got {c!r})", c == 1.0)
    check(f"balance 5->4 (got {await get_coins(U1)})", await get_coins(U1) == 4.0)

    # 2. insufficient
    c = await eco.check_and_deduct_balance(U2)
    check(f"insufficient -> None (got {c!r})", c is None)
    check("insufficient leaves balance", await get_coins(U2) == 0.5)

    # 3. exempt user never charged
    async with async_session_factory() as s:
        await s.execute(sa_update(User).where(User.telegram_id == U3).values(is_exempt=True))
        await s.commit()
    c = await eco.check_and_deduct_balance(U3)
    check(f"exempt -> 0.0 free (got {c!r})", c == 0.0)
    check("exempt balance untouched", await get_coins(U3) == 5.0)

    # 4. exact refund after repricing (the old bug refunded the NEW price)
    await set_policy(whisper_cost=7)
    r = await eco.refund_balance(U1, 1.0)
    check(f"refund(1.0) ok (got {r!r})", r is True)
    check(f"balance back to 5 not 4+7 (got {await get_coins(U1)})", await get_coins(U1) == 5.0)
    r = await eco.refund_balance(U3, 0.0)
    check("refund of 0.0 is a no-op", r is False)
    await set_policy(whisper_cost=1)

    # 5. zero price
    await set_policy(whisper_cost=0)
    c = await eco.check_and_deduct_balance(U2)
    check(f"zero price -> 0.0 (got {c!r})", c == 0.0)
    await set_policy(whisper_cost=1)

    # 6. exactly one of two simultaneous charges wins
    await set_coins(U1, 1.0)
    results = await asyncio.gather(
        eco.check_and_deduct_balance(U1),
        eco.check_and_deduct_balance(U1),
    )
    winners = [x for x in results if x is not None]
    check(f"concurrent double-spend: exactly one wins (got {results})",
          len(winners) == 1 and winners[0] == 1.0)
    check(f"balance can't go negative (got {await get_coins(U1)})",
          (await get_coins(U1) or 0) >= -1e-9)

    # 7. rate limiter: an hour-refusal must NOT eat a minute slot
    eco.reset_rate_limits(U2)
    a1 = await eco.authorize_chat_message(U2)          # allowed: both windows +1
    a2 = await eco.authorize_chat_message(U2)          # hour full -> refused
    minutes = eco._min_window._stamps.get(U2, [])
    check(f"1st allowed / 2nd hour-refused (got {a1.allowed},{a2.allowed})",
          a1.allowed and not a2.allowed and a2.reason == "rate")
    check(f"refused msg consumed no minute slot (got {len(minutes)})", len(minutes) == 1)

    # 8. concurrent policy creation does not crash on the PK
    eco._policy_cache = None; eco._policy_cache_at = 0.0
    p1, p2 = await asyncio.gather(eco.get_policy(), eco.get_policy())
    check("concurrent get_policy survives", p1 is not None and p2 is not None)

    # 9. daily claim: double-tap pays once
    async with async_session_factory() as s:
        await s.execute(sa_update(User).where(User.telegram_id == U3)
                        .values(last_daily_bonus=None))
        await s.commit()
    await set_coins(U3, 0)
    r1, r2 = await asyncio.gather(
        eco.claim_daily_bonus(U3), eco.claim_daily_bonus(U3)
    )
    oks = [r for r in (r1, r2) if r[0]]
    check(f"daily double-tap grants once (got {r1},{r2})", len(oks) == 1)
    check(f"daily balance +25 once (got {await get_coins(U3)})", await get_coins(U3) == 25.0)

    # 10. add_coins unknown user -> False, known -> True
    check("add_coins unknown False", await eco.add_coins(999, 5) is False)
    check("add_coins known True", await eco.add_coins(U2, 3) is True)
    check("add_coins credited", await get_coins(U2) == 3.5)

    # 11. match fee atomicity
    await set_coins(U1, 1.0)
    ok, cost = await eco.charge_match_fee(U1, eco.MODE_GIRL)
    check(f"match fee charged (got {ok},{cost})", ok and cost == 1.0)
    ok, cost = await eco.charge_match_fee(U1, eco.MODE_GIRL)
    check(f"match fee refused when broke (got {ok},{cost})", not ok)
    # exempt skips the fee entirely
    ok, cost = await eco.charge_match_fee(U3, eco.MODE_GIRL)
    check(f"exempt match free (got {ok},{cost})", ok and cost == 0)

    # 12. referral reward: coins + counter move together
    await set_coins(U2, 0)
    got = await eco.reward_referral(U2)
    check(f"referral pays 50 (got {got})", got[0] == 50)
    async with async_session_factory() as s:
        row = await s.scalar(select(User).where(User.telegram_id == U2))
    check(f"referral_count==1 coins==50 (got {row.referral_count},{row.coins})",
          row.referral_count == 1 and row.coins == 50)

    await engine.dispose()

asyncio.run(main())
print(f"\n{sum(1 for _, ok in checks if ok)}/{len(checks)} checks passed")
sys.exit(0 if all(ok for _, ok in checks) else 1)
