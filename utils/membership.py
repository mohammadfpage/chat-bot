"""
Reusable Telegram membership checks.

The same question — "is this user actually inside that channel/group?" — is
asked in three different places (the force-join middleware, the whisper send
flow and the whisper view flow). It lives here once so the rule stays
consistent, and so the result is cached for a few seconds instead of hitting
the Bot API on every single update.

A user counts as a member when the status is one of
``creator / administrator / member``; ``restricted`` only counts when the
``is_member`` flag is set (Telegram sets it for users still inside a chat
that hit a content restriction).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from html import escape

from config import settings
from database import RequiredChannel, async_session_factory
from sqlalchemy import select
from utils.whisper_config import get_whisper_config

logger = logging.getLogger(__name__)

#: Statuses that always mean "inside the chat".
MEMBER_STATUSES = ("creator", "administrator", "member")
#: Statuses that always mean "not inside the chat".
NON_MEMBER_STATUSES = ("left", "kicked")

#: How long a membership answer is trusted before asking Telegram again.
CACHE_TTL = 30.0

_cache: dict[tuple[str, int], tuple[bool, float]] = {}

#: Channels already told to the log as unverifiable, so a dead channel warns
#: once instead of once per update (which would drown the log on every run).
#: An entry is dropped as soon as one lookup for that channel succeeds again,
#: so a *fixed* channel gets a fresh warning only if it breaks a second time.
_warned_channels: set[str] = set()


@dataclass(frozen=True)
class ChatRef:
    """A chat a user may be required to join (a channel or a group)."""

    ref: int | str
    title: str
    username: str = ""
    source: str = "config"
    #: An invite URL the admin supplied verbatim. Private channels and paid
    #: invite links are not derivable from a username, so when one is given it
    #: wins over the computed fallback in :attr:`url`.
    link: str = ""

    @property
    def clean_username(self) -> str:
        return (self.username or "").strip().lstrip("@")

    @property
    def url(self) -> str:
        """Join link, also for private chats."""
        if self.link:
            return self.link
        if self.clean_username:
            return f"https://t.me/{self.clean_username}"
        if isinstance(self.ref, int) and str(self.ref).startswith("-100"):
            # Internal id -> the t.me/c/<short id> form for private chats.
            return f"https://t.me/c/{str(-self.ref - 1000000000000)}"
        return ""

    @property
    def api_ref(self) -> int | str:
        """The value the Bot API accepts for ``get_chat_member``."""
        if isinstance(self.ref, int) and self.ref:
            return self.ref
        return f"@{self.clean_username}"


def join_bullet(chat: ChatRef) -> str:
    """One bullet line of a join prompt: title PLUS the channel's public ID.

    The ID is the copyable fallback for a tap that never arrives — some
    clients choke on the URL button (flaky VPN, previews disabled), while a
    bare ``@handle`` in the message text can be long-pressed and copied into
    Telegram's own search, so the button is never the only way in. When the
    title already IS the handle (a leading ``@`` plus the same username —
    the ``.env``/legacy fallback writes exactly that) the handle is linked
    once instead of repeated, and a private invite link gets the same
    treatment under a short label; a derived ``t.me/c/…`` link is
    deliberately left off because it resolves nowhere outside its own chat.
    """
    title = escape(chat.title)
    username = chat.clean_username
    if username:
        href = escape(f"https://t.me/{username}")
        handle = escape(f"@{username}")
        stripped = chat.title.strip()
        if stripped.startswith("@") and stripped[1:].casefold() == username.casefold():
            return f"• <a href=\"{href}\">{handle}</a>"
        return f"• {title} — <a href=\"{href}\">{handle}</a>"
    link = (chat.link or "").strip()
    if link.startswith(("http://", "https://")) and (
        "joinchat" in link or link.rsplit("/", 1)[-1].startswith("+")
    ):
        return f"• {title} — <a href=\"{escape(link)}\">لینک عضویت</a>"
    return f"• {title}"


def clear_membership_cache() -> None:
    """Drop every cached membership answer (after a successful join)."""
    _cache.clear()


async def get_chat_member_status(bot, chat_ref: int | str, user_id: int) -> str | None:
    """Return the raw member status, or ``None`` when the lookup failed."""
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
    except Exception as exc:
        logger.debug("get_chat_member failed for %s/%s: %s", chat_ref, user_id, exc)
        return None
    return member.status


async def is_member(
    bot,
    chat_ref: int | str,
    user_id: int,
    *,
    force_refresh: bool = False,
) -> bool:
    """Return True when ``user_id`` is currently inside ``chat_ref``.

    **Fail-open on an unverifiable lookup.** When ``get_chat_member`` raises
    (network hiccup, the channel id was changed, the bot was kicked out of
    the channel), the answer is "could not check" — not "not a member".
    Treating unknown as *no* locks every user out of the whole bot the moment
    one channel misbehaves, with no error anywhere a user can see. Treating it
    as *yes* degrades force-join for the seconds the outage lasts, which costs
    the admin nothing they can undo. The admin still learns about it from the
    once-per-channel WARNING below instead of from a flood of user reports.

    Args:
        force_refresh: skip the TTL cache and ask Telegram right now. Used by
            the "عضو شدم" button so a user who just joined is not stuck.
    """
    key = (str(chat_ref), user_id)
    now = time.monotonic()

    if not force_refresh:
        cached = _cache.get(key)
        if cached and (now - cached[1]) < CACHE_TTL:
            return cached[0]

    status = await get_chat_member_status(bot, chat_ref, user_id)
    if status is None:
        chat_key = str(chat_ref)
        if chat_key not in _warned_channels:
            _warned_channels.add(chat_key)
            logger.warning(
                "Could not verify membership in %s — failing OPEN (users are "
                "let through) until a lookup succeeds. Check that the bot is "
                "an admin there and that the channel id/username is correct.",
                chat_ref,
            )
        _cache[key] = (True, now)
        return True

    _warned_channels.discard(str(chat_ref))
    ok = status in MEMBER_STATUSES
    _cache[key] = (ok, now)
    return ok


async def force_sub_enabled() -> bool:
    """The master switch for the forced-join requirement.

    Named after the spec's ``FORCE_SUB_ENABLED``; it is the existing
    ``whisper_config.require_join`` flag, which the admin panel already
    toggles. When this is False every call site must skip membership checks
    entirely rather than iterate an empty list and "pass" by accident.
    """
    config = await get_whisper_config()
    return bool(config.require_join)


async def configured_channels() -> list[ChatRef]:
    """Every channel the admin listed in the ``required_channels`` table."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(RequiredChannel)
                .where(RequiredChannel.is_active.is_(True))
                .order_by(RequiredChannel.position, RequiredChannel.id)
            )
            rows = result.scalars().all()
    except Exception as exc:
        # A missing/unmigrated table must not take the whole bot down; the
        # legacy + env channels below still apply.
        logger.warning("Could not read required_channels: %s", exc)
        return []

    return [
        ChatRef(
            ref=row.chat_id,
            title=row.title or (f"@{row.username}" if row.username else str(row.chat_id)),
            username=row.username or "",
            source="table",
            link=row.link or "",
        )
        for row in rows
    ]


