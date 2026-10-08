"""
Resolving a whisper recipient, in one place.

The problem this solves
-----------------------
Two entry points resolve a recipient — ``/نجوا`` in :mod:`handlers.whisper` and
the inline picker in :mod:`handlers.inline_anon` — and they used to answer the
same question differently:

* the inline picker asked the in-memory roster and, **only if the roster was
  completely empty**, fell back to the ``users`` table;
* ``/نجوا`` asked the ``users`` table and never the roster at all.

An either/or between two incomplete sources is not a fallback, it is a coin
toss. A person in the group who never started the bot was invisible to the first
and invisible to the second, and the answer they got — «گیرنده در گروه‌های ربات
پیدا نشد» — blamed their *group membership*, which was not the thing that had
failed. This module consults every source and reports which one answered.

Sources, and what each one actually proves
------------------------------------------
============================  ==========================================  ============
Source                        Proves                                      Cost
============================  ==========================================  ============
:class:`utils.group_members`  the person is inside a group the bot is in     none
``users`` table               the person has an open chat with the bot       1 query
``bot.get_chat``              the bot can currently address them             1 API call
============================  ==========================================  ============

Both first two are needed, and the third is what makes a *miss* explainable:

* the roster is how the feature stays a group feature — it is fed only by group
  traffic, so somebody who never shared a chat with the sender is not in it;
* the ``users`` table is what makes the send **possible**. Telegram refuses to
  let a bot open a conversation with a stranger ("bot can't initiate
  conversation with a user"), so a person who has never started the bot can be
  *known* and still *undeliverable*. Consulting the table is therefore not a
  loosening of the rule but the actual delivery precondition.

``get_chat`` is asked last and only on a miss, because it is the only source
that can settle the question the user is really asking about a *handle* — "does
this username even exist, and can the bot reach them?" — and because a wrong
handle produces the same 400 as a valid handle whose owner never started the bot.
That ambiguity is inherent to the Bot API, so :data:`UNKNOWN_HINT` names both
causes instead of pretending to know which one it was.

A miss on a *numeric* id is not a miss at all
--------------------------------------------
Every source above answers the same question — *does the bot have a relationship
with this person?* — and reading "no" as "this person does not exist" is the
mistake this module exists to undo. The whisper is revealed through
``callback.answer(show_alert=True)``, a modal only the tapper's own client
renders, so the recipient never needs to have started the bot and a missing
relationship blocks nothing. An unrecognised number is therefore handed on as a
:attr:`TargetLookup.provisional` hit and checked for real by
:func:`recipient_verdict` at the landing point, where ``getChatMember`` can
answer authoritatively.

Cost control
------------
Resolution runs on the inline picker's typing path, so a miss must not turn into
one ``get_chat`` per keystroke. Misses are memoised negatively for
:data:`_MISS_TTL` seconds; hits from the roster cost nothing at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy import func, select

from database import User, async_session_factory
from utils.group_members import find_member, is_member_of_chat

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Outcomes
# ────────────────────────────────────────────────────────────

#: Nothing was typed.
MISS_EMPTY = "empty"

#: Not a numeric id and not an ``@handle`` — a shape problem, so the syntax is
#: worth re-printing.
MISS_MALFORMED = "malformed"

#: Well-formed, but no source knows this person. :data:`UNKNOWN_HINT` spells out
#: the two real causes because the Bot API cannot tell them apart.
MISS_UNKNOWN = "unknown"


#: What the previous picker was wrong to say. «گیرنده در گروه‌های ربات پیدا نشد»
#: blamed group membership, which is not what was checked and not what failed.
#:
#: Reaching this at all now means one thing only: an ``@username`` nobody has ever
#: mentioned. A numeric id is never refused here — see :data:`TargetLookup.provisional`.
UNKNOWN_HINT = (
    "این یوزرنیم در هیچ گروهی که ربات در آن است و در فهرست ربات‌ها نیست.\n\n"
    "راه درست: آیدی‌عددی طرف را بنویسید، یا پیامش را ریپلای کنید، یا از او "
    "بخواهید یک‌بار ربات را استارت کند."
)


@dataclass(frozen=True, slots=True)
class TargetLookup:
    """Outcome of :func:`resolve_target`.

    ``miss`` is empty on success and otherwise one of the ``MISS_*`` constants;
    the caller never has to interpret ``user_id == 0``.

    ``provisional`` marks the one case that is a hit on thin evidence: a numeric
    id that no source recognises. See :func:`resolve_target` for why that is
    still a hit, and :func:`recipient_verdict` for how it gets checked properly.
    """

    user_id: int = 0
    first_name: str = ""
    username: str = ""
    miss: str = ""
    provisional: bool = False

    @property
    def found(self) -> bool:
        return self.miss == ""


# ────────────────────────────────────────────────────────────
# Memo
# ────────────────────────────────────────────────────────────

#: How long a failed ``get_chat`` is believed for. Long enough that holding the
#: picker open and retyping costs one call, short enough that somebody who was
#: just added to a group resolves without a restart.
_MISS_TTL = 90.0

#: Bound on remembered misses. The picker can be used in any chat by any user, so
#: this is keyed by token and has to be evictable rather than permanent.
_MISS_CACHE_MAX = 512

_miss_cache: dict[str, float] = {}


def _remember_miss(token: str) -> None:
    if len(_miss_cache) >= _MISS_CACHE_MAX:
        _miss_cache.clear()
    _miss_cache[token] = time.monotonic()


def _miss_is_fresh(token: str) -> bool:
    at = _miss_cache.get(token)
    if at is None:
        return False
    if (time.monotonic() - at) >= _MISS_TTL:
        _miss_cache.pop(token, None)
        return False
    return True


def clear_miss_cache() -> None:
    """Forget every memoised miss (tests, and the ``/fix_keyboard`` escape)."""
    _miss_cache.clear()


# ────────────────────────────────────────────────────────────
# Resolution
# ────────────────────────────────────────────────────────────


def shape_of(token: str) -> str:
    """``""`` when ``token`` is a usable reference, else the miss that applies.

    Split out so the two callers can validate before spending anything: the
    inline picker runs this on every keystroke, and a shape error is the common
    case while a person is halfway through typing a handle.
    """
    raw = (token or "").strip()
    if not raw:
        return MISS_EMPTY
    if raw.startswith("@"):
        return "" if len(raw) > 1 else MISS_MALFORMED
    return "" if raw.lstrip("-").isdigit() else MISS_MALFORMED


async def _from_users_table(token: str) -> TargetLookup | None:
    """The person who has an open chat with the bot.

    This is the delivery precondition: Telegram will not let the bot open a
    conversation with somebody who never started it, so a row here is what makes
    a whisper deliverable at all.
    """
    async with async_session_factory() as session:
        if token.startswith("@"):
            result = await session.execute(
                select(User).where(func.lower(User.username) == token[1:].lower())
            )
        else:
            result = await session.execute(
                select(User).where(User.telegram_id == int(token))
            )
        row = result.scalars().first()

    if row is None:
        return None
    return TargetLookup(
        user_id=row.telegram_id,
        first_name=row.first_name or "",
        username=row.username or "",
    )


async def has_started_bot(user_id: int) -> bool:
    """Has this person ever opened a chat with the bot?

    The ``users`` table is the answer, because a row is only written once a
    private conversation exists — on ``/start``, or the first time an
    economy/balance path registers somebody who never pressed it.

    This exists because the whisper senders used to learn the answer by
    *sending the recipient a private message* and seeing whether it arrived.
    That was the wrong way to ask: it put a «you have an anonymous message»
    alert in every recipient's chat purely as a side effect of a delivery
    probe, and it burned an API call per whisper to learn something the
    database already holds. The group card is the whole delivery now, so the
    question is asked here instead, for free.

    Returns True on any database trouble: the caller's use is advisory (it
    decides whether to *append a warning*), so guessing "yes" keeps a working
    whisper free of a scary caveat, and never blocks a send.
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(User.telegram_id).where(User.telegram_id == user_id)
            )
            return result.scalar_one_or_none() is not None
    except Exception as exc:  # noqa: BLE001 — advisory signal only
        logger.debug("has_started_bot(%s) failed: %s", user_id, exc)
        return True


