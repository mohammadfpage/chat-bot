"""
Middlewares: banned-user blocker and force-join enforcement.

Both are outer middlewares applied to messages, callbacks and inline queries.

Force-join is chat-type aware, and the channel list is a PRIVATE-CHAT-ONLY
artefact:

  * in a private chat (``/start`` and everything else) the user gets the real
    prompt with the channel buttons and the «تأیید عضویت» button,
  * in a group nothing is posted at all — neither the channel list nor a bot
    notice. A callback tap gets a short polite alert pointing at the bot's PM,
    and an inline query gets a single result that says the same thing. The
    user's own message is NEVER deleted and never answered in the group, so
    the chat stays exactly as they left it.

The required list is ``utils.membership.required_chats()`` — the admin panel's
``required_channels`` rows, the legacy single-chat fields AND the ``.env``
channel, de-duplicated. Reading only ``.env`` here (the old behaviour) meant a
channel configured purely from the panel was shown in prompts but never
enforced. The ``whisper_config.require_join`` master switch is honoured by the
same funnel: off ⇒ nothing is gated.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from sqlalchemy import select

from config import settings
from database import async_session_factory, User
from keyboards.inline import force_join_kb
from utils.membership import (
    ChatRef,
    join_bullet,
    missing_required_chats,
)

logger = logging.getLogger(__name__)


# Commands that always bypass the forced-join check.
# ``fix_keyboard`` / ``remove_keyboard`` are here because they are an
# URGENT, one-off repair: deleting the message and bouncing the sender into
# a force-join flow would leave the stuck keyboard exactly where it was.
_BYPASS_COMMANDS = {"start", "help", "fix_keyboard", "remove_keyboard"}

# Callbacks that are part of a membership flow and must reach their handler.
#
# ``read_whisper:*`` / ``whisper:*`` callbacks carry their OWN forced-join gate:
# they check membership inside the handler and render their own join prompt
# with the "عضو شدم" button that resumes the flow. The middleware must NOT
# intercept them, otherwise the user is stuck in a loop — told to join, taps
# "عضو شدم", and the callback is bounced again: an unwatchable loop that ended
# with the whisper card saying the message does not exist. Because of that,
# EVERY whisper callback is allowed through the middleware and handled by the
# whisper router itself, which applies the gate at the right granularity.
#
# ``read_whisper:`` must be listed explicitly: the inline-mode whisper card
# (handlers/inline_anon.py) does NOT use the ``whisper:`` prefix, so
# ``startswith("whisper:")`` alone would not match it and the recipient's tap
# would be swallowed here and never reach the read handler.
#
# The three whisper ACTION callbacks (``whisper_stats:`` / ``whisper_options:`` /
# ``delete_whisper:``) are on the same card, so they need the same treatment —
# and they also miss ``whisper:`` for the same structural reason: they are
# suffixed off a different word. Without them the recipient's own card bounces
# every one of them into a join prompt the card has no way to complete.
#
# «↩️ پاسخ» is deliberately ABSENT, and no longer merely missing: it is a
# ``switch_inline_query_current_chat``, so it carries no ``callback_data`` at all
# and arrives here as an ``inline_query`` update, which this middleware does not
# intercept at all. Its membership gate is the same one every whisper goes
# through, in the inline handler itself.
#
# The anonymous-chat callbacks (``start_anon_chat:`` / ``anon_chat:``) are the
# same situation for the same reason: they are a conversation BETWEEN two users
# that the channel membership has nothing to do with, and every one of them
# answers in a private chat — where a bounced callback shows nothing at all,
# because ``callback.answer()`` cannot be delivered. Interception would leave the
# card looking dead. The gate that does matter (has this person started the bot?)
# lives in ``utils.anon_sessions.has_started`` and is checked in the handler.
_ALLOWED_CALLBACK_PREFIXES = ("whisper:", "read_whisper:", "whisper_stats:",
                              "whisper_options:", "delete_whisper:",
                              "anon_open",
                              "anon_block", "inbox:", "unblock:",
                              "blocked_list:back", "check_membership",
                              "start_anon_chat:", "anon_chat:")


def _callback_allowed(data: str | None) -> bool:
    """True when the callback owns its own membership gate.

    Any callback that belongs to the whisper, anonymous-inbox or
    membership-verification flows owns its own gate, so the middleware
    never intercepts them. Callbacks that don't own a gate (plain menu
    buttons, admin commands, etc.) are subject to the force-join check.
    """
    if not data:
        return False
    return data.startswith(_ALLOWED_CALLBACK_PREFIXES)

#: The PRIVATE-CHAT copy of the prompt: the channel list with its buttons
#: lives here and nowhere else. This is what ``/start`` shows, and it is the
#: single place a user is told which channels to join.
_JOIN_PROMPT = (
    "برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید:\n"
    "{lines}\n\n"
    "پس از عضویت، دکمهٔ «تأیید عضویت» را بزنید تا بررسی شود."
)

#: What a GROUP tap and an inline query get instead of the channel list. No
#: links, no keyboard, no bot notice in the chat — just a pointer to the PM,
#: where the join is actually handled.
_GROUP_ALERT = (
    "⚠️ برای استفاده از ربات و خواندن/ارسال نجوا، ابتدا باید ربات را نصب "
    "و راه‌اندازی کنید.\n\n"
    "لطفاً ربات را در گفتگوی خصوصی باز کنید و دکمهٔ «شروع» را بزنید؛ "
    "عضویت در کانال‌های ربات همان‌جا از شما خواسته می‌شود."
)

#: Title / description of the single inline result that replaces the whole
#: picker for a user who is not a member yet.
_INLINE_TITLE = "ابتدا ربات را در گفتگوی خصوصی راه‌اندازی کنید"
_INLINE_DESCRIPTION = "عضویت در کانال‌ها فقط در گفتگوی خصوصی ربات انجام می‌شود."


async def _is_admin(user_id: int) -> bool:
    """Cheap admin check for middleware (root OR db flag)."""
    if user_id in settings.admin_ids_list:
        return True
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        db_user = result.scalar_one_or_none()
        return bool(db_user and db_user.is_admin)


async def _is_banned(user_id: int) -> bool:
    """Return True if the user is banned in the database."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        db_user = result.scalar_one_or_none()
        return bool(db_user and db_user.is_banned)


