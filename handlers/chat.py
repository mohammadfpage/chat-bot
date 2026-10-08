"""
Core chat logic — 1-on-1 matching and real-time message relay.

Design notes:
  * A user can chat with ONE partner at a time.
  * pair_map / search_queue are kept in-memory (fast, no DB writes).
  * Messages are ONLY relayed live — nothing is persisted to the database.
  * Videos / video notes / animations are strictly blocked.
  * Blocking is peer-to-peer (BlockList table), NOT global (User.is_banned).
  * Privacy: telegram_id is NEVER sent to peers; only first_name + profile photo.

Pricing and lifetime
---------------------
Three ways to get matched, all one handler (:func:`start_search`):

    «🔀 اتصال شانسی»   ``random_chat_cost`` coins (0 = free by default), matches anyone
    «👩 چت با دختر»    ``chat_girl_cost`` coins — matches women
    «👨 چت با پسر»     ``chat_boy_cost`` coins — matches men

All three prices live in ``bot_policy`` and are read at charge time, so the
admin can make any of them free — or paid — from the panel without a restart.

The fee is taken by :func:`_connect` at the moment BOTH users are paired, and
never while somebody is still queueing — so a user who waits and is never
matched pays nothing, and one whose partner never materialises is refunded.
Talking inside an established chat is free: :func:`_enforce_chat_auth`
rate-limits and nothing else.

A chat lives at most ``BotPolicy.chat_lifetime_hours`` (24h). The clock starts
when the pair is made and is checked on every message, so a conversation that is
actually being used never expires, while one left open overnight is closed for
both sides the moment it is next touched.
"""

import asyncio
import logging
import time
from contextlib import suppress
from html import escape
from typing import Optional

from aiogram import Bot, Router, F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import settings
from database import async_session_factory, User, BlockList, UserReport
from keyboards import (
    NEXT_CHAT_LABEL,
    REMATCH_LABEL,
    chat_end_kb,
    main_menu_kb,
    queue_menu_kb,
    chat_menu_kb,
    rematch_offer_kb,
    RANDOM_CONNECT_LABEL,
    CHAT_WITH_GIRL_LABEL,
    CHAT_WITH_BOY_LABEL,
)
from states import ChatState
from utils.economy import (
    MODE_BOY,
    MODE_GIRL,
    MatchMode,
    add_coins,
    authorize_chat_message,
    charge_match_fee,
    chat_lifetime_text,
    fmt_coins,
    get_policy,
    match_cost,
    rate_limit_text,
    reset_rate_limits,
    should_warn,
)

logger = logging.getLogger(__name__)
router = Router()

# Matching, menus and message relay are private-chat only: a group must never
# receive the bot's keyboards (or have its text eaten by a chat session).
router.message.filter(F.chat.type == ChatType.PRIVATE)

# ──────────────────────────────────────────────────
# In-memory chat state tracking
#   pair_map:      telegram_id -> partner_telegram_id
#   search_queue:  {user_id: (mode, gender)} waiting for a partner
#   chat_opened_at: telegram_id -> monotonic clock when this chat started
# ──────────────────────────────────────────────────
#:
#: The queue stores the waiter's GENDER as well as their mode. It has to: a
#: targeted mode is a request for a specific gender, so verifying a candidate
#: needs their gender, and re-reading the database once per queued user would
#: turn one button press into N queries.
pair_map: dict[int, int] = {}
search_queue: dict[int, tuple[MatchMode, str | None]] = {}
chat_opened_at: dict[int, float] = {}
chat_lock = asyncio.Lock()

#: Pending rematch invitations: offered partner -> (requester, when offered).
#: In-memory like ``pair_map`` — a restart clears it, and an invitation that
#: outlives the process takes its stale self with it.
rematch_offers: dict[int, tuple[int, float]] = {}

#: An invitation nobody answered within this many seconds is dead. Long
#: enough to survive "let me think about it", short enough that accepting an
#: hour-old request does not surprise somebody who forgot the conversation.
_REMATCH_TTL = 600.0

#: The partner each user last FINISHED a chat with — written on every clean
#: end («لغو چت», report, expiry sweep) and on every re-pair. Drives the
#: «🔁 اتصال مجدد» reply button on the chat-ended card: a reply button cannot
#: carry a target id, and the card must not print one either, so the id lives
#: here instead of in a callback payload the user can see.
last_partner: dict[int, int] = {}

#: The search mode each user last STARTED (None = اتصال شانسی), so
#: «🔍 چت بعدی» repeats exactly what they asked for last time with one tap.
last_mode: dict[int, MatchMode] = {}

#: Pairs whose rematch request was DECLINED — one entry per unordered pair,
#: set when the offer is refused. Once declined, NEITHER side may ask again:
#: re-requesting after a "no" is the nagging this lockout exists to prevent.
#: In-memory like ``rematch_offers`` — a restart forgets it, which is the
#: right trade-off for a grudge nobody should carry across a bot update.
_rematch_declined: set[frozenset[int]] = set()


def _decline_key(a: int, b: int) -> frozenset[int]:
    """Order-independent key for a decline lockout."""
    return frozenset((a, b))


def _remember_pair_end(user_id: int, partner_id: int) -> None:
    """Record the partner this user just finished with, in both directions."""
    last_partner[user_id] = partner_id
    last_partner[partner_id] = user_id


def _forget_pair_end(user_id: int) -> None:
    """Drop the stored partner of ``user_id`` (and the reciprocal pointer)."""
    pid = last_partner.pop(user_id, None)
    if pid is not None and last_partner.get(pid) == user_id:
        last_partner.pop(pid, None)

