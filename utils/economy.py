"""
Economy & anti-spam engine: coins, referrals, premium, rate limits.

All values flow from the single ``bot_policy`` table via ``get_policy()``
(with a short in-memory cache) so the admin can tune them live from the
admin panel.

Coins (سکه) are the ONLY currency. The old second unit — "tokens", charged per
message sent — has been removed from the database and from this module.

What costs money, precisely:

    «اتصال شانسی»      ``BotPolicy.random_chat_cost`` coins (0 = free, the default)
    «چت با دختر»      ``BotPolicy.chat_girl_cost`` coins, once per MATCH
    «چت با پسر»       ``BotPolicy.chat_boy_cost``  coins, once per MATCH
    a group whisper    ``BotPolicy.whisper_cost`` (see :func:`check_and_deduct_balance`)

Every price above is read from ``bot_policy`` at the moment it is charged, so
"free or paid, and how much" is an admin-panel edit — never a redeploy.

Talking is free. :func:`authorize_chat_message` rate-limits and nothing else —
it never touches the balance. The charge for a conversation is taken once, by
:func:`charge_match_fee`, at the moment the two users actually get connected.

Rate limiting uses in-memory sliding windows (per minute / per hour). It is
applied at the chat-send points only, so menu navigation and profile setup
never trigger it, and it never costs coins — too fast means the message is
dropped with a warning, nothing more.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from database import (
    CoinTransaction,
    async_session_factory,
    BotPolicy,
    User,
)
from utils.emojis import get_plain_emoji

POLICY_CACHE_TTL = 5.0

_policy_cache: BotPolicy | None = None
_policy_cache_at: float = 0.0


# ──────────────────────────────────────────────────
# Policy access & cache
# ──────────────────────────────────────────────────

async def _load_policy(session) -> BotPolicy:
    """Read (or create) the singleton row on ``session``.

    ``order_by(id)`` matters: if an older bug or a manual copy ever left TWO
    rows behind, "whichever row SQLite returns first" silently decides every
    price in the bot — the lowest id is at least deterministic. The insert is
    guarded against the concurrent-first-run race: both racers SELECT, both
    see nothing, the loser's INSERT violates the primary key and is retried
    as a read instead of crashing the handler that touched policy first.
    """
    result = await session.execute(select(BotPolicy).order_by(BotPolicy.id))
    policy = result.scalars().first()
    if policy is not None:
        return policy

    policy = BotPolicy()
    session.add(policy)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(BotPolicy).order_by(BotPolicy.id)
        )
        policy = result.scalars().first()
        if policy is None:  # pragma: no cover - PK race is the only known cause
            policy = BotPolicy()
            session.add(policy)
            await session.commit()
    return policy


async def get_policy() -> BotPolicy:
    """Return the (cached) bot policy row; lazily create it if missing."""
    global _policy_cache, _policy_cache_at
    now = time.monotonic()
    if _policy_cache is not None and (now - _policy_cache_at) < POLICY_CACHE_TTL:
        return _policy_cache

    async with async_session_factory() as session:
        policy = await _load_policy(session)

    _policy_cache = policy
    _policy_cache_at = time.monotonic()
    return policy


async def refresh_policy() -> BotPolicy:
    """Force reload the policy row (call right after an admin edit)."""
    global _policy_cache, _policy_cache_at
    async with async_session_factory() as session:
        policy = await _load_policy(session)
    _policy_cache = policy
    _policy_cache_at = time.monotonic()
    return policy


def peek_policy() -> BotPolicy:
    """The cached policy row, without awaiting anything.

    For synchronous TEXT builders (help cards, rejection strings, menu lines)
    that need a price but cannot run a query. Falls back to a fresh
    ``BotPolicy()`` — the schema defaults — when the cache is still cold, which
    is a safe answer: those defaults are exactly what the database holds before
    an admin has ever edited anything.
    """
    return _policy_cache if _policy_cache is not None else BotPolicy()


# ──────────────────────────────────────────────────
# Money: rounding, formatting, parsing
# ──────────────────────────────────────────────────
#
# Costs are allowed to be fractional (0.5, 2.5), so every balance write, every
# price read and every comparison goes through these three helpers. Without a
# single rounding point, ``0.1 + 0.2`` drifts and a wallet would show
# ``0.30000000000000004`` after a few charges.

#: Two decimals is all the economy ever stores; it is also the finest price an
#: admin can set, so "what was charged" and "what was displayed" cannot differ.
COIN_DECIMALS = 2

#: Sanity ceiling for any single number the admin types — a typo of ``50000``
#: must not become a permanent, unnoticed 50000-coin price.
MAX_POLICY_VALUE = 1_000_000

#: Persian (۰-۹) and Arabic-Indic (٠-٩) digits plus the Persian separators,
#: mapped onto what ``float()`` understands. Admins type on a Persian keyboard.
_NUMERAL_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬",
    "01234567890123456789.,",
)

#: ``1,000`` / ``12,345`` — a comma only ever groups thousands when it is
#: followed by exactly three digits all the way to the end.
_THOUSANDS = re.compile(r"\d{1,3}(,\d{3})+")


def round_coins(value: float | int | None) -> float:
    """The one place a coin amount becomes canonical: rounded to 2 decimals."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n):
        return 0.0
    return round(n, COIN_DECIMALS)


