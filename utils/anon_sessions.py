"""
Storage for real-time anonymous 1-on-1 chat sessions.

The «درخواست پیام ناشناس» card is only a *request*. This module owns the
lifecycle that turns it into a live, two-way session:

    pending ──(requester accepts)──▶ active ──(either side ends)──▶ ended
       │
       └──(requester declines)────▶ declined

Why a database row and not a dictionary
---------------------------------------
The obvious implementation of ``active_anon_sessions = {a: b, b: a}`` is an
in-process dict. That dict dies with the process: long-polling bots get
restarted, and every restart would silently drop live sessions while leaving
both users believing they are still connected — the worst possible failure for a
chat feature. The rows live in the same SQLite/Postgres database as everything
else, so a restart costs nothing and the pair survives it.

Both directions of one conversation are found through the SAME row because every
lookup matches either column (``requester_id`` OR ``partner_id``) instead of
picking one. That is the whole of "one session per pair": a user cannot be in
two live conversations with the same person, because the second request finds
the first row and refuses to create another.

Roles are stored as they happen
------------------------------
``requester_id`` really is the user who published the card and ``partner_id``
really is the one who pressed the button. The tempting shortcut — sorting the
two ids so the pair has a canonical shape — is a trap: the «قبول چت» button is
only ever shown to the publisher, so if sorting can move the publisher into
``partner_id`` for half of all id pairs, then for those pairs NOBODY can accept
their own request. Roles are not derivable from the ids, so they are stored.

Expiry
-------
An ``active`` row older than :data:`SESSION_MAX_LIFETIME_HOURS` is treated as gone
on read. Without it, a session abandoned by one side would keep swallowing that
user's private messages (the router forwards everything while a session is
live) and forward them into the void. Ending is always explicit too; this is
only the backstop for the case where nobody ever pressed the button.

The window is a hard cap from ``activated_at``, not an idle timeout. It used to
slide — :func:`touch` rewrote the anchor on every relayed message — which meant
the busier a pair was, the longer it could stay open. Activity now buys no extra
time at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from database import AnonChatSession, User, async_session_factory

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# Statuses
# ──────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DECLINED = "declined"
STATUS_ENDED = "ended"

#: Statuses that still "own" a pair: a new request against a pair that has
#: neither declined nor closed yet is a duplicate, not a new conversation.
OPEN_STATUSES = (STATUS_PENDING, STATUS_ACTIVE)

#: Hard cap on how long one accepted chat may live, in hours, measured from
#: ``activated_at``. It does not slide with activity: a conversation still
#: talking at hour 23 ends at hour 24.
SESSION_MAX_LIFETIME_HOURS = 24

#: Safe status values accepted by :func:`close`. Kept as a set so a typo in a
#: call site can never write a nonsense status into the table.
_ENDABLE = frozenset({STATUS_PENDING, STATUS_ACTIVE})


def _either(user_id: int) -> or_:
    """``requester_id == uid OR partner_id == uid`` — "this pair includes uid".

    Every lookup in this module goes through here. Writing the two-column match
    out by hand is how the two directions of a conversation drift apart: a
    filter on one column alone silently returns nothing for half of all pairs.
    """
    return or_(
        AnonChatSession.requester_id == user_id,
        AnonChatSession.partner_id == user_id,
    )


async def _load_pair(
    first: int,
    second: int,
    statuses: tuple[str, ...],
) -> AnonChatSession | None:
    """The newest row for this unordered pair, in any of ``statuses``."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonChatSession)
            .where(
                _either(first),
                _either(second),
                AnonChatSession.status.in_(statuses),
            )
            .order_by(AnonChatSession.id.desc())
        )
        return result.scalars().first()


def _is_stale(row: AnonChatSession) -> bool:
    """True when a session has outlived :data:`SESSION_MAX_LIFETIME_HOURS`."""
    started = row.activated_at or row.created_at
    if started is None:
        return False
    return datetime.utcnow() - started > timedelta(
        hours=SESSION_MAX_LIFETIME_HOURS
    )


# ──────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────