#: How often the background sweep closes chats that have outlived their lifetime.
#: The per-message check in :func:`_relay_allowed` is the real guarantee; this
#: only makes the close happen *by itself*, so a chat nobody is typing in still
#: ends on time instead of lingering until the next message.
_EXPIRY_SWEEP_SECONDS = 300

_expiry_task: asyncio.Task | None = None

#: Which mode a user pressing ``label`` is asking for. ``None`` = اتصال شانسی,
#: which also means "I will take anyone" — and is free exactly as long as the
#: admin leaves ``random_chat_cost`` at 0.
_LABEL_TO_MODE: dict[str, MatchMode] = {
    RANDOM_CONNECT_LABEL: None,
    CHAT_WITH_GIRL_LABEL: MODE_GIRL,
    CHAT_WITH_BOY_LABEL: MODE_BOY,
}

#: The gender each mode is looking for. A mode with no entry (random) takes
#: anyone. Written out rather than derived so «چت با دختر» can never accidentally
#: come to mean "boys".
_MODE_WANTS: dict[str, str] = {
    MODE_GIRL: "female",
    MODE_BOY: "male",
}

#: Fallback lifespan if the policy row cannot be read. Matches the schema default.
_DEFAULT_LIFETIME_HOURS = 24


# ──────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────

async def get_user(tg_id: int) -> User | None:
    """Fetch a user row from the database (or None)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        return result.scalar_one_or_none()


async def is_profile_complete(tg_id: int) -> bool:
    user = await get_user(tg_id)
    return bool(user and user.is_profile_complete)


async def is_banned(tg_id: int) -> bool:
    user = await get_user(tg_id)
    return bool(user and user.is_banned)


async def are_blocked(user_a: int, user_b: int) -> bool:
    """Return True if user_a blocked user_b OR user_b blocked user_a."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_a) & (BlockList.blocked_id == user_b)
            )
        )
        if result.scalar_one_or_none() is not None:
            return True

        result = await session.execute(
            select(BlockList).where(
                (BlockList.blocker_id == user_b) & (BlockList.blocked_id == user_a)
            )
        )
        return result.scalar_one_or_none() is not None


async def force_set_state(
    storage: BaseStorage, bot_id: int, user_id: int, state
) -> None:
    """Directly set another user's FSM state via shared storage."""
    key = StorageKey(bot_id=bot_id, user_id=user_id, chat_id=user_id)
    await storage.set_state(key=key, state=state)


async def _force_clear_peer_state(
    storage: BaseStorage, bot_id: int, user_id: int
) -> None:
    """Forcefully clear a peer's FSM state AND data (fixes stuck In_Chat)."""
    key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
    await storage.set_state(key=key, state=None)
    await storage.set_data(key=key, data={})


def _format_profile(user: User) -> str:
    """Format a user's profile for the match reveal message.

    NEVER includes telegram_id — only the fields the user filled in.
    Every field is escaped: name, city and height are free text the peer
    typed, and the reveal is sent with the bot's HTML default.
    """
    name = escape(user.first_name or "ناشناس")
    return (
        f"نام: {name}\n"
        f"سن: {user.age or '—'}\n"
        f"شهر: {escape(user.city) if user.city else '—'}\n"
        f"قد: {escape(user.height) if user.height else '—'}"
    )