def fmt_coins(value: float | int | None) -> str:
    """Display form of an amount: ``10``, ``2.5``, ``0.25`` — never ``2.5000000000000004``."""
    n = round_coins(value)
    if n == int(n):
        return str(int(n))
    return f"{n:.{COIN_DECIMALS}f}".rstrip("0").rstrip(".")


def parse_amount(raw: str) -> float | None:
    """Parse a non-negative money amount an admin typed. ``None`` = rejected.

    Accepts Persian/Arabic numerals, ``.`` or ``٫`` as the decimal point, and
    ``1,000``-style grouping. Anything negative, non-finite, above
    :data:`MAX_POLICY_VALUE` or carrying more than two decimals is refused —
    a fractional price the system would silently round away is worse than a
    clear "that is not a number".
    """
    text = (raw or "").strip().translate(_NUMERAL_MAP)
    if not text:
        return None
    text = text.replace(" ", "")
    if _THOUSANDS.fullmatch(text):
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")

    if not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or value > MAX_POLICY_VALUE:
        return None
    if round(value, COIN_DECIMALS) != value:
        return None
    return round(value, COIN_DECIMALS)


def parse_int(raw: str) -> int | None:
    """Same rules as :func:`parse_amount`, but the value must be a whole number.

    Rewards, rate limits and chat rules stay integers — only the price of a
    service may be fractional.
    """
    value = parse_amount(raw)
    if value is None or value != int(value):
        return None
    return int(value)


#: Ledger reason → its human name in reports and the user's own history.
#: Translated here rather than stored, so renaming a feature does not
#: rewrite the past.
REASON_LABELS: dict[str, str] = {
    "welcome": "سکهٔ خوش‌آمد",
    "daily": "جایزهٔ روزانه",
    "referral": "پاداش رفرال",
    "invitee": "هدیهٔ دعوت‌شده",
    "gift": "هدیهٔ ادمین",
    "whisper": "هزینهٔ نجوا",
    "match_girl": "هزینهٔ چت با دختر",
    "match_boy": "هزینهٔ چت با پسر",
    "match_random": "هزینهٔ اتصال شانسی",
    "refund_whisper": "بازگشت هزینهٔ نجوا",
    "refund_match": "بازگشت هزینهٔ اتصال",
}


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


async def whisper_cost() -> float:
    """Coins one group whisper costs right now (0 = free).

    Read live like every other price, so turning نجوا into a free feature from
    the panel takes effect on the very next send.
    """
    return max(round_coins((await get_policy()).whisper_cost), 0.0)


def _ledger(
    session,
    user_id: int,
    amount: float,
    reason: str,
    detail: str | None = None,
) -> None:
    """Queue a ledger row on the session that is about to commit the balance.

    Never opens a session of its own: the balance change and its record are one
    transaction or they are not a record at all. ``amount`` is signed — positive
    when the user received coins, negative when they were charged — so the admin
    report can sum both directions without decoding anything.
    """
    amount = round_coins(amount)
    if not amount:
        return
    session.add(
        CoinTransaction(
            user_id=user_id,
            amount=amount,
            reason=reason,
            detail=detail,
        )
    )


# ──────────────────────────────────────────────────
# Premium helper
# ──────────────────────────────────────────────────

def is_premium(user: User) -> bool:
    """Permanent VIP or an active paid/referral subscription."""
    if user.is_vip:
        return True
    if user.premium_until:
        return user.premium_until > datetime.utcnow()
    return False


def has_free_access(user: User) -> bool:
    """True when this user must NOT be charged: admin exemption or a subscription.

    The single place that answers "does this person pay?", so the exemption and
    the subscription cannot be honoured on one entry point and quietly ignored on
    another. Two rows count as a subscription:

    ``has_subscription``
        the explicit flag the admin panel flips.
    ``is_vip`` / ``premium_until``
        the premium tier this bot already had before the paywall existed. Folding
        it in is deliberate: keeping two independent "paid" concepts would mean a
        buyer of one silently losing access to the other.
    """
    if user.is_exempt:
        return True
    if user.has_subscription:
        return True
    return is_premium(user)


#: Schema default of one whisper (``BotPolicy.whisper_cost``'s ``default=``).
#: Display code must NEVER quote this: the live price comes from
#: :func:`whisper_cost`, which is what the balance gate actually charges.
WHISPER_COST_COINS = 1


def no_balance_text(coins: float) -> str:
    """Shown when the balance gate refuses a match or a whisper.

    Takes the balance rather than reading it itself so the number on screen is
    the number the gate just compared against. Never posted in a group — the
    sender's finances are nobody else's business.

    The «اتصال شانسی» line is derived from the live policy instead of being a
    promise: it used to claim random connect is ALWAYS free, which stops being
    true the moment the admin prices it.
    """
    random_cost = max(round_coins(peek_policy().random_chat_cost), 0.0)
    random_line = (
        "• «اتصال شانسی» همیشه رایگان است."
        if random_cost <= 0
        else f"• «اتصال شانسی» {fmt_coins(random_cost)} سکه است."
    )
    return (
        "<b>سکه کافی ندارید.</b>\n\n"
        f"موجودی شما: <b>{fmt_coins(max(coins, 0))}</b> سکه\n\n"
        "<b>راه‌های رایگان:</b>\n"
        "• جایزهٔ روزانه از منوی «🏆 امتیازات و سکه»\n"
        "• دعوت از دوستان با لینک اختصاصی\n\n"
        f"{random_line}"
    )


