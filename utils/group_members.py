"""
In-memory registry of the members of every group the bot lives in.

Why this exists
---------------
Resolving the recipient of an inline whisper used to mean "look in the
``users`` table" — i.e. "has this person ever started the bot". That is both
the wrong question (it whitelists strangers who never shared a group with the
sender) and an expensive one: the lookup ran on **every keystroke** of the
inline picker, so an admin typing a whisper generated a burst of database
round-trips for what is a single dictionary lookup.

The answer Telegram itself gives is membership, and it is delivered to us for
free:

* ``chat_member`` updates fire whenever somebody joins or leaves a group the
  bot is an **administrator** of, and
* every group message proves its sender is inside that group.

Both feed the flat, bounded, in-process maps below. Resolution is therefore a
dict hit — no session, no SQL, no await — which is what makes the picker cheap
no matter how often an admin searches.

Bounds and freshness
--------------------
Memory is capped on three axes (max chats, max members, max entry age) and
swept lazily, so a bot in very large groups cannot grow without limit. Telegram
pushes membership changes, so entries stay correct while the bot is an admin;
the age cap only discards what nobody has confirmed in a long time — an
``@username`` or id that falls out of the registry is reported as unknown
rather than guessed.

A bot that is not an administrator anywhere receives no ``chat_member`` updates
at all, and the Bot API has no listing for ordinary members either (see
:mod:`utils.roster_sync`), so this registry can never be complete on its own.
:func:`utils.target_lookup.resolve_target` therefore consults it *first* — it is
the cheapest source and the one that keeps the feature group-scoped — and falls
through to the ``users`` table and to Telegram itself rather than treating a miss
here as a refusal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────
# Bounds
# ────────────────────────────────────────────────────────────

#: How many groups we track at once. The least-recently-touched chat is dropped
#: past this, so a bot that gets removed from hundreds of groups does not keep
#: their rosters forever.
MAX_CHATS = 400

#: Hard ceiling on remembered people. Well above any realistic group overlap
#: and far below the point where a dict of this size costs anything.
MAX_MEMBERS = 120_000

#: Entries nobody has confirmed in this long are dropped on the next sweep. Not
#: a correctness requirement (membership changes arrive as updates) — it exists
#: so a person who left months ago cannot linger as a valid target forever.
ENTRY_MAX_AGE = 14 * 24 * 3600.0

#: Statuses that mean "not in the group any more".
GONE_STATUSES = frozenset({"left", "kicked"})

#: Telegram's own sentinel account: it stands in for an anonymous admin, so its
#: presence says nothing about a real person being in the chat.
ANONYMOUS_ADMIN_ID = 1087968824

# ────────────────────────────────────────────────────────────
# Storage
# ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MemberProfile:
    """What we know about one remembered member."""

    user_id: int
    first_name: str
    username: str
    seen_at: float

    @property
    def label(self) -> str:
        return self.username or self.first_name or str(self.user_id)


#: ``user_id`` → profile. A person in three groups appears once; the inline
#: flow only ever needs "is this id in a group the bot can see".
_by_id: dict[int, MemberProfile] = {}

#: lowercased ``username`` (without ``@``) → ``user_id``. Rebuilt whenever the
#: entry it points at is refreshed, so a name change can never strand the row.
_by_username: dict[str, int] = {}

#: ``chat_id`` → the user ids seen inside it. Kept so evicting one stale group
#: removes exactly its own roster and leaves people who are also in another
#: tracked group alone.
_chat_members: dict[int, set[int]] = {}

#: ``chat_id`` → last touch, kept oldest-first so eviction is a plain pop.
_chats: dict[int, float] = {}

#: Writes since the last sweep, so the sweep itself stays amortised.
_writes_since_sweep = 0


def _now() -> float:
    return time.monotonic()


def _detach(user_id: int) -> None:
    """Remove one person from every index that mentions them."""
    profile = _by_id.pop(user_id, None)
    if profile and profile.username and _by_username.get(profile.username) == user_id:
        _by_username.pop(profile.username, None)


#: How many groups one sweep may retire. Each retirement costs a pass over
#: every roster still tracked, so an unbounded ``while`` under the members cap
#: would turn one oversized sweep into quadratic work on the hot path. Anything
#: left over is retired by the next sweep instead — the cap is still enforced,
#: just amortised across writes rather than inside a single call.
_SWEEP_CHAT_BUDGET = 32


def _sweep() -> None:
    """Drop stale / excess entries. Only walks what is already in memory."""
    cutoff = _now() - ENTRY_MAX_AGE
    for user_id in [u for u, p in _by_id.items() if p.seen_at < cutoff]:
        _detach(user_id)

    # Oldest groups go first, and a chat past the cap is forgotten entirely.
    budget = _SWEEP_CHAT_BUDGET
    while budget > 0 and _chats and (len(_chats) > MAX_CHATS or len(_by_id) > MAX_MEMBERS):
        budget -= 1
        forget_chat(next(iter(_chats)))


# ────────────────────────────────────────────────────────────
# Writing
# ────────────────────────────────────────────────────────────


def remember_member(
    user_id: int,
    first_name: str = "",
    username: str = "",
    *,
    chat_id: int | None = None,
) -> None:
    """Record (or refresh) one member. Never raises, never awaits.

    ``chat_id`` should be passed whenever the event carries a chat: it is what
    lets the registry evict one stale group's roster precisely instead of
    dropping arbitrary people.
    """
    global _writes_since_sweep

    if not user_id or user_id == ANONYMOUS_ADMIN_ID:
        return

    name = (first_name or "").strip()[:64]
    handle = (username or "").strip().lstrip("@").lower()[:64]

    profile = _by_id.get(user_id)
    if profile is None:
        profile = MemberProfile(user_id, "", "", 0.0)
        _by_id[user_id] = profile
    elif profile.username and profile.username != handle:
        if _by_username.get(profile.username) == user_id:
            _by_username.pop(profile.username, None)

    # Keep whatever we already knew: an update that carries no name must never
    # blank out a good one.
    profile.first_name = name or profile.first_name
    profile.username = handle or profile.username
    profile.seen_at = _now()

    if profile.username:
        _by_username[profile.username] = user_id

    if chat_id is not None:
        _chats.pop(chat_id, None)
        _chats[chat_id] = _now()
        _chat_members.setdefault(chat_id, set()).add(user_id)
        # Queue the durable copy. A plain set insert, no await: this function is
        # called from the outer middleware on every group message, and the flush
        # happens later on a background task (see utils.roster_store).
        from utils.roster_store import note_member

        note_member(user_id, chat_id, profile.username, profile.first_name)

    _writes_since_sweep += 1
    # Amortised: one full pass per thousand writes, never per member event.
    if _writes_since_sweep >= 1000 or len(_by_id) > MAX_MEMBERS:
        _writes_since_sweep = 0
        _sweep()


def forget_member(user_id: int) -> None:
    """Drop a person Telegram told us has left the group."""
    _detach(user_id)


def forget_chat(chat_id: int) -> None:
    """Forget a whole group roster — it aged out, or the bot was removed."""
    _chats.pop(chat_id, None)
    members = _chat_members.pop(chat_id, set())
    still_tracked = {
        uid for other, roster in _chat_members.items() if other != chat_id
        for uid in roster
    }
    for user_id in members:
        if user_id not in still_tracked:
            _detach(user_id)


def remember_chat_removed(chat_id: int) -> None:
    """Alias kept for the ``my_chat_member`` handler's vocabulary."""
    forget_chat(chat_id)