async def _send_profile_with_photo(
    bot, target_id: int, text: str, kb, source_user: User | None
) -> None:
    """Send a match notification with the source user's profile photo.

    Only sends a photo when the source user chose to show it in setup
    (``show_profile_photo``) — stored custom photo first, otherwise the
    latest Telegram profile photo.
    """
    photo_file_id: str | None = None
    if source_user and source_user.show_profile_photo:
        if source_user.profile_photo:
            photo_file_id = source_user.profile_photo
        else:
            photos = await bot.get_user_profile_photos(
                source_user.telegram_id, limit=1
            )
            if photos and photos.total_count > 0:
                photo_file_id = photos.photos[0][-1].file_id

    try:
        if photo_file_id:
            await bot.send_photo(
                target_id,
                photo=photo_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                target_id,
                text,
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as e:
        logger.warning("Failed to send profile to %s: %s", target_id, e)


# ──────────────────────────────────────────────────
# Chat lifetime
# ──────────────────────────────────────────────────

async def _lifetime_seconds() -> int:
    """How long one chat may live, in seconds, from ``bot_policy``.

    Read per call rather than cached at import so the admin can shorten or
    extend it from the panel. The fallback applies only if the policy row cannot
    be read at all — a broken settings read must not silently turn the 24-hour
    cap into "unlimited".
    """
    try:
        policy = await get_policy()
        hours = max(policy.chat_lifetime_hours or 0, 1)
    except Exception as exc:  # noqa: BLE001 — a limit must not break the chat
        logger.warning("Could not read chat_lifetime_hours: %s", exc)
        hours = _DEFAULT_LIFETIME_HOURS
    return hours * 3600


def _expired(user_id: int, lifetime: int) -> bool:
    """True when this user's chat has outlived its allowed span."""
    opened = chat_opened_at.get(user_id)
    return opened is not None and (time.monotonic() - opened) > lifetime


async def _sweep_expired(bot: Bot, fsm_storage: BaseStorage) -> int:
    """Close every chat past its lifetime and notify both sides. Returns the count.

    Reads a consistent snapshot of the maps under :data:`chat_lock` so a pair can
    never be seen half-removed. Both partners appear in both maps, so pairs are
    collected as unordered sets — without that, one pair would be torn down (and
    told "chat ended") twice.

    It does its own teardown rather than reusing :func:`_force_end`, because that
    helper answers the *triggering* ``Message``: here there is no message, and a
    sweep must not post into whichever chat happened to be the odd one out.
    """
    lifetime = await _lifetime_seconds()
    now = time.monotonic()

    async with chat_lock:
        stale = {
            frozenset((uid, pair_map[uid]))
            for uid, opened in chat_opened_at.items()
            if uid in pair_map and (now - opened) > lifetime
        }

    if not stale:
        return 0

    # Read once for the whole sweep, not once per pair: a burst of expiries on a
    # busy bot is exactly when the database is least welcome.
    policy = await get_policy()
    hours = max(policy.chat_lifetime_hours, 1)
    reason = f"مدت مجاز چت ({hours} ساعت) به پایان رسید."

    closed = 0
    for pair in stale:
        # Arbitrary but deterministic: iteration order of a set is not stable.
        first = min(pair)
        partner = max(pair)
        if partner not in pair_map or pair_map.get(partner) != first:
            continue  # already torn down by a message racing the sweep

        pair_map.pop(first, None)
        pair_map.pop(partner, None)
        chat_opened_at.pop(first, None)
        chat_opened_at.pop(partner, None)
        _remember_pair_end(first, partner)
        for uid in (first, partner):
            reset_rate_limits(uid)
            try:
                await force_set_state(
                    fsm_storage, bot.id, uid, ChatState.idle
                )
            except Exception as exc:  # noqa: BLE001 — cleanup must not stop the sweep
                logger.warning("Could not free FSM state for %s: %s", uid, exc)

        for uid in (first, partner):
            try:
                await bot.send_message(
                    uid,
                    f"<b>چت پایان یافت.</b>\n\n{reason}",
                    parse_mode="HTML",
                    reply_markup=chat_end_kb(True),
                )
            except Exception as exc:
                logger.warning("Could not notify %s of expiry: %s", uid, exc)
        logger.info("Chat between %s and %s expired (%s hours)", first, partner, hours)
        closed += 1

    return closed


async def chat_expiry_loop(bot: Bot, fsm_storage: BaseStorage) -> None:
    """Close expired chats on a timer until cancelled."""
    while True:
        try:
            await asyncio.sleep(_EXPIRY_SWEEP_SECONDS)
            await _sweep_expired(bot, fsm_storage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive one bad sweep
            logger.warning("Chat expiry sweep failed: %s", exc)


async def start_chat_expiry(bot: Bot, storage: BaseStorage) -> None:
    """Startup hook: begin closing expired chats in the background.

    ``storage`` arrives through ``dp.workflow_data`` — aiogram's ``EventObserver``
    injects ``bot`` plus the workflow data and nothing else, so the parameter name
    has to match the key registered in ``bot.py``.

    Must be ``async def`` — see :func:`utils.roster_sync.start_roster_sync` for
    why a sync ``dp.startup`` callback cannot create tasks here.
    """
    global _expiry_task
    if _expiry_task is not None and not _expiry_task.done():
        return
    _expiry_task = asyncio.create_task(chat_expiry_loop(bot, storage))


async def stop_chat_expiry() -> None:
    """Shutdown hook: stop the expiry loop."""
    global _expiry_task

    task, _expiry_task = _expiry_task, None
    if task is None:
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _force_end(
    message: Message,
    state: FSMContext,
    fsm_storage: BaseStorage,
    user_id: int,
    reason: str,
) -> None:
    """Tear a pair down and tell both sides why.

    Shared by the user-facing «لغو چت», by /report, and by the expiry check, so
    there is exactly one place that knows how to unpair two people and free their
    FSM states — getting that half right is what strands a partner in
    ``in_chat`` with nobody to talk to.

    Both end cards are sent with ``chat_end_kb`` — a REPLACEMENT reply keyboard
    whose first row is «🔍 چت بعدی». Attaching only an inline button (what this
    did before) left the in-chat «لغو چت»/«بلاک» rows on screen with no handler
    behind them in the idle state: two visibly pressable buttons that did
    nothing, and no visible route to the next chat at all.
    """
    partner_id = pair_map.pop(user_id, None)
    chat_opened_at.pop(user_id, None)
    reset_rate_limits(user_id)

    if partner_id:
        pair_map.pop(partner_id, None)
        chat_opened_at.pop(partner_id, None)
        reset_rate_limits(partner_id)
        _remember_pair_end(user_id, partner_id)
        await force_set_state(fsm_storage, message.bot.id, partner_id, ChatState.idle)
        try:
            await message.bot.send_message(
                partner_id,
                f"<b>چت پایان یافت.</b>\n\n{reason}",
                parse_mode="HTML",
                reply_markup=chat_end_kb(True),
            )
        except Exception as e:
            logger.warning("Failed to notify partner %s: %s", partner_id, e)

    await state.set_state(ChatState.idle)
    await message.answer(
        f"<b>چت پایان یافت.</b>\n\n{reason}",
        parse_mode="HTML",
        reply_markup=chat_end_kb(user_id in last_partner),
    )


# ──────────────────────────────────────────────────
# Start search / find partner
# ──────────────────────────────────────────────────

@router.message(F.text.in_(list(_LABEL_TO_MODE)))
async def start_search(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Match the user, in whichever of the three modes they asked for."""
    await _begin_search(
        message, state, fsm_storage, _LABEL_TO_MODE[message.text.strip()]
    )


async def _begin_search(
    message: Message,
    state: FSMContext,
    fsm_storage: BaseStorage,
    mode: MatchMode,
) -> None:
    """Queue or match the author of ``message`` for ``mode``.

    Extracted from the «اتصال شانسی»/«چت با دختر»/«چت با پسر» button handler so
    «🔍 چت بعدی» runs the exact same path — one body for the ban/profile/queue
    logic means one place to fix it, and the reminder button can only ever
    repeat what the labelled buttons do.
    """
    user_id = message.from_user.id

    if await is_banned(user_id):
        await message.answer("شما از ربات محروم شده‌اید.")
        return

    me = await get_user(user_id)
    if me is None or not me.is_profile_complete:
        await message.answer(
            "برای اتصال، پروفایل خود را کامل کنید.\n"
            "از دکمهٔ «👤 پروفایل من» شروع کنید.",
            reply_markup=main_menu_kb(),
        )
        return

    # A paid mode needs a declared gender on BOTH ends: ours to know who to look
    # for, the queue's to know who is allowed to be found.
    wants = _MODE_WANTS.get(mode) if mode else None
    if wants and not me.gender:
        await message.answer(
            "برای این حالت باید جنسیت خود را در پروفایل مشخص کنید.\n"
            "از دکمهٔ «👤 پروفایل من» استفاده کنید.",
            reply_markup=main_menu_kb(),
        )
        return

    if user_id in pair_map:
        await message.answer(
            "شما در یک چت فعال هستید.",
            reply_markup=chat_menu_kb(),
        )
        return

    if await state.get_state() == ChatState.in_queue:
        await message.answer(
            "در حال جستجو هستید.",
            reply_markup=queue_menu_kb(),
        )
        return

    # ── Find a partner ──
    # Remembered only now, after every validation passed: «چت بعدی» should
    # repeat a search the user was actually allowed to start, not one that was
    # refused for an incomplete profile.
    last_mode[user_id] = mode
    partner_id: Optional[int] = None
    partner_mode: Optional[MatchMode] = None
    async with chat_lock:
        # Drop entries that can no longer be paired: already chatting, or the
        # same user pressing a second button while queued.
        stale = {
            uid
            for uid in search_queue
            if uid in pair_map or uid == user_id
        }
        for uid in stale:
            search_queue.pop(uid, None)

        for candidate in list(search_queue):
            their_mode, their_gender = search_queue[candidate]
            # Mutual gender compatibility — see _gender_ok.
            if not _gender_ok(mode, me.gender, their_mode, their_gender):
                continue
            if not await are_blocked(user_id, candidate):
                partner_id = candidate
                partner_mode = their_mode
                search_queue.pop(candidate, None)
                break

        if partner_id is not None:
            pair_map[user_id] = partner_id
            pair_map[partner_id] = user_id
            now = time.monotonic()
            chat_opened_at[user_id] = now
            chat_opened_at[partner_id] = now
        else:
            search_queue[user_id] = (mode, me.gender)

    if partner_id is not None:
        await state.set_state(ChatState.in_chat)
        await force_set_state(
            fsm_storage, message.bot.id, partner_id, ChatState.in_chat
        )
        await _announce_match(
            message.bot,
            fsm_storage,
            user_id,
            partner_id,
            mode,
            partner_mode,
        )
        return

    # ── Queued: say what this will cost, so the fee is never a surprise ──
    await state.set_state(ChatState.in_queue)
    cost = await match_cost(mode)
    price = (
        "رایگان" if cost <= 0 else f"<b>{fmt_coins(cost)} سکه</b> — فقط وقتی وصل شویم"
    )
    await message.answer(
        "<b>در حال جستجو</b>\n\n"
        "به‌محض پید شدن طرف مقابل وصل می‌شوید.\n\n"
        f"هزینهٔ این حالت: {price}\n"
        "در طول چت هیچ سکه‌ای کسر نمی‌شود.",
        parse_mode="HTML",
        reply_markup=queue_menu_kb(),
    )


# ──────────────────────────────────────────────────
# Chat-ended menu: «🔍 چت بعدی» / «🔁 اتصال مجدد»
# ──────────────────────────────────────────────────

#: The only FSM states a fresh search or rematch request may start from.
_FREE_STATES = (None, ChatState.idle.state)


@router.message(F.text == NEXT_CHAT_LABEL)
async def next_chat(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """«🔍 چت بعدی» — repeat the user's last search with a single tap.

    Deliberately state-unfiltered: this button lives on the chat-ended card,
    which is sent while the state is already ``idle``. The branches below then
    make a stale keyboard (queue, an active chat, an unfinished flow) answer
    honestly instead of silently dropping the tap — a button that does nothing
    is the exact failure this whole keyboard exists to remove.
    """
    user_id = message.from_user.id

    if user_id in pair_map:
        await message.answer(
            "در یک چت فعال هستید؛ اول آن را پایان دهید.",
            reply_markup=chat_menu_kb(),
        )
        return

    current = await state.get_state()
    if current == ChatState.in_queue.state:
        await message.answer("در حال جستجو هستید.", reply_markup=queue_menu_kb())
        return
    if current == ChatState.in_chat.state:
        # State says in_chat but the pair is gone — repair the view, the
        # pairing itself is already over.
        await state.set_state(ChatState.idle)
        await message.answer(
            "چت فعالی وجود ندارد.", reply_markup=chat_end_kb(user_id in last_partner)
        )
        return
    if current not in _FREE_STATES:
        # An unfinished flow (profile setup, a parked whisper, …) owns the
        # screen right now. Answer so the tap registers, but touch nothing —
        # that flow's own escape row is the way out, and swapping its keyboard
        # is how users get stranded.
        await message.answer(
            "اول کار فعلی خود را تمام کنید؛ بعد می‌توانید چت بعدی را شروع کنید."
        )
        return

    await _begin_search(message, state, fsm_storage, last_mode.get(user_id))


@router.message(F.text == REMATCH_LABEL)
async def rematch_request(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """«🔁 اتصال مجدد» — re-invite the partner recorded by the last chat end.

    Answered from ``last_partner`` rather than an inline callback: a reply
    button carries no payload, which is also why no partner id is ever printed
    on the end card.
    """
    user_id = message.from_user.id
    partner_id = last_partner.get(user_id)

    current = await state.get_state()
    if user_id in pair_map or current == ChatState.in_chat.state:
        await message.answer(
            "در یک چت فعال هستید؛ اول آن را پایان دهید.",
            reply_markup=chat_menu_kb(),
        )
        return
    if current == ChatState.in_queue.state:
        await message.answer("در حال جستجو هستید.", reply_markup=queue_menu_kb())
        return
    if current not in _FREE_STATES:
        await message.answer(
            "اول کار فعلی خود را تمام کنید؛ بعد می‌توانید اتصال مجدد را درخواست کنید."
        )
        return

    if partner_id is None:
        await message.answer(
            "طرف قبلی دیگر در دسترس نیست؛ می‌توانید «🔍 چت بعدی» را بزنید.",
            reply_markup=chat_end_kb(False),
        )
        return

    refusal = await _ask_rematch(
        user_id, partner_id, message.bot, state, message.from_user.first_name
    )
    if refusal is None:
        await message.answer(
            "درخواست اتصال مجدد برای طرف مقابل ارسال شد.\n"
            "تا وقتی پاسخ دهد می‌توانید «🔍 چت بعدی» را بزنید.",
            reply_markup=chat_end_kb(True),
        )
        return

    # ``_ask_rematch`` forgets the stored partner when the request can never
    # succeed (already declined / blocked), so this rebuild drops the dead row.
    await message.answer(refusal, reply_markup=chat_end_kb(user_id in last_partner))


def _gender_ok(
    my_mode: MatchMode,
    my_gender: str | None,
    their_mode: MatchMode,
    their_gender: str | None,
) -> bool:
    """Would these two be a legitimate pairing?

    Symmetric on purpose. Checking only the searcher's wish would let a man
    pressing «چت با دختر» be handed a man who merely pressed «چت با پسر», which
    is not what either button promised.

    A random-connect searcher (``my_mode is None``) asks for nobody in
    particular, so they accept    anyone — which is what makes «اتصال شانسی» a useful catch-all option rather
    than a second restricted one. A targeted waiter
    with an unknown gender is skipped rather than matched, because guessing would
    hand somebody the opposite of what they paid for.
    """
    my_wants = _MODE_WANTS.get(my_mode) if my_mode else None
    their_wants = _MODE_WANTS.get(their_mode) if their_mode else None
    if my_wants is not None and their_gender != my_wants:
        return False
    if their_wants is not None and my_gender != their_wants:
        return False
    return True


async def _abort_pair(
    bot: Bot,
    fsm_storage: BaseStorage,
    user_id: int,
    partner_id: int,
    reason: str,
) -> None:
    """Break a pair that must not exist, telling BOTH sides equally.

    Used when a connection fee cannot be paid. Whoever pressed the button is not
    necessarily the one who gets notified by the normal «چت پایان یافت» path, so
    the teardown cannot lean on ``_force_end``: it answers the triggering message
    and would put a stranger's reason in the user's mouth. Both parties here
    learned about the pairing from nobody, so both hear it directly.
    """
    for uid in (user_id, partner_id):
        pair_map.pop(uid, None)
        chat_opened_at.pop(uid, None)
        reset_rate_limits(uid)
    try:
        await force_set_state(fsm_storage, bot.id, user_id, ChatState.idle)
        await force_set_state(fsm_storage, bot.id, partner_id, ChatState.idle)
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the reason
        logger.warning("Could not free FSM state after a failed charge: %s", exc)

    for uid in (user_id, partner_id):
        try:
            await bot.send_message(
                uid,
                f"<b>چت برقرار نشد.</b>\n\n{reason}",
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        except Exception as exc:
            logger.warning("Could not notify %s of a failed connection: %s", uid, exc)


async def _announce_match(
    bot: Bot,
    fsm_storage: BaseStorage,
    user_id: int,
    partner_id: int,
    mode: MatchMode | None,
    partner_mode: MatchMode | None,
    *,
    charge: bool = True,
) -> None:
    """Charge the entry fee of whoever chose a paid mode, then announce the pair.

    The fee belongs to the CHOICE, not to the person who happened to press the
    button last. A user can sit in the queue for an hour after tapping «چت با
    دختر» and be connected by somebody who merely tapped «اتصال شانسی» — so the
    searcher's ``mode`` alone is not enough to decide who pays. Every party whose
    own mode was a paid one is charged, each at the rate they were quoted when
    they queued. «اتصال شانسی» is charged too — but only when the admin has
    actually priced it, which is why every party is considered and the price,
    not the label, decides who pays.

    Charges happen HERE, after the pair exists, so a user who waits and is never
    connected pays nothing. If a charge fails the pair is broken up again and
    anything already taken is handed straight back — the alternative is dropping
    somebody into a chat they cannot pay for.

    ``charge=False`` skips billing entirely — a rematch re-connects two people
    who already paid to meet, and the card honestly reports «رایگان».
    """
    charged_by: dict[int, float] = {}
    if charge:
        # Random connect is free by DEFAULT, so it is only billed when the admin
        # has set a price for it — reading ``match_cost(None)`` once keeps the
        # decision and the charge on the same number even if the policy changes
        # mid-pair.
        random_cost = await match_cost(None)
        payers = [
            (uid, m)
            for uid, m in ((user_id, mode), (partner_id, partner_mode))
            if m is not None or random_cost > 0
        ]

        for payer_id, payer_mode in payers:
            ok, amount = await charge_match_fee(payer_id, payer_mode)
            if ok:
                charged_by[payer_id] = amount
                continue

            for paid_id, paid_amount in charged_by.items():
                if paid_amount > 0:
                    await add_coins(paid_id, paid_amount, reason="refund_match")
            await _abort_pair(
                bot,
                fsm_storage,
                user_id,
                partner_id,
                "اتصال برقرار نشد؛ سکه کافی نداشتید.",
            )
            return

    policy = await get_policy()

    # The pair is real now — both sides consented to be here, which supersedes
    # any OLDER decline lockout between these two: the "no" they said was
    # about an earlier invitation, not about being in this chat. Reaching the
    # next chat end may therefore offer a rematch again.
    _rematch_declined.discard(_decline_key(user_id, partner_id))

    def fee_line(uid: int) -> str:
        amount = charged_by.get(uid, 0)
        if amount <= 0:
            return "\nهزینهٔ اتصال: رایگان"
        return (
            f"\nهزینهٔ اتصال: <b>{fmt_coins(amount)} سکه</b> (فقط همین یک بار)"
        )

    header = "<b>وصل شدید</b>\n\n"
    footer = (
        "\n\nپیام‌ها رایگان است؛ هیچ پیامی سکه کسر نمی‌کند.\n"
        f"{chat_lifetime_text(policy.chat_lifetime_hours)}\n"
        "برای پایان، «❌ لغو چت» را بزنید."
    )

    me = await get_user(user_id)
    partner = await get_user(partner_id)

    await _send_profile_with_photo(
        bot,
        user_id,
        f"{header}{_format_profile(partner) if partner else '—'}"
        f"{fee_line(user_id)}{footer}",
        chat_menu_kb(),
        partner,
    )

    await _send_profile_with_photo(
        bot,
        partner_id,
        f"{header}{_format_profile(me) if me else '—'}{fee_line(partner_id)}{footer}",
        chat_menu_kb(),
        me,
    )


# ──────────────────────────────────────────────────
# Cancel search
# ──────────────────────────────────────────────────

@router.message(F.text == "❌ لغو جستجو", ChatState.in_queue)
async def cancel_search(message: Message, state: FSMContext) -> None:
    """Leave the search queue. Nothing was charged, so nothing is refunded."""
    async with chat_lock:
        search_queue.pop(message.from_user.id, None)
    await state.set_state(ChatState.idle)
    await message.answer("جستجو لغو شد.", reply_markup=main_menu_kb())


# ──────────────────────────────────────────────────
# End active chat  (all variants)
# ──────────────────────────────────────────────────

@router.message(
    F.text.in_({"❌ لغو چت", "❌ پایان چت", "🔙 بازگشت به منو"}),
    ChatState.in_chat,
)
async def end_chat(message: Message, state: FSMContext, fsm_storage: BaseStorage) -> None:
    """End the current chat and free both users.

    Each side gets the chat-ended REPLACEMENT keyboard («🔍 چت بعدی» first,
    «🔁 اتصال مجدد» while a previous partner is known) — built inside
    ``_force_end`` so no end path can forget it.
    """
    await _force_end(
        message,
        state,
        fsm_storage,
        message.from_user.id,
        "می‌توانید چت بعدی را شروع کنید.",
    )


# ──────────────────────────────────────────────────
# Block (peer-to-peer) — with StorageKey state sync
# ──────────────────────────────────────────────────

@router.message(F.text == "🛑 گزارش کاربر / بلاک", ChatState.in_chat)
async def report_user(message: Message, state: FSMContext, fsm_storage: BaseStorage) -> None:
    """Block the chat partner (peer-to-peer) and end the chat.

    This inserts a row into BlockList — it does NOT set the global
    ``User.is_banned`` flag, which is reserved for admin-only bans.
    """
    user_id = message.from_user.id
    partner_id = pair_map.pop(user_id, None)

    if partner_id:
        pair_map.pop(partner_id, None)
        chat_opened_at.pop(user_id, None)
        chat_opened_at.pop(partner_id, None)
        reset_rate_limits(user_id)
        reset_rate_limits(partner_id)

        # Nobody may «اتصال مجدد» their way back to a blocked partner, so the
        # stored pointers die with the chat — only if they point at each other,
        # never a genuinely older partner they still may re-invite.
        if last_partner.get(user_id) == partner_id:
            _forget_pair_end(user_id)
        if last_partner.get(partner_id) == user_id:
            _forget_pair_end(partner_id)

        # Forcefully clear the peer's FSM state so they are not left stuck in
        # in_chat with nobody to talk to.
        await _force_clear_peer_state(fsm_storage, message.bot.id, partner_id)

        # Peer-to-peer block: insert into BlockList
        async with async_session_factory() as session:
            existing = await session.execute(
                select(BlockList).where(
                    (BlockList.blocker_id == user_id) & (BlockList.blocked_id == partner_id)
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(BlockList(blocker_id=user_id, blocked_id=partner_id))
            # The report itself is kept too — the PM to admins scrolls away,
            # the inbox does not.
            session.add(UserReport(reporter_id=user_id, reported_id=partner_id))
            await session.commit()

        try:
            await message.bot.send_message(
                partner_id,
                "<b>اتصال شما قطع شد.</b>\n\nبه منوی اصلی بازگشتید.",
                parse_mode="HTML",
                reply_markup=chat_end_kb(partner_id in last_partner),
            )
        except Exception as e:
            logger.warning("Failed to notify blocked user %s: %s", partner_id, e)

        # Every admin gets the report, not just the first configured id —
        # admin_ids_list[0] silently dropped the other half of the team.
        for admin_id in settings.admin_ids_list:
            try:
                await message.bot.send_message(
                    admin_id,
                    "<b>گزارش بلاک</b>\n\n"
                    f"<code>{user_id}</code> کاربر <code>{partner_id}</code> را بلاک کرد.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to notify admin %s: %s", admin_id, e)

    await state.set_state(ChatState.idle)
    await message.answer(
        "کاربر بلاک شد و چت پایان یافت.",
        reply_markup=chat_end_kb(user_id in last_partner),
    )


# ──────────────────────────────────────────────────
# Rematch — reconnect with the previous partner
# ──────────────────────────────────────────────────

def _prune_rematch_offers() -> None:
    """Drop invitations nobody answered within ``_REMATCH_TTL``."""
    now = time.monotonic()
    for pid, (_requester, offered_at) in list(rematch_offers.items()):
        if now - offered_at > _REMATCH_TTL:
            rematch_offers.pop(pid, None)


async def _rematch_partner_is_idle(
    fsm_storage: BaseStorage, bot_id: int, user_id: int
) -> bool:
    """True when the OTHER user's FSM is idle (or was never set)."""
    key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
    try:
        their_state = await fsm_storage.get_state(key)
    except Exception as exc:  # noqa: BLE001 — an unread state must not pair blindly
        logger.warning("Could not read state of %s for rematch: %s", user_id, exc)
        return False
    return their_state in (None, ChatState.idle.state)


async def _ask_rematch(
    requester: int,
    partner_id: int,
    bot: Bot,
    requester_state: FSMContext,
    requester_name: str | None,
) -> str | None:
    """Send a rematch invitation from ``requester`` to ``partner_id``.

    Returns ``None`` once the invitation card is on its way, otherwise a short
    Persian refusal for the caller to show. Shared by the «🔁 اتصال مجدد» reply
    button (target read from ``last_partner``) and the legacy inline end-card
    button (target read from the callback) so the revalidation rules — and
    above all the decline lockout — cannot drift apart between the two entry
    points.

    Side effect on a refusal that can never expire (declined / blocked): the
    stored partner is forgotten, so the caller's rebuilt keyboard drops the
    dead «اتصال مجدد» row instead of offering a button that only answers "no".
    """
    if partner_id == requester:
        return "درخواست نامعتبر است."
    if requester in pair_map or requester in search_queue:
        return "ابتدا چت فعلی خود را پایان دهید."
    if await requester_state.get_state() not in _FREE_STATES:
        return "ابتدا چت فعلی خود را پایان دهید."
    if partner_id in pair_map or partner_id in search_queue:
        return "طرف مقابل هم‌اکنون در چت یا جستجو است."
    if _decline_key(requester, partner_id) in _rematch_declined:
        _forget_pair_end(requester)
        return (
            "طرف مقابل درخواست اتصال مجدد را رد کرده است.\n"
            "می‌توانید «🔍 چت بعدی» را بزنید."
        )
    if await are_blocked(requester, partner_id):
        _forget_pair_end(requester)
        return "این اتصال ممکن نیست."

    _prune_rematch_offers()
    if partner_id in rematch_offers:
        return "یک درخواست برای این کاربر قبلاً ارسال شده است."
    their_entry = rematch_offers.get(requester)
    if their_entry is not None and their_entry[0] == partner_id:
        return "طرف مقابل هم درخواست داده؛ روی پیام او «قبول» را بزنید."

    rematch_offers[partner_id] = (requester, time.monotonic())
    try:
        await bot.send_message(
            partner_id,
            "🔁 <b>درخواست اتصال مجدد</b>\n\n"
            f"{escape(requester_name or '') or 'یک کاربر'} می‌خواهد "
            "دوباره با شما وصل شود.\n"
            "اتصال مجدد رایگان است.",
            parse_mode="HTML",
            reply_markup=rematch_offer_kb(),
        )
    except Exception as exc:
        rematch_offers.pop(partner_id, None)
        logger.warning("Rematch offer to %s failed: %s", partner_id, exc)
        return "ارسال درخواست ممکن نشد."

    return None


@router.callback_query(F.data.startswith("rematch:ask:"))
async def cb_rematch_ask(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """«🔁 اتصال مجدد» on a legacy inline end card — same helper, same rules."""
    try:
        partner_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return

    refusal = await _ask_rematch(
        callback.from_user.id,
        partner_id,
        callback.bot,
        state,
        callback.from_user.first_name,
    )
    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return

    await callback.answer("درخواست اتصال مجدد ارسال شد.", show_alert=True)
    # The invitation is live — take the button off the card so a second tap
    # cannot even try (``_ask_rematch`` would refuse on dedupe anyway, but a
    # refused tap still shows an error for something already done).
    try:
        await callback.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — a stale card has nothing left to clear
        pass


@router.callback_query(F.data == "rematch:accept")
async def cb_rematch_accept(
    callback: CallbackQuery, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Partner accepted: re-pair both users without charging again."""
    accepter = callback.from_user.id
    _prune_rematch_offers()
    entry = rematch_offers.pop(accepter, None)
    if entry is None:
        await callback.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
        return
    requester = entry[0]

    # Re-check everything: the world moved between the invitation and the tap.
    if accepter in pair_map or accepter in search_queue:
        await callback.answer(
            "شما هم‌اکنون در چت یا جستجو هستید.", show_alert=True
        )
        return
    if requester in pair_map or requester in search_queue:
        await callback.answer(
            "طرف مقابل وارد چت دیگری شده است.", show_alert=True
        )
        return
    if await state.get_state() not in (None, ChatState.idle.state):
        await callback.answer(
            "ابتدا چت فعلی خود را پایان دهید.", show_alert=True
        )
        return
    if await are_blocked(accepter, requester):
        await callback.answer("این اتصال ممکن نیست.", show_alert=True)
        return
    if not await _rematch_partner_is_idle(fsm_storage, callback.bot.id, requester):
        await callback.answer(
            "طرف مقابل در چت دیگری است.", show_alert=True
        )
        return

    # Answer FIRST: the query expires seconds after the tap, and the profile
    # cards below are worth more than a toast that may arrive too late.
    await callback.answer("وصل شدید!", show_alert=True)

    try:
        await callback.edit_message_text(
            "✅ درخواست پذیرفته شد؛ وصل شدید.", reply_markup=None
        )
    except Exception:  # noqa: BLE001 — an old card may be uneditable now
        pass

    now = time.monotonic()
    pair_map[accepter] = requester
    pair_map[requester] = accepter
    chat_opened_at[accepter] = now
    chat_opened_at[requester] = now
    _remember_pair_end(accepter, requester)

    await state.set_state(ChatState.in_chat)
    await force_set_state(fsm_storage, callback.bot.id, requester, ChatState.in_chat)

    await _announce_match(
        callback.bot, fsm_storage, accepter, requester, None, None, charge=False
    )


@router.callback_query(F.data == "rematch:decline")
async def cb_rematch_decline(callback: CallbackQuery) -> None:
    """Partner declined: lock the pair out of further requests, tell the asker.

    The lockout is the point. The old handler only deleted the offer, so the
    requester could tap «اتصال مجدد» again and turn a "no" into a nag — exactly
    the loop this refuses to be part of. Both directions are locked, and the
    decliner's own stored partner is forgotten so their stale end card cannot
    re-send the request either.
    """
    decliner = callback.from_user.id
    _prune_rematch_offers()
    entry = rematch_offers.pop(decliner, None)
    if entry is None:
        await callback.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
        return
    requester = entry[0]

    await callback.answer("درخواست رد شد.", show_alert=True)

    _rematch_declined.add(_decline_key(requester, decliner))
    _forget_pair_end(decliner)

    try:
        await callback.edit_message_text("درخواست رد شد.", reply_markup=None)
    except Exception:  # noqa: BLE001 — an old card may already be edited
        pass

    # Notify the requester with NO reply keyboard on purpose: they may be
    # queueing or chatting by now, and replacing another flow's keyboard from
    # a background message is how users end up staring at the wrong menu. If
    # their «🔁 اتصال مجدد» row is still visible, the next press answers with
    # the refusal text and rebuilds the keyboard without that row.
    try:
        await callback.bot.send_message(
            requester,
            "درخواست اتصال مجدد شما پذیرفته نشد.\n"
            "می‌توانید «🔍 چت بعدی» را بزنید.",
        )
    except Exception as exc:
        logger.warning("Could not notify %s of rematch decline: %s", requester, exc)


# ──────────────────────────────────────────────────
# Message forwarding (text, voice, photo — LIVE only)
# ──────────────────────────────────────────────────

async def _forward_to_partner(user_id: int, bot, send_func) -> bool:
    """Send content to the partner if the user is currently paired."""
    partner_id = pair_map.get(user_id)
    if not partner_id:
        return False
    try:
        await send_func(partner_id)
        return True
    except Exception as e:
        logger.warning("Failed to forward to %s: %s", partner_id, e)
        return False


async def _enforce_chat_auth(
    message: Message,
    state: FSMContext,
    fsm_storage: BaseStorage,
) -> bool:
    """The gate every relayed message passes. Returns ``True`` to send it.

    Two checks, in this order:

    1. **Has this chat outlived its 24 hours?** Checked before anything else and
       acted on immediately, because an expired chat must not even rate-limit —
       the user needs to be told it is over, not that they are typing too fast.
       The clock is only consulted, never extended, so a chat in genuine use
       still ends once it crosses the line; the guarantee the UI promises ("this
       chat lasts at most N hours") has to be true for the person reading it.
    2. **Anti-flood rate limit.** Drops the message and warns. Never charges —
       the price of a chat was taken once, at connection.
    """
    user_id = message.from_user.id

    policy = await get_policy()

    if _expired(user_id, await _lifetime_seconds()):
        await _force_end(
            message,
            state,
            fsm_storage,
            user_id,
            f"مدت مجاز چت ({max(policy.chat_lifetime_hours, 1)} ساعت) "
            "به پایان رسید.",
        )
        return False

    auth = await authorize_chat_message(user_id)
    if auth.allowed:
        return True

    if should_warn(user_id):
        await message.answer(
            rate_limit_text(auth.wait, policy.messages_per_minute)
        )
    return False


@router.message(ChatState.in_chat, F.text)
async def forward_text(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Relay text instantly — never saved to DB."""
    if not await _enforce_chat_auth(message, state, fsm_storage):
        return
    ok = await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_message(
            pid, message.text, parse_mode=None
        ),
    )
    if not ok and message.from_user.id not in pair_map:
        await message.answer("چت فعالی وجود ندارد.", reply_markup=main_menu_kb())


@router.message(ChatState.in_chat, F.voice)
async def forward_voice(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Relay voice messages instantly."""
    if not await _enforce_chat_auth(message, state, fsm_storage):
        return
    await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_voice(pid, message.voice.file_id),
    )


@router.message(ChatState.in_chat, F.photo)
async def forward_photo(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Relay photos instantly (largest size)."""
    if not await _enforce_chat_auth(message, state, fsm_storage):
        return
    await _forward_to_partner(
        message.from_user.id,
        message.bot,
        lambda pid: message.bot.send_photo(pid, message.photo[-1].file_id),
    )


@router.message(ChatState.in_chat, F.video | F.video_note | F.animation)
async def block_video(message: Message) -> None:
    """Strictly forbid videos / video notes / animations."""
    await message.answer("ارسال فیلم مجاز نیست.")


@router.message(ChatState.in_chat)
async def unsupported_media(
    message: Message, state: FSMContext, fsm_storage: BaseStorage
) -> None:
    """Catch-all for any other content type during chat."""
    if not await _enforce_chat_auth(message, state, fsm_storage):
        return
    await message.answer("این نوع محتوا پشتیبانی نمی‌شود.")