def _coins_after_charge(user: User, cost: float) -> tuple[bool, float]:
    """``(allowed, coins)`` for one already-loaded user row. No I/O.

    Split out because the read-only picker check and the charging check have to
    reach the same verdict: a policy written down once cannot drift from the
    code that enforces it. ``cost`` is passed in rather than read here so both
    callers bill the ONE price they looked up — a whisper sent while an admin is
    mid-edit must not be quoted 1 and charged 5.
    """
    if has_free_access(user):
        return True, round_coins(user.coins)
    cost = round_coins(cost)
    if cost <= 0:
        return True, round_coins(user.coins)
    # Both sides are already rounded to COIN_DECIMALS, so this comparison is
    # between canonical values — no float dust can make 2.5 look like 2.49999…
    coins = round_coins(user.coins)
    return coins >= cost, coins


async def can_afford_whisper(
    user_id: int,
    *,
    first_name: str | None = None,
    username: str | None = None,
) -> tuple[bool, float]:
    """Would :func:`check_and_deduct_balance` allow this send? ``(allowed, coins)``.

    The side-effect-free twin of :func:`check_and_deduct_balance`, for the places
    that have to answer BEFORE anything is committed: the open inline picker
    shows this verdict as a tappable rejection row, and charging a coin for a
    query the user never sends would be indefensible. The balance is returned so
    the caller can render :func:`no_balance_text` without a second query.
    """
    user = await ensure_user(user_id, first_name, username)
    if user is None:
        return False, 0
    return _coins_after_charge(user, await whisper_cost())


def _atomic_deduct(user_id: int, cost: float):
    """Conditional ``UPDATE`` that bills ``user_id`` exactly ``cost`` coins.

    The affordability check, the free-access check and the write live in ONE
    SQL statement, for two reasons:

    * **No double-spend, no lost verdict.** The old shape (SELECT, compare in
      Python, assign, commit) made two simultaneous sends read the same
      balance; with SQLite's read-then-upgrade locking the loser did not even
      get a clean refusal — it crashed with ``SQLITE_BUSY_SNAPSHOT``. A single
      statement starts from the freshest committed row and simply matches or
      does not.
    * **Free users can never match.** The exemption flags are part of the
      WHERE clause, so an exempt/subscribed/premium row is not "checked then
      skipped" — it is outside the statement entirely. ``IS TRUE`` on each
      flag keeps NULL legacy columns counting as *paying*, exactly like
      :func:`has_free_access`.

    The new balance is rounded in SQL so the column stays canonical (2
    decimals) instead of accumulating ``0.30000000000000004`` dust.
    """
    now = datetime.utcnow()
    free = or_(
        User.is_exempt.is_(True),
        User.has_subscription.is_(True),
        User.is_vip.is_(True),
        and_(User.premium_until.is_not(None), User.premium_until > now),
    )
    return (
        update(User)
        .where(
            User.telegram_id == user_id,
            func.round(func.coalesce(User.coins, 0), COIN_DECIMALS) >= cost,
            ~free,
        )
        .values(
            coins=func.round(
                func.coalesce(User.coins, 0) - cost, COIN_DECIMALS
            )
        )
        .execution_options(synchronize_session=False)
    )


async def check_and_deduct_balance(
    user_id: int,
    *,
    first_name: str | None = None,
    username: str | None = None,
) -> float | None:
    """May ``user_id`` send one whisper — and pay for it if that costs anything?

    The ONE gate that replaced the old fixed "N whispers per day" ceiling. Every
    entry point goes through it (the inline picker, the group command and the
    parked «عضو شدم» continuation alike), so a limit can never be enforced on
    one road and not on another.

    Returns the amount actually charged, as a ``float``:

    * ``> 0``  — allowed, and those coins are gone (the exact number, so
      :func:`refund_balance` can hand back precisely this much even if the
      admin reprices the whisper between charge and refund);
    * ``0.0``  — allowed for free (exempt / subscribed / premium, or the admin
      set the price to 0);
    * ``None`` — refused; the caller shows :func:`no_balance_text`.

    Registration happens first (:func:`ensure_user`), so a member of a group who
    has never pressed ``/start`` is judged against a real row instead of being
    told they have no balance — the same reasoning as the token gate.

    The verdict and the write are one statement (:func:`_atomic_deduct`); when
    it matches nothing, a second read classifies *why* — free access, not
    enough coins, or a vanished row — without writing anything.
    """
    user = await ensure_user(user_id, first_name, username)
    if user is None:
        return None
    if has_free_access(user):
        return 0.0

    cost = await whisper_cost()
    if cost <= 0:
        return 0.0

    async with async_session_factory() as session:
        result = await session.execute(_atomic_deduct(user_id, cost))
        if result.rowcount:
            _ledger(session, user_id, -cost, "whisper")
            await session.commit()
            return cost

        # 0 rows: classify from a fresh read (free flags may have been
        # granted since the first read; the balance may simply be short).
        managed = await session.scalar(
            select(User).where(User.telegram_id == user_id)
        )
        if managed is None:
            return None
        if has_free_access(managed):
            return 0.0
        allowed, _ = _coins_after_charge(managed, cost)
        if not allowed:
            return None
        # Payable, not exempt, yet the atomic UPDATE matched nothing — only a
        # rounding tie between SQLite and Python can produce this. Fail CLOSED:
        # a denied send is retryable, a free whisper is not.
        return None