async def _from_live_chat(bot: Bot | None, token: str) -> TargetLookup | None:
    """Ask Telegram directly whether this reference resolves to a person.

    The only source that can be authoritative about a *username*, and the only
    one that survives the ``users`` table being out of date (a handle changed, a
    row was written before the rename). Costs an API call, so it runs last and
    only after the cheap sources have already said no.
    """
    if bot is None:
        return None

    try:
        chat = await bot.get_chat(token)
    except Exception as exc:
        # Overwhelmingly "chat not found": either the handle does not exist or
        # its owner never started the bot. The Bot API does not distinguish the
        # two, which is exactly why UNKNOWN_HINT names both.
        logger.debug("Target lookup: get_chat(%s) failed: %s", token, exc)
        return None

    # ``get_chat`` also accepts channel usernames and group handles. A whisper
    # to a chat is not a thing, so anything that is not a person is refused.
    if chat.type != "private":
        logger.debug("Target lookup: %s is a %s, not a person", token, chat.type)
        return None

    return TargetLookup(
        user_id=chat.id,
        first_name=getattr(chat, "first_name", "") or "",
        username=getattr(chat, "username", "") or "",
    )


async def resolve_target(bot: Bot | None, token: str) -> TargetLookup:
    """Resolve ``123456789`` or ``@someone`` to a deliverable whisper target.

    Consults every source rather than choosing between two incomplete ones, and
    returns the first hit. Never raises: a lookup runs on the inline picker's
    typing path, where an exception would take down the whole update.

    A numeric id that nothing recognises is still a **hit**
    (:attr:`TargetLookup.provisional`), and that is the fix for «I typed the
    right id and the bot is an admin in the group, why can it not send?».

    It used to be a hard refusal, because every source here asks the same
    question — *does the bot have a relationship with this person?* — and
    answering no was read as *this person does not exist*. Those are not the
    same thing, and conflating them is what made the bot look broken:

    * the whisper is revealed through ``callback.answer(show_alert=True)``, a
      modal that only the tapper's own client renders. **The recipient never has
      to have started the bot**, so "no relationship" is not a delivery blocker;
    * the card still lands in the group, so the check that actually matters is
      whether the recipient is in *that* group — and ``getChatMember`` at the
      landing point answers it, but only for the people Telegram shows us (see
      :func:`recipient_verdict` for what happens when it shows us nothing);
    * every registry here is incomplete by construction (the Bot API has no
      listing for ordinary group members), so a miss is far more often an
      incomplete index than a wrong number.

    An ``@username`` is the one case that still has to be refused: without a
    relationship there is no way to turn a handle into a user id, and a card
    addressed to nothing cannot be delivered or verified. Say so, and point at
    the numeric id rather than at group membership.
    """
    bad = shape_of(token)
    if bad:
        return TargetLookup(miss=bad)

    raw = token.strip()

    # 1. In-memory roster: free, and the reason the feature stays group-scoped.
    profile = find_member(raw)
    if profile is not None:
        return TargetLookup(
            user_id=profile.user_id,
            first_name=profile.first_name,
            username=profile.username,
        )

    # 2. Durable roster: survives a restart, so a target picked yesterday still
    # resolves today. Costs a query, which is why the free index above goes first.
    from utils.roster_store import lookup as _roster_lookup

    stored = await _roster_lookup(raw)
    if stored is not None:
        return TargetLookup(
            user_id=stored["user_id"],
            first_name=stored["first_name"],
            username=stored["username"],
        )

    # 3. Has an open chat with the bot, so the whisper can actually arrive.
    from_table = await _from_users_table(raw)
    if from_table is not None:
        return from_table

    # 4. Telegram itself. Memoised, because the picker asks again per keystroke.
    if not _miss_is_fresh(raw):
        live = await _from_live_chat(bot, raw)
        if live is not None:
            return live
        _remember_miss(raw)

    # 5. A number is already the whole address. Hand it over and let
    #    :func:`recipient_verdict` settle whether such a person is really here.
    if not raw.startswith("@"):
        return TargetLookup(user_id=int(raw), provisional=True)

    return TargetLookup(miss=MISS_UNKNOWN)