async def has_started(user_id: int) -> bool:
    """True when this user has pressed ``/start`` at least once.

    A bot may not message a user who never started it, so the «شروع چت ناشناس»
    flow has to know this BEFORE it accepts anything. The ``users`` table is the
    record of exactly that: ``handlers/start.py`` inserts a row on the first
    ``/start`` and never deletes it.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(User.id).where(User.telegram_id == user_id)
        )
        return result.scalar_one_or_none() is not None


async def active_session(user_id: int) -> AnonChatSession | None:
    """``user_id``'s live session row, or ``None``.

    One query answers every question the relay needs — is there a session and who is
    the other side — so the handler does not have to go back to the database a
    second time per message.
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(AnonChatSession)
                .where(
                    AnonChatSession.status == STATUS_ACTIVE,
                    _either(user_id),
                )
                .order_by(AnonChatSession.id.desc())
            )
            row = result.scalars().first()
    except Exception as exc:
        # A session lookup must never take the router down: without it the user
        # simply gets the normal bot behaviour, which is a far better outcome
        # than a dispatch-level crash on every private message.
        logger.warning("Could not read the anon session of %s: %s", user_id, exc)
        return None

    if row is None or _is_stale(row):
        return None
    return row


def partner_of(row: AnonChatSession, user_id: int) -> int:
    """The other side of ``row`` as seen from ``user_id``.

    The pair is stored canonically, so which column ``user_id`` sits in is the
    only thing that decides the answer — there is no third possibility to guard
    against, and no need to trust the caller's bookkeeping.
    """
    return row.partner_id if row.requester_id == user_id else row.requester_id


async def active_partner(user_id: int) -> int | None:
    """The other side of ``user_id``'s live session, or ``None``.

    This is the whole of ``active_anon_sessions`` as far as the rest of the bot
    is concerned: both directions resolve through the same row, so a session
    started by either side behaves identically.
    """
    row = await active_session(user_id)
    return partner_of(row, user_id) if row is not None else None


async def live_partners(*user_ids: int) -> dict[int, int]:
    """``{user_id: partner_id}`` for those of ``user_ids`` already chatting.

    Both sides have to be checked before a request is opened, not just the
    person who pressed the button. A pair-scoped check — "is THIS pair already
    open?" — answers the wrong question: it is perfectly possible for the card's
    publisher to be mid-conversation with somebody else while a second person
    presses their card, and accepting that request would drop them into two live
    chats at once. From then on ``active_session`` can only ever return one of
    them, so half the conversation would silently vanish.
    """
    busy: dict[int, int] = {}
    for user_id in dict.fromkeys(user_ids):
        if not user_id:
            continue
        partner = await active_partner(user_id)
        if partner is not None:
            busy[user_id] = partner
    return busy