async def refund_balance(user_id: int, amount: float) -> bool:
    """Give back exactly ``amount`` — the coins a refusal consumed.

    :func:`check_and_deduct_balance` runs BEFORE the anti-spam gate (see
    ``handlers.inline_anon._run_send_gates``), so a send refused for being too
    fast would otherwise burn a coin for a message that was never delivered.

    The amount comes from the charge itself rather than from a re-read of the
    live price: repricing the whisper between charge and refund must not change
    what the sender gets back. ``amount <= 0`` means the send was free
    (exempt/subscribed/zero price — nothing was ever taken) and this is a
    no-op, not an error.

    Best effort: a failed refund costs the admin one coin, while the alternative
    is silently charging for nothing.
    """
    amount = round_coins(amount)
    if amount <= 0:
        return False
    return await add_coins(user_id, amount, reason="refund_whisper")


# ──────────────────────────────────────────────────
# Matching fees — charged ONCE, when a chat is established
# ──────────────────────────────────────────────────

#: Which way a user asked to be matched. ``None`` is «اتصال شانسی»: it matches
#: anyone, and it is free exactly as long as the admin leaves its price at 0.
MatchMode = Optional[str]

MODE_GIRL = "girl"
MODE_BOY = "boy"


async def match_cost(mode: MatchMode) -> float:
    """Coins a successful match costs in ``mode`` (0 = free).

    Read live from ``bot_policy`` on every call rather than baked into a
    constant, so the admin can reprice «چت با دختر» — or turn «اتصال شانسی»
    from free into paid — and have it take effect on the next match with no
    restart.
    """
    policy = await get_policy()
    if mode == MODE_GIRL:
        return max(round_coins(policy.chat_girl_cost), 0.0)
    if mode == MODE_BOY:
        return max(round_coins(policy.chat_boy_cost), 0.0)
    return max(round_coins(policy.random_chat_cost), 0.0)


async def charge_match_fee(
    user_id: int,
    mode: MatchMode,
    *,
    first_name: str | None = None,
    username: str | None = None,
) -> tuple[bool, float]:
    """Charge ``user_id`` for a match that just succeeded. ``(ok, coins)``.

    Called exactly once per established connection, and never while the user is
    still queued: a user who waits in the queue and is never matched pays
    nothing, and a user whose connection fails to establish is refunded by the
    caller. That ordering is the whole point — the fee buys a conversation, not
    an attempt.

    The same transaction-then-recheck shape as :func:`check_and_deduct_balance`,
    for the same reason: the free-access flags are admin-editable, so the verdict
    is re-derived from a row this transaction owns instead of trusting a copy
    read when the queue was entered.
    """
    cost = await match_cost(mode)
    if cost <= 0:
        return True, 0

    user = await ensure_user(user_id, first_name, username)
    if user is None:
        return False, 0
    if has_free_access(user):
        return True, 0

    async with async_session_factory() as session:
        result = await session.execute(_atomic_deduct(user_id, cost))
        if result.rowcount:
            _ledger(session, user_id, -cost, f"match_{mode or 'random'}")
            await session.commit()
            return True, cost

        # 0 rows: classify without writing (see check_and_deduct_balance).
        managed = await session.scalar(
            select(User).where(User.telegram_id == user_id)
        )
        if managed is None:
            return False, 0
        if has_free_access(managed):
            return True, 0
        coins = round_coins(managed.coins)
        if coins < cost:
            return False, coins
        # Payable but unmatched: rounding tie, fail CLOSED (see _atomic_deduct).
        return False, coins


def match_cost_text(policy: BotPolicy | None = None) -> str:
    """One-line summary of what each matching mode costs, for the menus.

    Takes the policy row rather than awaiting one, so a caller that already has
    it does not pay for a second round-trip on a screen it is already rendering.
    A price of 0 renders as «رایگان» rather than «0 سکه» — the same fact, without
    making a free service look like a broken price tag.
    """
    girl = max(round_coins(getattr(policy, "chat_girl_cost", 1)), 0.0) if policy else 1
    boy = max(round_coins(getattr(policy, "chat_boy_cost", 1)), 0.0) if policy else 1
    random = max(round_coins(getattr(policy, "random_chat_cost", 0)), 0.0) if policy else 0

    def price(value: float) -> str:
        return "رایگان" if value <= 0 else f"<b>{fmt_coins(value)}</b> سکه"

    # Each mode carries its own icon so the three are scannable at a glance —
    # this line is the FIRST thing a new user reads about what the bot charges.
    return (
        f"{get_plain_emoji('dice')} اتصال شانسی: {price(random)} · "
        f"{get_plain_emoji('female')} چت با دختر: {price(girl)} · "
        f"{get_plain_emoji('male')} چت با پسر: {price(boy)}"
    )