# ────────────────────────────────────────────────────────────
# Reading — pure dict hits, no I/O, no await
# ────────────────────────────────────────────────────────────


def find_member(token: str) -> Optional[MemberProfile]:
    """Resolve ``123456789`` or ``@someone`` to a remembered member.

    Returns ``None`` for anything that is not a person we have actually seen
    inside a group the bot can see.
    """
    raw = (token or "").strip()
    if not raw:
        return None

    if raw.startswith("@") and len(raw) > 1:
        user_id = _by_username.get(raw[1:].lower())
        return _by_id.get(user_id) if user_id is not None else None

    if raw.lstrip("-").isdigit():
        return _by_id.get(int(raw))
    return None


def is_known_member(user_id: int) -> bool:
    """True when this person is inside at least one group the bot can see."""
    return user_id in _by_id


def is_member_of_chat(chat_id: int, user_id: int) -> bool:
    """True when this person was seen inside *this* group.

    Narrower than :func:`is_known_member`, which only asks "somewhere the bot can
    hear" — a claim that is useless for the question "may a card about them land
    in group X".

    This is the registry's one piece of *positive* evidence about membership, and
    it exists because Telegram has no other: ``getChatMember`` cannot see a user
    who never started the bot, and no API lists ordinary members. So a hit here
    means we watched them arrive or were listed as an admin, which is stronger
    than anything a failed lookup implies.

    A miss proves nothing — the Bot API caps the roster at admins and whoever
    happened to move while we were listening. Callers must treat it as unknown.
    """
    return user_id in _chat_members.get(chat_id, ())


def member_label(user_id: int) -> str:
    """Best-effort display name; ``""`` when we never saw this person."""
    profile = _by_id.get(user_id)
    return profile.label if profile else ""


def registry_ready() -> bool:
    """True once we have seen anybody at all.

    ``False`` means the bot is not an administrator in any group it can hear,
    so no ``chat_member`` update ever arrived and the roster cannot be trusted
    to be complete. Callers use it to decide whether falling back to a looser
    source beats refusing every whisper outright.
    """
    return bool(_by_id)


def stats() -> dict[str, int]:
    """Debug counters for the admin panel / logs."""
    return {
        "members": len(_by_id),
        "usernames": len(_by_username),
        "chats": len(_chats),
    }


__all__ = [
    "ANONYMOUS_ADMIN_ID",
    "ENTRY_MAX_AGE",
    "GONE_STATUSES",
    "MAX_CHATS",
    "MAX_MEMBERS",
    "MemberProfile",
    "find_member",
    "forget_chat",
    "forget_member",
    "is_known_member",
    "is_member_of_chat",
    "member_label",
    "remember_chat_removed",
    "remember_member",
    "registry_ready",
    "stats",
]