# ────────────────────────────────────────────────────────────
# The check that actually matters: is the recipient in the group
# the card lands in?
# ────────────────────────────────────────────────────────────

#: The recipient is in the landing group; the send proceeds.
VERDICT_OK = "ok"

#: Telegram authoritatively says they are not. This is the message the user
#: asked for: «not in THIS group».
VERDICT_NOT_HERE = "not_here"

#: Telegram could not find such a user at all, so the number is wrong.
#:
#: No longer emitted by :func:`recipient_verdict`, and deliberately so: a typo and
#: a real member who never started the bot produce the identical "user not found",
#: so any check strong enough to catch the typo also refuses the member. Kept
#: because the senders still branch on it, which keeps a future signal that *can*
#: tell them apart from needing a second pass over both call sites.
VERDICT_BAD_ID = "bad_id"


async def _roster_has_member(chat_id: int, target_id: int) -> bool:
    """Ask the durable roster, importing it lazily to keep this module light.

    ``utils.roster_store`` reaches for the database; keeping it out of the import
    graph here is what lets :mod:`utils.target_lookup` be imported on its own.
    """
    from utils.roster_store import is_member_of

    return await is_member_of(chat_id, target_id)


async def recipient_verdict(bot: Bot | None, chat_id: int, target_id: int) -> str:
    """May a whisper card addressed to ``target_id`` be placed in ``chat_id``?

    This is the gate that lets an unverified id through the picker without
    letting a wrong one cost the sender anything. It is asked at the landing
    point — where the chat id is finally known — and it runs BEFORE the economy
    gate charges the sender, so a refusal is free.

    A status from Telegram settles it outright: present means ``OK``, and an
    explicit ``left``/``kicked`` means ``NOT_HERE``. The interesting part is what
    a *failed* lookup means, and the old answer here was wrong in a way that
    made the feature look broken.

    It used to read: "the bot is an administrator, so ``getChatMember`` is
    authoritative, therefore a failure means no such user." But Telegram does not
    show a bot the users who never started it — **administrators included**.
    ``getChatMember`` answers "user not found" for a member who is standing right
    there in the group, and that is indistinguishable from a typo. So the gate
    refused exactly the people it was built to serve, and the sender was told the
    number was bad while the recipient was in the room. This was the «یکی رو پیدا
    کرد ولی نتونست نجوا بده» report.

    What Telegram can actually tell us is split by whether it is *able* to see the
    person at all, and that is what the fallback below keys on:

    * we have seen them inside **this** group — positive evidence, so a failed
      lookup can only mean "hidden", and the send proceeds;
    * they have an open chat with the bot, so Telegram answers about them
      authoritatively, and a failure is a real "not in this group";
    * neither — a number we have never seen, for a person Telegram is hiding. The
      number may be right, and there is no way to find out, so the send proceeds:
      a card in a group is cheap to lose, while refusing a legitimate whisper
      makes the feature unusable for everyone who has not tapped /start.
    """
    if bot is None:
        return VERDICT_OK

    # Imported here rather than at module scope: utils.membership reaches for the
    # database, so keeping it out of this module's import graph lets it be
    # imported on its own.
    from utils.membership import NON_MEMBER_STATUSES, get_chat_member_status

    status = await get_chat_member_status(bot, chat_id, target_id)
    if status is not None:
        return VERDICT_OK if status not in NON_MEMBER_STATUSES else VERDICT_NOT_HERE

    # The registry only ever holds what we watched arrive or were listed as an
    # admin, so a hit is real evidence about *this* group and a miss is silence.
    if is_member_of_chat(chat_id, target_id):
        logger.info(
            "%s is a known member of %s and Telegram hid them (never started "
            "the bot) — allowing the send.",
            target_id,
            chat_id,
        )
        return VERDICT_OK

    # Same question, asked of the durable roster. Worth a query precisely when
    # the check above misses: the in-memory index is empty right after a restart,
    # which is exactly when an ordinary member would otherwise be refused. A miss
    # here still proves nothing, so it falls through rather than refusing.
    if await _roster_has_member(chat_id, target_id):
        logger.info(
            "%s is in the stored roster of %s (seen before this process started) "
            "— allowing the send.",
            target_id,
            chat_id,
        )
        return VERDICT_OK

    if await has_started_bot(target_id):
        # Visible to us, yet absent here: Telegram can answer this properly, so
        # believe it — but "wrong group", not "wrong number". The id is real.
        logger.info(
            "%s started the bot but is not in %s — refusing as not_here.",
            target_id,
            chat_id,
        )
        return VERDICT_NOT_HERE

    # Never seen by anything we know of, and Telegram will not look. A wrong
    # refusal is the worse error, so let it through.
    logger.info(
        "%s is invisible to us in %s (no roster entry, never started the bot) "
        "and a failed lookup cannot tell a typo from a hidden member — allowing.",
        target_id,
        chat_id,
    )
    return VERDICT_OK


__all__ = [
    "MISS_EMPTY",
    "MISS_MALFORMED",
    "MISS_UNKNOWN",
    "UNKNOWN_HINT",
    "TargetLookup",
    "VERDICT_BAD_ID",
    "VERDICT_NOT_HERE",
    "VERDICT_OK",
    "clear_miss_cache",
    "has_started_bot",
    "recipient_verdict",
    "resolve_target",
    "shape_of",
]