# ──────────────────────────────────────────────────
# Sliding-window rate limiter (in-memory)
# ──────────────────────────────────────────────────

class _SlidingWindow:
    """Count events per key inside a rolling time window.

    ``check`` is the only entry point and it RECORDS the hit it allows. There is
    deliberately no separate ``record``: the old design had one, and the caller
    invoked it unconditionally right after ``check`` — including on the path
    where ``check`` had just refused. That raised ``AttributeError`` on every
    single chat message (there is no such method), and because the coin was
    already deducted upstream, every send both crashed and lost its price.
    """

    def __init__(self, window: float):
        self.window = window
        self._stamps: dict[int, list[float]] = {}

    def _purge(self, key: int) -> None:
        now = time.monotonic()
        lst = self._stamps.get(key)
        if not lst:
            return
        lst[:] = [t for t in lst if now - t < self.window]

    def check(self, key: int, limit: int) -> tuple[bool, int]:
        """Record an allowed hit; return ``(allowed, wait_seconds_if_not_allowed)``.

        Only an ALLOWED hit is recorded. A refused one must not consume a slot,
        or a user who is being told to slow down would keep pushing their own
        window further out and never recover. (Kept for single-window callers;
        :func:`authorize_chat_message` uses :meth:`peek` + :meth:`record`
        because it has TWO windows that must be decided before either is
        spent.)
        """
        if limit <= 0:
            return True, 0
        self._purge(key)
        lst = self._stamps.setdefault(key, [])
        if len(lst) < limit:
            lst.append(time.monotonic())
            return True, 0
        oldest = min(lst) if lst else time.monotonic()
        wait = int(self.window - (time.monotonic() - oldest)) + 1
        return False, max(wait, 1)

    def peek(self, key: int, limit: int) -> int:
        """Would a hit be allowed right now? ``0`` = yes, else the wait.

        Records NOTHING — that is the whole point. The limiter has two windows
        (minute and hour) and a message may fail either; recording while only
        one of them has passed would burn a slot of the passing window for a
        message that was never delivered, so both windows must be asked BEFORE
        either is written to.
        """
        if limit <= 0:
            return 0
        self._purge(key)
        lst = self._stamps.get(key) or []
        if len(lst) < limit:
            return 0
        oldest = min(lst)
        wait = int(self.window - (time.monotonic() - oldest)) + 1
        return max(wait, 1)

    def record(self, key: int) -> None:
        """Spend one slot for ``key`` (call only after every window said yes)."""
        self._purge(key)
        self._stamps.setdefault(key, []).append(time.monotonic())

    def reset(self, key: int) -> None:
        """Forget a user's history — used when a chat starts or ends.

        A fresh conversation should not inherit the previous one's burst budget,
        and this is also what keeps the dict from growing without bound for
        accounts that chat once and never return.
        """
        self._stamps.pop(key, None)


_min_window = _SlidingWindow(60)      # per-minute window
_hour_window = _SlidingWindow(3600)   # per-hour window
_warned_at: dict[int, float] = {}


def reset_rate_limits(user_id: int) -> None:
    """Clear both windows and the warning cooldown for one user."""
    _min_window.reset(user_id)
    _hour_window.reset(user_id)
    _warned_at.pop(user_id, None)


def should_warn(user_id: int, cooldown: float = 45.0) -> bool:
    """Throttle warnings so a limited user isn't bombarded with messages."""
    now = time.monotonic()
    last = _warned_at.get(user_id, 0.0)
    if now - last < cooldown:
        return False
    _warned_at[user_id] = now
    return True


# ──────────────────────────────────────────────────
# One-shot authorization for sending a chat message
# ──────────────────────────────────────────────────

@dataclass
class ChatAuth:
    allowed: bool
    reason: str      # "ok" | "rate"
    wait: int = 0    # seconds to wait (rate limit)
    remaining: int = 0
    is_vip: bool = False


async def _persist_user(user: User) -> None:
    async with async_session_factory() as session:
        await session.merge(user)
        await session.commit()