async def pending_request_for(user_id: int) -> AnonChatSession | None:
    """The newest open request this user is a side of, if any.

    Matched on BOTH columns on purpose — see :func:`_either`. A card published
    by user A carries only ``start_anon_chat:<A>``, and a filter on
    ``requester_id`` alone finds nothing whenever A is the side stored as
    ``partner_id``, i.e. whenever B's id happens to be the larger one.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonChatSession)
            .where(
                _either(user_id),
                AnonChatSession.status == STATUS_PENDING,
            )
            .order_by(AnonChatSession.id.desc())
        )
        return result.scalars().first()


async def by_token(token: str) -> AnonChatSession | None:
    """Resolve the token carried by the «قبول چت» / «رد» buttons."""
    if not token:
        return None
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonChatSession).where(AnonChatSession.token == token)
        )
        return result.scalars().first()


# ──────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────

async def open_request(requester_id: int, partner_id: int) -> AnonChatSession:
    """Record "``partner_id`` wants to answer ``requester_id``'s card".

    The roles are stored exactly as they are: ``requester_id`` is whoever
    published the card, because the «قبول چت» button is rendered only in that
    user's DM and the accept handler authorises on this column.

    Idempotent on the OPEN pair: a card can be pressed by several people, and
    the same person can press it twice. An existing pending/active row for the
    pair is returned untouched, so no duplicate «قبول چت» buttons pile up.

    A closed pair gets a NEW row rather than a revived one. Reopening the old
    row would resurrect its token — and every «قبول چت» button ever sent out
    still carries that token, so a stale press months later would drop the user
    straight into a live chat they had already ended.

    Returns the row; the caller re-reads ``status`` to find out whether the
    acceptance still has to be asked for.
    """
    if requester_id == partner_id:
        raise ValueError("a user cannot request an anonymous chat with itself")

    existing = await _load_pair(requester_id, partner_id, OPEN_STATUSES)
    if existing is not None:
        return existing

    token = uuid_token()

    async with async_session_factory() as session:
        row = AnonChatSession(
            token=token,
            requester_id=requester_id,
            partner_id=partner_id,
            status=STATUS_PENDING,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    logger.info(
        "Anon chat request opened: %s -> %s (%s)", requester_id, partner_id, token
    )
    return row


async def activate(row: AnonChatSession) -> bool:
    """Flip a pending request to ``active``. False when it was already closed.

    The status is re-checked inside the transaction so two taps of «قبول چت»
    racing each other cannot both "win": the loser gets ``False`` and is told
    the chat already ended, instead of resurrecting it. That only works because
    ``active`` is NOT an acceptable source state here — accepting is a
    ``pending → active`` transition, and nothing else.

    The pair is checked for a live session as well. ``open_request`` normally
    makes a second live row impossible, but it reads-then-writes and two card
    presses in opposite directions can interleave; refusing here costs nothing
    and keeps the invariant "one live conversation per pair" true even then.
    """
    async with async_session_factory() as session:
        current = await session.scalar(
            select(AnonChatSession).where(AnonChatSession.id == row.id)
        )
        if current is None or current.status != STATUS_PENDING:
            return False

        clash = await session.scalar(
            select(AnonChatSession)
            .where(
                AnonChatSession.id != row.id,
                AnonChatSession.status == STATUS_ACTIVE,
                _either(current.requester_id),
                _either(current.partner_id),
            )
            .limit(1)
        )
        if clash is not None:
            logger.info(
                "Refusing to activate anon session %s: the pair is already live "
                "in session %s",
                row.id,
                clash.id,
            )
            return False

        current.status = STATUS_ACTIVE
        current.activated_at = datetime.utcnow()
        current.ended_at = None
        await session.commit()
        row.status = STATUS_ACTIVE
        row.activated_at = current.activated_at
    return True


async def close(row: AnonChatSession, status: str) -> bool:
    """Move a request/session to ``declined`` or ``ended``.

    Returns ``True`` only for the caller that actually performed the transition,
    so the "the chat has ended" notice is sent once rather than twice.
    """
    if status not in (STATUS_DECLINED, STATUS_ENDED):
        raise ValueError(f"not a closing status: {status}")

    async with async_session_factory() as session:
        current = await session.scalar(
            select(AnonChatSession).where(AnonChatSession.id == row.id)
        )
        if current is None or current.status not in _ENDABLE:
            return False
        current.status = status
        current.ended_at = datetime.utcnow()
        await session.commit()
    row.status = status
    return True


async def end_for_user(user_id: int) -> int | None:
    """End ``user_id``'s live session. Returns the other side, or ``None``."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(AnonChatSession)
            .where(
                AnonChatSession.status == STATUS_ACTIVE,
                _either(user_id),
            )
            .order_by(AnonChatSession.id.desc())
        )
        row = result.scalars().first()
        if row is None:
            return None
        other = partner_of(row, user_id)
        row.status = STATUS_ENDED
        row.ended_at = datetime.utcnow()
        await session.commit()
    logger.info("Anon chat ended by %s (partner %s)", user_id, other)
    return other


async def touch(session_id: int) -> None:
    """Kept for API compatibility — deliberately does NOT extend a session.

    This used to rewrite ``activated_at`` on every relayed message, which made
    the 24-hour cap slide: the more two people talked, the further the deadline
    moved, so a busy pair never expired at all. A chat has a fixed lifetime from
    the moment it is accepted, so activity must not buy more time.

    ``activated_at`` is the single expiry anchor for both :func:`_is_stale` and
    the pending-request age check; refreshing it here is the one thing that
    breaks both. Nothing needs to record "last activity" — that column does not
    exist, and adding one would only invite a second, wrong anchor.
    """
    return None


def uuid_token() -> str:
    """A 16-hex-char token for ``callback_data`` (``anon_chat:accept:<token>``).

    Half of :func:`uuid.uuid4`'s hex is plenty here: the row is only reachable
    through a button Telegram hands to exactly one private chat, and 16 chars
    keeps the full callback comfortably inside the 64-byte ceiling.
    """
    return uuid4().hex[:16]


__all__ = [
    "OPEN_STATUSES",
    "SESSION_MAX_LIFETIME_HOURS",
    "STATUS_ACTIVE",
    "STATUS_DECLINED",
    "STATUS_ENDED",
    "STATUS_PENDING",
    "active_partner",
    "active_session",
    "activate",
    "by_token",
    "close",
    "end_for_user",
    "has_started",
    "live_partners",
    "open_request",
    "partner_of",
    "pending_request_for",
    "touch",
]