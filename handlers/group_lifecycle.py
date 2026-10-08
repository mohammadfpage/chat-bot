"""
Group lifecycle: onboarding, rights, and roster housekeeping.

Three jobs, all driven by ``my_chat_member``:

**Onboarding.** When the bot lands in a group it posts ONE card that names the
feature, hands the reader a link to the tutorial and asks for whatever rights
are still missing — welcome and rights request in a single message, because
two near-identical cards in a chat that has not asked for either is a wall,
not an introduction. The card is posted from the group itself rather than only
in the DM of whoever added the bot: a bot cannot DM someone who never started
it, and the person who reads the card is the whole group.

**Rights.** Telegram offers three levels of presence — plain ``member``,
``administrator``, and ``administrator`` with ``is_anonymous`` — and each one
deserves a different answer, so the handler asks for exactly what is missing:
promotion when the bot cannot list members, anonymity when the bot is a visible
administrator (purely cosmetic, and most group owners do not want a bot listed
in their admins). On a join the ask travels inside the onboarding card; the
DMs to the admins go out separately either way — including a direct DM to the
admin who performed the change, because ``getChatAdministrators`` refuses a
bot that is not yet an administrator, which is exactly the state the notice is
sent from. On a demotion there is nothing to welcome anybody to, so the request
is posted on its own.

**Housekeeping.** Telegram caches a reply keyboard per chat, and re-adding the
bot does NOT flush it — so the same one-shot
:func:`~middleware.group_keyboard_watch.spawn_group_keyboard_clear` is scheduled
on every transition into a status that can post. Becoming an administrator is
also the first moment ``getChatAdministrators`` works, which is what fills the
whisper recipient roster with the group's admins, so the roster sync rides the
same update.

This router also owns ``chat_member``: that is the authoritative feed for the
in-memory group roster in :mod:`utils.group_members`, which is what the inline
whisper picker resolves its recipient against. Registering the handler here is
also what makes aiogram include ``chat_member`` in the update types it asks
Telegram for.

Nothing in here may raise into the dispatcher: a group that could not be greeted
must still be able to use the whisper picker.
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, Router
from aiogram.types import Chat, ChatMember, ChatMemberUpdated, User

from keyboards import (
    ONBOARDING_ANONYMOUS,
    ONBOARDING_PROMOTE,
    group_admin_link,
    group_hidden_admin_kb,
    group_onboarding_kb,
    group_promote_kb,
)
from middleware.group_keyboard_watch import spawn_group_keyboard_clear
from utils.chat_types import is_group
from utils.group_admin import (
    INSIDE_STATUSES,
    STATE_ADMIN,
    STATE_ANONYMOUS,
    STATE_MEMBER,
    anonymous_admin_text,
    chat_link,
    classify,
    notify_admins,
    promote_text,
)
from utils.group_members import (
    GONE_STATUSES,
    forget_chat,
    forget_member,
    remember_member,
)
from utils.roster_sync import spawn_chat_roster_sync

logger = logging.getLogger(__name__)
router = Router()

#: Statuses in which the bot can actually post (and therefore clear a
#: keyboard). ``restricted``/``left``/``kicked`` cannot send, so no attempt
#: is made — the first message after a re-invite will trigger the watch
#: middleware instead.
_ACTIVE_STATUSES = frozenset({"member", "administrator"})


# ──────────────────────────────────────────────────────────
# Onboarding texts
# ──────────────────────────────────────────────────────────


def welcome_text(chat: Chat | None, bot_username: str) -> str:
    """The card posted the moment the bot arrives in a group.

    Written to be read once by somebody who has never seen the bot: what it does
    in one line, how to learn it, and nothing else. The bot's own @username is in
    it because that string is the entire interface — everything else in the bot
    is triggered by typing it in a chat.
    """
    title = escape(chat.title or "این گروه") if chat else "این گروه"
    handle = escape(f"@{bot_username}" if bot_username else "@YourBot")
    return (
        f"👋 <b>ربات نجوا به «{title}» اضافه شد.</b>\n\n"
        f"🆔 <code>{handle}</code>\n\n"
        "🔐 هر عضو می‌تواند یک پیام خصوصی («نجوا») برای عضو دیگری بفرستد؛ متن آن "
        "فقط برای گیرنده باز می‌شود.\n\n"
        "💡 برای یادگیری، دکمهٔ زیر را بزن."
    )


# ──────────────────────────────────────────────────────────
# Onboarding steps — each one best-effort and independently failable
# ──────────────────────────────────────────────────────────


async def _send(
    bot: Bot,
    chat: Chat,
    text: str,
    reply_markup=None,
) -> bool:
    """Post one card into a group. ``False`` when the bot may not speak there.

    A bot that was added with "post messages" switched off cannot say anything,
    and a muted chat would raise. Neither is worth an exception: onboarding is a
    courtesy, and failing it must not cost the group its whisper roster.
    """
    try:
        await bot.send_message(
            chat.id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return True
    except Exception as exc:
        logger.info("Group card could not be posted in %s: %s", chat.id, exc)
        return False


async def _post_onboarding(
    bot: Bot,
    chat: Chat,
    *,
    state: str,
    previous: ChatMember | None,
    adder: User | None,
) -> None:
    """The ONE card a group gets when the bot arrives.

    Welcome and rights request used to be two separate messages, so adding the
    bot always dumped a pair of near-identical cards into a chat that had not
    asked for either. They are the same conversation — "here is what I do, and
    here is what I need from you" — so they are one message now, with the
    rights button as a row of the same keyboard.

    The DMs to the group's admins are still sent separately: they are a
    different chat, and they are the only path that reaches an owner who is
    not watching the group.
    """
    try:
        me = await bot.me()
    except Exception as exc:
        logger.info("Could not read our own profile for the welcome card: %s", exc)
        return

    username = me.username or ""

    # Nothing to ask when the bot was already given the rights it needs, and
    # nothing to ask for promotion when it was added as an admin already.
    ask = ""
    ask_text = ""
    if state == STATE_MEMBER:
        ask, ask_text = ONBOARDING_PROMOTE, promote_text(chat)
    elif state == STATE_ADMIN and classify(previous) != STATE_ANONYMOUS:
        ask, ask_text = ONBOARDING_ANONYMOUS, anonymous_admin_text(chat)

    text = welcome_text(chat, username)
    if ask_text:
        text = f"{text}\n\n{ask_text}"
    await _send(
        bot,
        chat,
        text,
        group_onboarding_kb(username, ask=ask, chat_url=chat_link(chat)),
    )

    if ask == ONBOARDING_PROMOTE:
        await _ask_for_promotion(bot, chat, adder, post_in_group=False)
    elif ask == ONBOARDING_ANONYMOUS:
        await _suggest_anonymous_admin(bot, chat, adder, post_in_group=False)


async def _ask_for_promotion(
    bot: Bot, chat: Chat, adder: User | None = None, *, post_in_group: bool = True
) -> None:
    """«Please make me an administrator» — in the group and in the admins' DMs.

    The in-group card is posted only when the request has not already gone out
    as part of the single onboarding card (``post_in_group=False`` after a
    join, where :func:`_post_onboarding` carries it). Its button is a deep link
    that opens the rights sheet with this bot already selected, so the admin
    confirms instead of navigating. That is as close to automatic as the Bot
    API gets: ``promoteChatMember`` requires the caller to be an
    administrator, so a bot cannot promote itself, no matter what rights it
    holds.

    The DMs are best-effort and take ``adder`` — the admin whose action triggered
    this — because ``getChatAdministrators`` refuses a bot that is not yet an
    administrator, which is precisely the state we are asking to be moved out of.
    Without that fallback the request reached nobody outside the group, and a
    group owner who is not watching it never found out.
    """
    link = chat_link(chat)
    username = ""
    try:
        me = await bot.me()
        username = me.username or ""
    except Exception as exc:
        # Without a username there is no deep link, and the card degrades to the
        # text notice plus the group link in the DMs. Never fatal.
        logger.info("Could not read our own profile for the promotion card: %s", exc)

    posted = False
    if post_in_group:
        posted = await _send(bot, chat, promote_text(chat), group_promote_kb(username))
    if post_in_group and not posted:
        logger.info(
            "Cannot ask %s for promotion in-chat; relying on the admin DMs.", chat.id
        )

    admin_link = group_admin_link(username)
    dm_text = promote_text(chat) + (
        f"\n\n🔗 <a href=\"{admin_link or link}\">"
        + ("ارتقای ربات به ادمین" if admin_link else "ورود به گروه")
        + "</a>"
        if (admin_link or link)
        else ""
    )
    delivered = await notify_admins(bot, chat, dm_text, also=adder)
    logger.info(
        "Promotion request for %s: in-chat=%s, admins notified=%d",
        chat.id,
        posted,
        delivered,
    )


async def _suggest_anonymous_admin(
    bot: Bot, chat: Chat, adder: User | None = None, *, post_in_group: bool = True
) -> None:
    """Nudge towards ``is_anonymous``.

    Functionally a no-op for us — but a visible bot in the admins list is the
    thing group owners complain about most, and they will only ever find the
    setting if somebody points at it once. Skipped entirely when the bot is
    already anonymous, which is the only way this handler can be reached twice.

    ``post_in_group=False`` after a join: the nudge already travelled as a row
    of the onboarding card, and only the DMs are outstanding.
    """
    link = chat_link(chat)
    if post_in_group:
        await _send(bot, chat, anonymous_admin_text(chat), group_hidden_admin_kb(link))

    dm_text = anonymous_admin_text(chat) + (
        f"\n\n🔗 <a href=\"{link}\">ورود به گروه</a>" if link else ""
    )
    await notify_admins(bot, chat, dm_text, also=adder)


@router.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated) -> None:
    """React to every change of the bot's own rights in a group.

    Fires for each ``my_chat_member`` update (added, promoted, demoted, removed).
    Only the transitions that need a word are acted on:

    =============================================  ===========================
    transition                                    reaction
    =============================================  ===========================
    left/kicked → member|administrator             welcome + rights request
    administrator → member (demoted)              promotion request
    administrator(is_anonymous) → administrator   anonymous-admin suggestion
    =============================================  ===========================

    Everything is best-effort: membership bookkeeping must never raise into the
    dispatcher.
    """
    if not is_group(update.chat.type):
        return

    bot = update.bot
    if bot is None:  # context not bound — nothing can be sent anyway
        return

    member = update.new_chat_member
    # In a my_chat_member update the subject is the bot itself, but being
    # explicit keeps this correct even if Telegram ever widens the update.
    if member.user.id != bot.id:
        return

    if member.status not in _ACTIVE_STATUSES:
        return

    chat = update.chat
    previous = update.old_chat_member
    state = classify(member)

    logger.debug(
        "Group lifecycle: bot %s in %s (%s → %s).",
        member.status,
        chat.id,
        previous.status,
        member.status,
    )
    spawn_group_keyboard_clear(bot, chat.id, chat.title)

    # Administrator is also the only status in which the admin listing works, so
    # this transition is the moment a roster becomes listable for the first
    # time. Pulling it here is what makes a whisper recipient who is an admin of
    # the group resolvable straight after the bot is promoted.
    if member.status == "administrator":
        spawn_chat_roster_sync(bot, chat.id)

    # The rights conversation only happens on the way IN. A bot that was removed
    # and re-added, or demoted and re-promoted, gets asked again — that is the
    # moment the need is real again.
    joined = previous.status not in INSIDE_STATUSES
    demoted = previous.status == "administrator" and member.status == "member"
    if not (joined or demoted):
        return

    # ``update.from_user`` is whoever performed the change — the admin who added
    # or promoted the bot. They are the one recipient who is guaranteed to be
    # able to act on the request, so they get a DM even though the bot cannot
    # enumerate the admin list from inside a group it is not an admin of.
    adder = update.from_user

    if joined:
        # ONE card out of the group — welcome and the rights ask together —
        # plus the DMs to the people who can act on the ask.
        await _post_onboarding(bot, chat, state=state, previous=previous, adder=adder)
        return

    # Demoted: nothing to welcome anybody to, so the request stands alone.
    if state == STATE_MEMBER:
        await _ask_for_promotion(bot, chat, adder)
    elif state == STATE_ADMIN and classify(previous) != STATE_ANONYMOUS:
        await _suggest_anonymous_admin(bot, chat, adder)


# ──────────────────────────────────────────────────────────
# Group roster — the source the inline picker resolves targets from
# ──────────────────────────────────────────────────────────

@router.chat_member()
async def on_member_changed(update: ChatMemberUpdated) -> None:
    """Keep the in-memory roster in step with who joins and leaves.

    This is the authoritative feed: a bot that is an administrator in a group
    receives ``chat_member`` for every membership change there, so the registry
    in :mod:`utils.group_members` is complete instead of "whoever happened to
    talk today". Registering the handler is also what makes aiogram ask
    Telegram for ``chat_member`` updates at all.

    A bot that is only a plain member never sees these, which is why the
    registry also learns from ordinary group messages (see
    ``middleware.group_member_watch``), from ``getChatAdministrators`` once it is
    promoted (see :mod:`utils.roster_sync`), and from the ``users`` table at
    lookup time (see :mod:`utils.target_lookup`).

    Best-effort and silent: roster bookkeeping must never raise into the
    dispatcher.
    """
    if not is_group(update.chat.type):
        return

    member = update.new_chat_member
    if member is None:  # context not bound — nothing to record
        return

    if member.status in GONE_STATUSES:
        forget_member(member.user.id)
    else:
        remember_member(
            member.user.id,
            member.user.first_name or "",
            member.user.username or "",
            chat_id=update.chat.id,
        )


@router.my_chat_member()
async def on_bot_left_group(update: ChatMemberUpdated) -> None:
    """Drop the roster once the bot itself is gone from a group."""
    member = update.new_chat_member
    if member is None or member.user.id != update.bot.id:
        return
    if member.status in GONE_STATUSES:
        forget_chat(update.chat.id)


__all__ = ["router"]