async def ensure_user(
    user_id: int,
    first_name: str | None = None,
    username: str | None = None,
) -> User | None:
    """Return the user row for ``user_id``, creating it on first sight.

    ``/start`` is NOT the only way into this bot. A member of a group can send
    a whisper, an inline whisper, or press one of our buttons long before ever
    opening a private chat with us — and every one of those entry points used
    to treat "no row in ``users``" as *out of credit*. The result was a brand
    new member being told their balance was empty about a charge they had never
    incurred, which read as a bug and blocked the feature outright.

    Registering on first use keeps the economy self-consistent: unknown equals
    brand new, and brand new equals the welcome coins, which is exactly what
    ``cmd_start`` hands out. Existing rows are returned untouched; only the name
    is back-filled when we happen to know it and the row does not have one yet.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

    if user is not None:
        touched = False
        if not user.username and username:
            user.username = username
            touched = True
        if not user.first_name and first_name:
            user.first_name = first_name
            touched = True
        if touched:
            await _persist_user(user)
        return user

    policy = await get_policy()
    async with async_session_factory() as session:
        session.add(
            User(
                telegram_id=user_id,
                first_name=first_name,
                username=username,
                coins=policy.welcome_coins,
            )
        )
        _ledger(session, user_id, policy.welcome_coins, "welcome")
        await session.commit()
    return await ensure_user(user_id)


async def log_coin(
    user_id: int,
    amount: float,
    reason: str,
    detail: str | None = None,
) -> None:
    """Record a coin movement that was committed elsewhere.

    For callers that own their own transaction (``handlers.start`` creates the
    user row itself, because it also has to know whether this was a first
    ``/start`` for the referral). The balance is already final by the time this
    runs — this is the receipt, not the payment.
    """
    if not amount:
        return
    async with async_session_factory() as session:
        _ledger(session, user_id, amount, reason, detail)
        await session.commit()


async def authorize_chat_message(
    user_id: int,
    first_name: str | None = None,
    username: str | None = None,
) -> ChatAuth:
    """Anti-flood check before relaying a message inside a live chat.

    This used to also charge a per-message cost. It no longer does — and must
    not: the price of a conversation is taken once, by :func:`charge_match_fee`
    at the moment the connection is established, and talking on an established
    chat is free until it hits its 24-hour limit.

    So the whole of this function is the rate limit:

      1. register the user on first use (see :func:`ensure_user`)
      2. apply the per-minute / per-hour sliding windows

    Too fast means the message is dropped with a warning. Nothing is charged, so
    flooding cannot drain a balance — the anti-spam rule and the billing rule
    are now genuinely independent, and a user can be warned as often as they
    like for free.
    """
    policy = await get_policy()

    user = await ensure_user(user_id, first_name, username)
    if user is None:
        return ChatAuth(False, "rate", remaining=0)

    vip = is_premium(user)

    if not policy.enabled:
        return ChatAuth(True, "ok", remaining=user.coins or 0, is_vip=vip)

    mult = policy.vip_multiplier if vip else 1

    # Peek BOTH windows first, record in NEITHER until both have said yes.
    # The old order called the minute window's check() — which records the hit
    # it allows — before the hour window had voted, so a message refused for
    # the hour cap still ate a minute slot: the user was punished twice for
    # one undelivered message.
    per_minute = max(policy.messages_per_minute, 1)
    wait_min = _min_window.peek(user_id, per_minute * mult)
    if wait_min:
        return ChatAuth(
            False, "rate", wait=wait_min, remaining=user.coins or 0, is_vip=vip
        )

    per_hour = max(policy.messages_per_hour, 1)
    wait_hour = _hour_window.peek(user_id, per_hour * mult)
    if wait_hour:
        return ChatAuth(
            False, "rate", wait=wait_hour, remaining=user.coins or 0, is_vip=vip
        )

    _min_window.record(user_id)
    _hour_window.record(user_id)
    return ChatAuth(True, "ok", remaining=user.coins or 0, is_vip=vip)


# ──────────────────────────────────────────────────
# Friendly warning texts
# ──────────────────────────────────────────────────

def chat_lifetime_text(hours: int) -> str:
    """The 24-hour rule, stated in one line for the menus and the chat header."""
    return (
        f"{get_plain_emoji('clock')} هر چت حداکثر <b>{max(hours, 1)} ساعت</b> "
        "باز می‌ماند و بعد به‌صورت خودکار بسته می‌شود."
    )


def rate_limit_text(wait: int, per_minute: int = 20) -> str:
    """Shown when the user sends too fast.

    Takes the limit it is quoting so the number on screen is the number the
    limiter actually enforced, the same way :func:`no_balance_text` does.

    Explicitly says nothing was charged. Without that line a user who is being
    rate-limited assumes they are being billed and stops typing entirely, which
    is the opposite of what the limit is for.
    """
    return (
        "<b>کمی آرام‌تر.</b>\n\n"
        f"حداکثر <b>{per_minute}</b> پیام در دقیقه مجاز است. "
        f"<b>{wait} ثانیه</b> صبر کنید.\n"
        "هیچ سکه‌ای کسر نمی‌شود."
    )


# ──────────────────────────────────────────────────
# Wallet / profile info
# ──────────────────────────────────────────────────

async def wallet_info(tg_id: int) -> dict | None:
    """Return a snapshot of the user's wallet (or None)."""
    policy = await get_policy()
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        return None

    premium = is_premium(user)
    premium_text = (
        f"تا {user.premium_until:%Y-%m-%d}" if user.premium_until else "همیشگی"
    ) if user.is_vip else (
        f"تا {user.premium_until:%Y-%m-%d}" if premium and user.premium_until else "فعال نیست"
    )

    # ── Daily bonus availability ──
    daily_ok = False
    daily_wait = 0
    if user.last_daily_bonus is not None:
        elapsed = datetime.utcnow() - user.last_daily_bonus
        if elapsed < timedelta(hours=24):
            daily_wait = int(24 * 3600 - elapsed.total_seconds())
        else:
            daily_ok = True
    else:
        daily_ok = True

    return {
        "coins": user.coins or 0,
        "premium": premium,
        "premium_text": premium_text,
        "referral_count": user.referral_count or 0,
        "daily_bonus_coins": policy.daily_bonus_coins,
        "daily_claimable": daily_ok,
        "daily_wait_seconds": daily_wait,
        # Referral rewards are surfaced straight from the policy row so the
        # screen always quotes what will ACTUALLY be paid. Nothing here is a
        # literal: the admin sets both numbers in the panel and this card
        # changes with them.
        "referral_invitee_coins": policy.referral_invitee_coins,
        "referral_coin_reward": policy.referral_coin_reward,
        "referral_premium_days": policy.referral_premium_days,
        "chat_girl_cost": policy.chat_girl_cost,
        "chat_boy_cost": policy.chat_boy_cost,
        # The whole pricing block, live: the wallet used to hardcode
        # «اتصال شانسی: رایگان», which the admin can now change.
        "random_chat_cost": policy.random_chat_cost,
        "whisper_cost": policy.whisper_cost,
        "chat_lifetime_hours": policy.chat_lifetime_hours,
    }