#: Users who already received the one-time ban notice. In-memory on purpose:
#: the docstring promises ONE notification per ban, and a DB write per blocked
#: update would cost more than the block itself. Reset on restart, which just
#: means a banned user may see the notice once more after a deploy.
_ban_notified: set[int] = set()


def forget_ban_notice(user_id: int) -> None:
    """Let a re-banned user hear the notice again (called by the unban path)."""
    _ban_notified.discard(user_id)


class BlockBannedMiddleware(BaseMiddleware):
    """
    Instantly blocks any banned user at the middleware level.

    Banned users cannot trigger ANY handler (messages or callbacks) — they
    only receive a short notification once. This guarantees that a ban takes
    effect immediately, even mid-conversation.
    """

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return await handler(event, data)

        # Admins can never be blocked this way.
        if await _is_admin(user.id):
            return await handler(event, data)

        if not await _is_banned(user.id):
            return await handler(event, data)

        # Banned → swallow the event. The ONE notification the docstring
        # promises used to be missing entirely (silent swallow); send it here,
        # once, without letting a dead chat turn the block into a crash.
        if user.id not in _ban_notified:
            _ban_notified.add(user.id)
            bot = data.get("bot")
            if bot is not None:
                try:
                    await bot.send_message(
                        user.id, "⛔ شما از ربات محروم شده‌اید."
                    )
                except Exception as exc:
                    logger.debug("Could not notify banned user %s: %s", user.id, exc)
        return None