async def required_chats() -> list[ChatRef]:
    """Chats a user must be inside before the bot serves them.

    Unions three sources into ONE de-duplicated ordered list:

      1. the single chat configured in the admin panel before multi-channel
         support existed (``whisper_config.required_chat_*``),
      2. every active row of the ``required_channels`` table,
      3. the global channel from ``.env`` (``CHANNEL_ID`` / ``CHANNEL_USERNAME``).

    Keeping the legacy field in the union is deliberate: without it an admin
    who had already configured a channel would find it silently dropped the
    moment this shipped.
    """
    refs: list[ChatRef] = []
    seen: set[str] = set()

    def add(ref: ChatRef) -> None:
        # De-duplicate on the value get_chat_member is actually called with,
        # so ``-100123`` and ``@mychannel`` for the same chat are matched by
        # the numeric id rather than counted twice.
        key = str(ref.api_ref)
        if key in seen:
            return
        seen.add(key)
        refs.append(ref)

    config = await get_whisper_config()
    if config.require_join and config.required_chat_id:
        username = (config.required_chat_username or "").strip()
        add(
            ChatRef(
                ref=config.required_chat_id,
                title=config.required_chat_title
                or (f"@{username}" if username else "کانال ربات"),
                username=username,
                source="admin",
            )
        )

    for chat in await configured_channels():
        add(chat)

    channel_username = (settings.channel_username or "").strip().lstrip("@")
    channel_id = settings.channel_id
    if channel_id:
        add(
            ChatRef(
                ref=channel_id,
                title=f"@{channel_username}" if channel_username else "کانال ربات",
                username=channel_username,
                source="env",
            )
        )
    elif channel_username:
        add(
            ChatRef(
                ref=f"@{channel_username}",
                title=f"@{channel_username}",
                username=channel_username,
                source="env",
            )
        )

    return refs


async def missing_required_chats(
    bot, user_id: int, *, force_refresh: bool = False
) -> list[ChatRef]:
    """Return the required chats ``user_id`` has NOT joined yet.

    Returns an EMPTY list when the admin has switched the requirement off,
    even though ``required_chats()`` still resolves channels in that state.
    Gating here — at the one function every call site funnels through — is
    what makes "Force-Sub disabled ⇒ skip the channel checks entirely"
    true by construction instead of something each caller has to remember.
    """
    if not await force_sub_enabled():
        return []

    missing: list[ChatRef] = []
    for chat in await required_chats():
        if not await is_member(
            bot, chat.api_ref, user_id, force_refresh=force_refresh
        ):
            missing.append(chat)
    return missing


async def resolve_chat_ref(
    bot, raw: str
) -> tuple[int | None, str, str]:
    """Turn admin input into ``(chat_id, username, title)``.

    Accepts a numeric id (``-100123...``), a ``@username`` or a ``t.me/...``
    invite link, and resolves the human title through ``bot.get_chat``.
    """
    text = (raw or "").strip()
    if not text:
        return None, "", ""

    if text.startswith(("http://", "https://", "t.me/")):
        text = text.rstrip("/").split("/")[-1]
        text = text.split("?")[0].lstrip("@")
    else:
        text = text.lstrip("@")

    if not text:
        return None, "", ""

    chat_ref: int | str = int(text) if text.lstrip("-").isdigit() else f"@{text}"

    try:
        chat = await bot.get_chat(chat_ref)
    except Exception as exc:
        logger.warning("Cannot resolve chat %s: %s", chat_ref, exc)
        return None, text.lstrip("@"), ""

    username = (chat.username or "").strip()
    title = chat.title or (f"@{username}" if username else str(chat.id))
    return chat.id, username, title


__all__ = [
    "ChatRef",
    "MEMBER_STATUSES",
    "NON_MEMBER_STATUSES",
    "CACHE_TTL",
    "clear_membership_cache",
    "configured_channels",
    "force_sub_enabled",
    "get_chat_member_status",
    "is_member",
    "join_bullet",
    "required_chats",
    "missing_required_chats",
    "resolve_chat_ref",
]