# ──────────────────────────────────────────────────
# Grants (used by referral rewards & admin gifts)
# ──────────────────────────────────────────────────

async def add_coins(
    tg_id: int,
    amount: float,
    *,
    reason: str = "gift",
    detail: str | None = None,
) -> bool:
    """Credit ``amount`` coins to a user. Returns False if user not found.

    ``reason`` is a :class:`CoinTransaction` code — it defaults to ``"gift"``
    because an unlabelled credit is an admin gift in every caller that does not
    say otherwise (the panel's «هدیه دادن»), while refunds and payouts pass
    their own so the report never counts a returned charge as new income.
    """
    amount = round_coins(amount)
    if amount <= 0:
        return False
    async with async_session_factory() as session:
        # Credit + existence check in ONE statement: a read-modify-write here
        # could lose a concurrent deduction (the classic lost update), and it
        # turned a plain balance query into a read-then-upgrade transaction
        # that could die with SQLITE_BUSY_SNAPSHOT mid-gift.
        result = await session.execute(
            update(User)
            .where(User.telegram_id == tg_id)
            .values(
                coins=func.round(
                    func.coalesce(User.coins, 0) + amount, COIN_DECIMALS
                )
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return False
        _ledger(session, tg_id, amount, reason, detail)
        await session.commit()
    return True


async def add_premium_days(tg_id: int, days: int) -> bool:
    """Extend (or create) a premium subscription by ``days`` days."""
    if days <= 0:
        return False
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return False
        base = user.premium_until or datetime.utcnow()
        user.premium_until = max(base, datetime.utcnow()) + timedelta(days=days)
        await session.commit()
    return True


async def bulk_add_coins(
    amount: float,
    *,
    user_ids: list[int] | None = None,
    reason: str = "gift",
    detail: str | None = None,
) -> list[int]:
    """Credit ``amount`` coins to MANY users in a single transaction.

    ``user_ids=None`` credits every non-banned account — the same population
    the panel's broadcast uses, so «هدیه به همه» and «ارسال همگانی» agree on
    who counts as a user. An explicit list restricts the credit to exactly
    those accounts; that is how the random-N gift keeps the grant set and the
    notification set identical.

    One balance change + one ledger row per recipient, committed together: a
    partial gift that half the users can see would be worse than no gift at
    all. Returns the telegram_ids actually credited (empty = nobody qualified,
    nothing written).
    """
    amount = round_coins(amount)
    if amount <= 0:
        return []
    async with async_session_factory() as session:
        stmt = select(User)
        if user_ids is None:
            stmt = stmt.where(User.is_banned.is_(False))
        else:
            stmt = stmt.where(User.telegram_id.in_(user_ids))
        users = list((await session.execute(stmt)).scalars().all())

        granted: list[int] = []
        for user in users:
            user.coins = round_coins((user.coins or 0) + amount)
            _ledger(session, user.telegram_id, amount, reason, detail)
            granted.append(user.telegram_id)
        await session.commit()
    return granted


async def bulk_add_premium_days(
    days: int,
    *,
    user_ids: list[int] | None = None,
) -> list[int]:
    """Extend MANY subscriptions by ``days`` days in a single transaction.

    Per row the semantics match :func:`add_premium_days` exactly (a live
    subscription is extended from ``max(premium_until, now)`` so it never
    shortens), and the selection matches :func:`bulk_add_coins`. Returns the
    telegram_ids actually credited.
    """
    if days <= 0:
        return []
    now = datetime.utcnow()
    async with async_session_factory() as session:
        stmt = select(User)
        if user_ids is None:
            stmt = stmt.where(User.is_banned.is_(False))
        else:
            stmt = stmt.where(User.telegram_id.in_(user_ids))
        users = list((await session.execute(stmt)).scalars().all())

        granted: list[int] = []
        for user in users:
            base = user.premium_until or now
            user.premium_until = max(base, now) + timedelta(days=days)
            granted.append(user.telegram_id)
        await session.commit()
    return granted


async def claim_daily_bonus(tg_id: int) -> tuple[bool, int, int]:
    """Claim the once-per-24h coin bonus from the wallet screen.

    Returns ``(ok, coins_granted, wait_seconds_until_next_claim)``.
    When ``ok`` is False the user must wait ``wait`` seconds before retrying.
    """
    policy = await get_policy()
    async with async_session_factory() as session:
        # "Claim if the cooldown allows it" and the write are one statement,
        # so a double-tap (two devices, a replayed callback) cannot pass the
        # Python-side cooldown check twice and pay out twice.
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=24)
        result = await session.execute(
            update(User)
            .where(
                User.telegram_id == tg_id,
                or_(
                    User.last_daily_bonus.is_(None),
                    User.last_daily_bonus <= cutoff,
                ),
            )
            .values(
                last_daily_bonus=now,
                coins=func.round(
                    func.coalesce(User.coins, 0) + policy.daily_bonus_coins,
                    COIN_DECIMALS,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            _ledger(session, tg_id, policy.daily_bonus_coins, "daily")
            await session.commit()
            return True, policy.daily_bonus_coins, 0

        # Refused: either no such user or the cooldown is still running.
        user = await session.scalar(
            select(User).where(User.telegram_id == tg_id)
        )
        if user is None:
            return False, 0, 0
        if user.last_daily_bonus is not None:
            elapsed = now - user.last_daily_bonus
            if elapsed < timedelta(hours=24):
                wait = int(24 * 3600 - elapsed.total_seconds())
                return False, 0, max(wait, 1)
        # Cooldown expired and the row exists, yet nothing matched — only a
        # concurrent claim between our UPDATE and this read explains it; treat
        # it as theirs and make the caller wait a beat.
        return False, 0, 1


async def grant_invitee_bonus(tg_id: int) -> int:
    """Give the referral welcome bonus (coins) to a newly-joined member.

    Returns the number of coins granted (0 when disabled or user missing).
    """
    policy = await get_policy()
    invite_coins = getattr(policy, 'referral_invitee_coins', 0)
    if invite_coins <= 0:
        return 0
    async with async_session_factory() as session:
        result = await session.execute(
            update(User)
            .where(User.telegram_id == tg_id)
            .values(
                coins=func.round(
                    func.coalesce(User.coins, 0) + invite_coins,
                    COIN_DECIMALS,
                )
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return 0
        _ledger(session, tg_id, invite_coins, "invitee")
        await session.commit()
    return invite_coins


async def reward_referral(inviter_id: int) -> tuple[int, int, int]:
    """Grant the referral reward to an inviter.

    Returns ``(coins_granted, premium_days, total_referrals)``.

    Both amounts come from ``bot_policy`` at call time, so the price of an invite
    is whatever the admin last set in the panel — there is no default baked in
    here to drift from it.
    """
    policy = await get_policy()
    async with async_session_factory() as session:
        # Coins + counter in ONE statement: the pair must move together (the
        # ledger detail quotes the new count, and a lost update here would
        # hand out a reward whose referral count never grew).
        result = await session.execute(
            update(User)
            .where(User.telegram_id == inviter_id)
            .values(
                coins=func.round(
                    func.coalesce(User.coins, 0) + policy.referral_coin_reward,
                    COIN_DECIMALS,
                ),
                referral_count=func.coalesce(User.referral_count, 0) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return 0, 0, 0

        inviter = await session.scalar(
            select(User).where(User.telegram_id == inviter_id)
        )
        if inviter is None:  # pragma: no cover - deleted between two statements
            await session.rollback()
            return 0, 0, 0

        # Logged as its OWN reason rather than folded into "income": this is
        # the number the admin panel's referral report sums, and it has to
        # survive the reward being repriced next month.
        _ledger(
            session,
            inviter_id,
            policy.referral_coin_reward,
            "referral",
            f"ref #{inviter.referral_count}",
        )

        days = policy.referral_premium_days
        if days > 0:
            base = inviter.premium_until or datetime.utcnow()
            inviter.premium_until = max(base, datetime.utcnow()) + timedelta(days=days)

        await session.commit()

    return policy.referral_coin_reward, days, inviter.referral_count


__all__ = [
    "WHISPER_COST_COINS",
    "MODE_BOY",
    "MODE_GIRL",
    "MatchMode",
    "fmt_coins",
    "round_coins",
    "parse_amount",
    "parse_int",
    "get_policy",
    "refresh_policy",
    "peek_policy",
    "whisper_cost",
    "is_premium",
    "has_free_access",
    "ensure_user",
    "can_afford_whisper",
    "check_and_deduct_balance",
    "refund_balance",
    "no_balance_text",
    "match_cost",
    "match_cost_text",
    "charge_match_fee",
    "authorize_chat_message",
    "chat_lifetime_text",
    "rate_limit_text",
    "should_warn",
    "reset_rate_limits",
    "wallet_info",
    "add_coins",
    "add_premium_days",
    "claim_daily_bonus",
    "grant_invitee_bonus",
    "reward_referral",
    "log_coin",
    "ChatAuth",
]