class ForceJoinMiddleware(BaseMiddleware):
    """Outer middleware that enforces channel membership on every update.

    The channel list itself is shown in ONE place only — the private chat. In a
    group the user is pointed at the PM and nothing else: no channel buttons,
    no bot notice in the chat, and above all no deletion of their message.
    """

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery | InlineQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return await handler(event, data)

        # All admins bypass the forced-join check.
        if await _is_admin(user.id):
            return await handler(event, data)

        # Whitelist of callbacks that own their own membership gate. Checked
        # before ANY database or API work: these flows re-verify with
        # force_refresh inside their own handlers, so the middleware's cache
        # is neither consulted nor (as it used to be) wiped wholesale here —
        # clearing every cached answer on every allowed tap threw away up to
        # 30 seconds of lookups the rest of the bot had already paid for.
        if isinstance(event, CallbackQuery) and _callback_allowed(event.data):
            return await handler(event, data)

        # Bypass for commands that must never trigger a join prompt.
        if isinstance(event, Message) and event.text and event.text.startswith("/"):
            cmd = event.text.split()[0].lstrip("/").split("@")[0].lower()
            if cmd in _BYPASS_COMMANDS:
                return await handler(event, data)

        bot = data.get("bot")
        if not bot:
            return await handler(event, data)

        # The FULL required list decides — panel rows first, .env as the
        # legacy fallback — not just the .env pair this middleware used to
        # read. An empty result means "force-sub off / nothing configured",
        # i.e. no gate at all; a crash in the lookup fails OPEN (see
        # utils.membership.is_member) so a broken channel cannot lock
        # everybody out of the bot.
        try:
            missing = await missing_required_chats(bot, user.id)
        except Exception as exc:  # pragma: no cover - belt and braces
            logger.warning("Force-join check failed — letting the update "
                           "through: %s", exc)
            missing = []
        if not missing:
            return await handler(event, data)

        # ── Not a member of everything yet ──
        # A non-admin user who has NEVER started the bot is not blocked: they
        # simply need to open the PM, join and tap "تأیید عضویت". The
        # whisper/inline handlers then pick the flow back up and reactivate
        # their parked whisper, so nothing is lost.
        await self._intercept(event, missing)
        return None

    async def _intercept(
        self,
        event: Message | CallbackQuery | InlineQuery,
        missing: list[ChatRef],
    ) -> None:
        """Deliver the "not a member yet" answer for the update's context.

        Split out from ``__call__`` so the three branches read as what they are
        — one rule per chat type — instead of a single block that grew a
        different answer for each.
        """
        if isinstance(event, InlineQuery):
            await _answer_inline_query(event)
            return

        if isinstance(event, CallbackQuery):
            prompt = event.message
            if prompt is not None and prompt.chat.type == ChatType.PRIVATE:
                # A PRIVATE tap gets the real channel list, exactly like a
                # message would — the old code answered every callback with
                # the group alert ("open the bot in PM"), which is nonsense
                # when they are already in the PM with a dead button under
                # their finger. An alert also cannot carry the join
                # keyboard, so the prompt goes out as a proper message.
                await _answer_join_prompt(event, prompt, missing)
                return
            # A group button tap never shows the channel list: an alert on the
            # user's own screen only, so nothing is written into the chat.
            await event.answer(_GROUP_ALERT, show_alert=True)
            return

        # A group or channel message: silently drop the update. The user is
        # told nothing here, their message is never deleted, and the join
        # prompt waits for them in the PM — where ``/start`` puts it.
        if event.chat.type != ChatType.PRIVATE:
            return

        # Private chat: the ONE place the channel list is allowed to exist.
        await _answer_join_prompt(event, event, missing)


async def _answer_join_prompt(
    event: Message | CallbackQuery,
    target: Message,
    missing: list[ChatRef],
) -> None:
    """Post the real join prompt (channel list + «تأیید عضویت») in the PM.

    ``event`` is what must be acknowledged — a callback tap gets its spinner
    cleared first, otherwise Telegram keeps it spinning until it times out.
    ``target`` is the message the prompt is sent next to: the tapped message
    for callbacks, the blocked message itself otherwise.

    Failures are swallowed on purpose: this runs inside a middleware, and a
    prompt that cannot be delivered (user blocked the bot one second ago)
    must not turn into an unhandled error that swallows the whole update.
    """
    if isinstance(event, CallbackQuery):
        try:
            # A short toast so the tap itself gives feedback; the full list
            # rides in as a message right after (an alert cannot carry a
            # keyboard, and this message may be old and uneditable).
            await event.answer("⚠️ برای استفاده از ربات، ابتدا عضو کانال‌ها شوید.")
        except Exception:
            pass

    # The @handle rides on every line (see ``join_bullet``): the button URL
    # is not the only way in — a tap the VPN swallows still leaves a
    # copyable ID in the text above it.
    lines = "\n".join(join_bullet(chat) for chat in missing) or "—"
    try:
        await target.answer(
            _JOIN_PROMPT.format(lines=lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=force_join_kb(missing),
        )
    except Exception as exc:
        logger.debug("Could not deliver join prompt: %s", exc)


async def _answer_inline_query(inline_query: InlineQuery) -> None:
    """Answer the picker with ONE article that points at the PM.

    Telegram shows a spinner forever when an inline query is never answered,
    so a refusal is still an answer — and exactly one result is returned: the
    whole point is that the user is sent to the private chat, where the join
    is actually handled, instead of being handed a channel list to work
    through inside someone else's conversation.
    """
    try:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="force_join:pm",
                    title=_INLINE_TITLE,
                    description=_INLINE_DESCRIPTION,
                    input_message_content=InputTextMessageContent(
                        message_text=_GROUP_ALERT,
                        parse_mode="HTML",
                    ),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
    except Exception:
        # A malformed or expired query must not raise out of a middleware.
        try:
            await inline_query.answer(results=[], cache_time=0, is_personal=True)
        except Exception:
            pass


__all__ = [
    "BlockBannedMiddleware",
    "ForceJoinMiddleware",
]
