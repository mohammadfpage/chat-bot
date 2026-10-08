"""
Reading the bot's own rights inside a group, and asking for them politely.

Telegram gives a bot three very different levels of presence in a chat, and the
group handler needs to tell them apart:

    member        can read what it can see and post, but is listed among the
                  ordinary participants and cannot call
                  ``getChatAdministrators`` — which is what fills the whisper
                  recipient roster with the group's admins.
    administrator can call ``getChatAdministrators``, so whisper recipients who
                  run the group become resolvable the moment it is promoted.
    anonymous     an administrator whose posts are signed «Group Anonymous Bot».
                  Nothing functionally changes for us; it only hides the bot
                  from the members list of the group.

The last two are the same status, so :func:`classify` is what separates them:
Telegram reports ``administrator`` plus an ``is_anonymous`` flag, and that flag
is the only difference the group owner can act on.

Degradation: ``getChatAdministrators`` is itself an administrator-only call. A bot
that was added as a plain member cannot enumerate who runs the group, so
:func:`notify_admins` returns the admins it found (possibly none) and the caller
also posts the same request in the group itself — the visible, working path.
"""

from __future__ import annotations

import logging
from html import escape

from aiogram.types import Chat, ChatMember, ChatMemberAdministrator, ChatMemberOwner, User

from utils.emojis import get_plain_emoji

logger = logging.getLogger(__name__)

#: How present the bot is. Named after the three states the group owner can pick.
STATE_MEMBER = "member"
STATE_ADMIN = "admin"
STATE_ANONYMOUS = "anonymous"
STATE_RESTRICTED = "restricted"

#: Statuses in which the bot is inside the chat at all.
INSIDE_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})


def classify(member: ChatMember | None) -> str:
    """Which of the three presences does ``member`` represent?"""
    if member is None:
        return STATE_RESTRICTED
    if member.status == "administrator" and isinstance(
        member, ChatMemberAdministrator
    ):
        return STATE_ANONYMOUS if member.is_anonymous else STATE_ADMIN
    if member.status in ("administrator", "creator"):
        return STATE_ADMIN
    if member.status in INSIDE_STATUSES:
        return STATE_MEMBER
    return STATE_RESTRICTED


def is_admin_state(state: str) -> bool:
    """True for both administrator flavours (signed and anonymous)."""
    return state in (STATE_ADMIN, STATE_ANONYMOUS)


def chat_link(chat: Chat | None) -> str:
    """A t.me URL an admin can press to get back into the group.

    Public groups have a username; private ones are reachable through the
    ``/c/`` short id. Returns ``""`` when neither works — a button that cannot
    be pressed is worse than no button, so the caller skips it in that case.
    """
    if chat is None:
        return ""
    if chat.username:
        return f"https://t.me/{chat.username}"
    raw = str(chat.id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}"
    return ""


def promote_text(chat: Chat | None) -> str:
    """The body of the "please promote me" notice, from the chat itself.

    The rights the bot actually needs are named rather than left vague: an
    admin who promotes without deleting messages still gets a bot whose card
    the group cannot remove afterwards.
    """
    title = escape(chat.title or "گروه") if chat else "گروه"
    return (
        f"{get_plain_emoji('warning')} <b>برای فعال شدن کامل ربات، مدیر گروه "
        f"«{title}» باید ربات را ادمین کند.</b>\n\n"
        "ادمین شدن یعنی:\n"
        "• ربات بتواند اعضای گروه را بخواند (برای ارسال نجوا به اعضا)\n"
        "• بتواند پیام‌های خودش را حذف کند\n\n"
        "👤 مسیر: مدیر گروه ← مدیریت اعضا ← ربات ← «ادمین»"
    )


def anonymous_admin_text(chat: Chat | None) -> str:
    """The nudge towards anonymous admin status.

    Purely cosmetic from our side: nothing changes functionally. It is worth
    asking for anyway because an ordinary administrator is a visible member of
    the group, and most group owners consider a messaging bot a distraction they
    never opted into.
    """
    title = escape(chat.title or "گروه") if chat else "گروه"
    return (
        f"{get_plain_emoji('info')} <b>پیشنهاد:</b> در «{title}» گزینهٔ "
        "«ادمین ناشناس» را برای ربات فعال کنید.\n\n"
        "در این حالت ربات در فهرست مدیران گروه دیده نمی‌شود و پیام‌هایش با "
        "نام «ربات گروه» ارسال می‌شود."
    )


async def notify_admins(
    bot,
    chat: Chat,
    text: str,
    *,
    also: User | None = None,
) -> int:
    """DM ``text`` to the humans who can act on it. Returns how many got it.

    ``also`` is the person who performed the membership change — the admin who
    added or promoted the bot. It matters because
    ``getChatAdministrators`` is itself an **administrator-only** call, and the
    one moment we most want to say "please promote me" is the moment the bot is
    *not* an administrator yet: the listing comes back refused and the request
    reaches nobody but the group. The adder, on the other hand, is an admin by
    definition of having added a bot, so they are always worth a DM even when the
    listing is unavailable. (The DM still needs them to have started the bot at
    some point — Telegram will not let a bot open a fresh conversation.)

    Best-effort by design: this runs inside a ``my_chat_member`` handler, and a
    failing notification must never abort the membership bookkeeping. The owner
    is always included by the listing — they are the only person who can actually
    promote beyond what an administrator may grant.
    """
    recipients: list[tuple[int, str]] = []
    seen: set[int] = set()

    try:
        members = await bot.get_chat_administrators(chat.id)
    except Exception as exc:
        # The normal case for the "please promote me" notice, because the bot is
        # by definition not yet an administrator when it is sent. Logged at INFO
        # rather than DEBUG: it used to vanish, and the visible symptom was a
        # group whose owner never received anything in their DMs.
        logger.info(
            "Cannot list admins of %s (bot is not an administrator?): %s",
            chat.id,
            exc,
        )
        members = []

    for member in members:
        # Bots cannot be talked to, and Telegram rejects a DM to another bot
        # outright — skipping them is not an optimisation.
        if member.user.is_bot:
            continue
        if member.user.id in seen:
            continue
        seen.add(member.user.id)
        recipients.append((member.user.id, member.status))

    if also is not None and not also.is_bot and also.id not in seen:
        seen.add(also.id)
        recipients.append((also.id, "the admin who added the bot"))

    link = chat_link(chat)
    delivered = 0
    for user_id, who in recipients:
        try:
            await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception as exc:
            logger.info("Admin notice to %s failed (%s): %s", user_id, who, exc)

    logger.debug(
        "Admin notice for %s: %d/%d delivered (group link: %s)",
        chat.id,
        delivered,
        len(recipients),
        link or "unavailable",
    )
    return delivered


def is_human_owner(member: ChatMember | None) -> bool:
    """True for the group's owner (``creator``), who can always promote.

    Only the owner can grant rights that an administrator cannot grant back, so
    a "please promote me" that only reaches admins can stall forever.
    """
    return isinstance(member, ChatMemberOwner)


async def bot_is_admin(bot, chat_id: int) -> bool:
    """True when the bot holds admin rights in ``chat_id``.

    Load-bearing beyond cosmetics: ``getChatMember`` is only *authoritative for
    other members* when the asking bot is an administrator. A plain member that
    asks about a third party may be handed a wrong answer, so every caller that
    would act on that answer has to know which case it is in. Answering the
    question about the bot's OWN status is the one lookup Telegram always
    answers truthfully.
    """
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except Exception as exc:
        logger.debug("Cannot read our own status in %s: %s", chat_id, exc)
        return False
    return classify(me) in (STATE_ADMIN, STATE_ANONYMOUS)


__all__ = [
    "INSIDE_STATUSES",
    "STATE_ADMIN",
    "STATE_ANONYMOUS",
    "STATE_MEMBER",
    "STATE_RESTRICTED",
    "anonymous_admin_text",
    "bot_is_admin",
    "chat_link",
    "classify",
    "is_admin_state",
    "is_human_owner",
    "notify_admins",
    "promote_text",
]