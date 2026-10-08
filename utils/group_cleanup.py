"""
On-startup group keyboard cleanup.

Why this exists
---------------
Telegram clients cache the last reply keyboard **per chat**. Removing the bot,
being kicked and re-added, clearing the app cache — none of it flushes that
cache. The only thing that clears it is a *new message from the bot carrying
``ReplyKeyboardRemove``*, so a group that was ever sent a
``ReplyKeyboardMarkup`` by an older build keeps the keyboard stuck at the bottom
for every member until someone explicitly repairs it.

So on every start we re-send the removal to each group the bot has ever posted
in. Combined with :mod:`middleware.keyboard_guard` — which makes it impossible
to put the keyboard *back* — this is the "permanent removal" half of the fix.

Why send-then-delete
--------------------
The Bot API has no silent message: a bot must post real text, and a text of
``""`` is rejected. So the sweep sends a single invisible character, lets the
clients apply the removal, then deletes the message. The ordering is load
bearing: the reply markup is processed by the client when the update *arrives*,
so the delay before deleting must be long enough for slow mobile clients to
have handled it. Deleting first would leave the cache dirty again.

Chat ids come from the ``whispers`` table (``chat_id`` is recorded on every
whisper card), cross-checked against ``whisper_config.required_chat_id``. Each
id is verified with ``get_chat`` before being messaged: a row can outlive the
bot's membership of the group, and a broadcast to an id the bot no longer
belongs to just produces an error per group on every restart.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, Message
from sqlalchemy import select, update

from config import settings
from database import (
    async_session_factory,
    GroupChat,
    RequiredChannel,
    Whisper,
    WhisperConfig,
)
from utils.chat_types import is_group
from utils.group_members import forget_chat

logger = logging.getLogger(__name__)

#: What the sweep posts so clients can apply ``ReplyKeyboardRemove``. It must
#: be a REAL character: Telegram rejects anything it considers empty, and a
#: lone zero-width space (the old value) was answered with
#: "Bad Request: text must be non-empty" — which silently disabled this whole
#: cleanup on every start ("Cleared 0/1 group chat(s)"). A single "." is the
#: least intrusive thing that passes validation; the message is deleted right
#: after the clients have processed the removal.
_INVISIBLE_TEXT = "."

#: Pause between groups. Guards against flood control when a popular group has
#: many whisper rows and the distinct-id list is long.
_CHAT_DELAY = 1.0

#: Cap the sweep so a large history cannot stall startup for minutes.
_MAX_CHATS = 200

#: Telegram announces a group→supergroup conversion by refusing the old id and
#: naming the new one: "The group has been migrated to a supergroup with id
#: -1004303622920 from -5524975501". Capturing that number is the only way to
#: learn it — there is no API that reports the rename, and no update is emitted
#: for it either.
_MIGRATED_RE = re.compile(
    r"migrated to a supergroup with id (-?\d+)", re.IGNORECASE
)


def _migrated_to(exc: TelegramAPIError) -> int | None:
    """The new chat id hidden in a migration error, or ``None`` for any other."""
    match = _MIGRATED_RE.search(getattr(exc, "message", "") or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def known_chat_ids() -> set[int]:
    """Every group-ish chat id the bot has ever interacted with.

    Three sources, because any one of them alone leaves a hole: whisper rows
    (the original reason this sweep exists), the force-join target, and the
    ``group_chats`` registry — which is what remembers a group the bot was
    simply *added* to and that has never produced a whisper.

    Read defensively: a failure here must not prevent the bot from starting,
    so the caller treats an empty result as "nothing to clean".
    """
    ids: set[int] = set()

    try:
        async with async_session_factory() as session:
            result = await session.execute(select(Whisper.chat_id).distinct())
            ids.update(int(row) for row in result.scalars() if row is not None)

            groups = await session.execute(select(GroupChat.chat_id).distinct())
            ids.update(int(row) for row in groups.scalars() if row is not None)

            config = await session.execute(select(WhisperConfig).limit(1))
            row = config.scalar_one_or_none()
            if row is not None and row.required_chat_id:
                ids.add(int(row.required_chat_id))
    except Exception as exc:
        logger.warning("Keyboard cleanup: could not read chat ids: %s", exc)

    return ids


async def rewrite_chat_id(old_chat_id: int, new_chat_id: int) -> int:
    """Point every stored reference at ``new_chat_id``. Returns rows changed.

    A group that became a supergroup keeps its history but not its id, and this
    bot addresses groups by id in four tables. Left alone, the old id would rot
    in place: the startup sweep would skip it as "not found" forever, whispers
    would keep being recorded against a chat nobody can reach, and the
    force-join target would silently point at a dead group.

    Only rows matching the exact old id are touched, so a sweep can never
    disturb an unrelated group, and a no-op migration costs nothing.
    """
    changed = 0
    try:
        async with async_session_factory() as session:
            for statement in (
                update(Whisper)
                .where(Whisper.chat_id == old_chat_id)
                .values(chat_id=new_chat_id),
                update(GroupChat)
                .where(GroupChat.chat_id == old_chat_id)
                .values(chat_id=new_chat_id),
                update(WhisperConfig)
                .where(WhisperConfig.required_chat_id == old_chat_id)
                .values(required_chat_id=new_chat_id),
                # A force-join target can be a group too, and it would rot the
                # same way.
                update(RequiredChannel)
                .where(RequiredChannel.chat_id == old_chat_id)
                .values(chat_id=new_chat_id),
            ):
                result = await session.execute(statement)
                changed += result.rowcount or 0
            await session.commit()
    except Exception as exc:
        # Losing the rewrite is recoverable: the group is simply swept under its
        # new id next time the bot is added there. Raising here would abort the
        # whole startup sweep.
        logger.warning(
            "Keyboard cleanup: could not rewrite %s -> %s: %s",
            old_chat_id,
            new_chat_id,
            exc,
        )
        return 0

    # The in-memory roster is keyed by the old id too; drop it so the new id is
    # remembered fresh instead of the stale roster shadowing it.
    forget_chat(old_chat_id)

    logger.warning(
        "Group %s became supergroup %s — rewrote %d stored reference(s).",
        old_chat_id,
        new_chat_id,
        changed,
    )
    return changed


async def _resolve_group(bot: Bot, chat_id: int) -> Chat | None:
    """Return the chat if it is a group/supergroup the bot can still reach.

    Follows a group→supergroup migration instead of treating it as staleness:
    the id changed, but the group did not go anywhere, so the sweep repairs the
    stored references and carries on with the *new* chat.
    """
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        new_chat_id = _migrated_to(exc)
        if new_chat_id is not None and new_chat_id != chat_id:
            await rewrite_chat_id(chat_id, new_chat_id)
            try:
                chat = await bot.get_chat(new_chat_id)
            except TelegramAPIError as retry_exc:
                logger.info(
                    "Keyboard cleanup: %s migrated to %s but that is unreachable "
                    "(%s).",
                    chat_id,
                    new_chat_id,
                    retry_exc.message,
                )
                return None
            except Exception as retry_exc:
                logger.warning(
                    "Keyboard cleanup: get_chat(%s) after migration failed: %s",
                    new_chat_id,
                    retry_exc,
                )
                return None
        else:
            # Overwhelmingly "bot was kicked" or "chat not found" — the id is
            # stale, which is normal and not worth a scary log line.
            logger.info("Keyboard cleanup: skipping %s (%s).", chat_id, exc.message)
            return None
    except Exception as exc:
        logger.warning("Keyboard cleanup: get_chat(%s) failed: %s", chat_id, exc)
        return None

    if not is_group(chat.type):
        return None
    return chat


async def _clear_chat(bot: Bot, chat_id: int) -> bool:
    """Send the removal to one chat, then clean the message up.

    Returns True when the removal was delivered. A failure to *delete* the
    message afterwards is deliberately not fatal: the client has already
    applied the removal by then, and an invisible leftover message is a far
    smaller problem than the keyboard this whole routine exists to clear.
    """
    # Imported here, not at module scope: ``middleware.keyboard_guard`` drags in
    # the whole middleware package, whose watcher imports this module back. At
    # module scope that made ``utils.group_cleanup`` unimportable unless
    # something had already imported ``middleware`` first.
    from middleware.keyboard_guard import clear_keyboard_markup

    try:
        sent: Message = await bot.send_message(
            chat_id,
            _INVISIBLE_TEXT,
            reply_markup=clear_keyboard_markup(),
        )
    except TelegramAPIError as exc:
        logger.info(
            "Keyboard cleanup: could not clear %s: %s", chat_id, exc.message
        )
        return False
    except Exception as exc:
        logger.warning("Keyboard cleanup: send to %s failed: %s", chat_id, exc)
        return False

    # Clients apply the removal on receipt, so the message has to outlive the
    # slowest one we care about before it is pulled.
    await asyncio.sleep(max(settings.group_keyboard_cleanup_delay, 0.0))

    try:
        await bot.delete_message(chat_id, sent.message_id)
    except TelegramAPIError as exc:
        logger.info(
            "Keyboard cleanup: sent to %s but could not delete the message: %s "
            "(it is invisible, harmless)",
            chat_id,
            exc.message,
        )
    except Exception as exc:
        logger.warning("Keyboard cleanup: delete in %s failed: %s", chat_id, exc)

    return True


async def _clear_one(bot: Bot, chat: Chat) -> bool:
    """``_clear_chat`` for a verified group ``Chat`` (kept for the sweep)."""
    return await _clear_chat(bot, chat.id)


async def cleanup_group_keyboards(bot: Bot) -> None:
    """Startup hook: force-clear the cached reply keyboard in every group.

    Registered on ``dp.startup``, so aiogram injects ``bot`` automatically.

    Every failure path is swallowed on purpose. A cleanup that raises would
    abort ``start_polling`` and take the whole bot offline over a cosmetic UI
    bug in someone else's group, so this routine reports and moves on.
    """
    if not settings.group_cleanup_enabled:
        logger.info("Keyboard cleanup: disabled via config.")
        return

    try:
        chat_ids = sorted(await known_chat_ids())[:_MAX_CHATS]
        if not chat_ids:
            logger.info("Keyboard cleanup: no known group chats, nothing to do.")
            return

        logger.info("Keyboard cleanup: sweeping %d group chat(s)...", len(chat_ids))
        cleared = 0
        for chat_id in chat_ids:
            chat = await _resolve_group(bot, chat_id)
            if chat is None:
                continue
            if await _clear_one(bot, chat):
                cleared += 1
            await asyncio.sleep(_CHAT_DELAY)

        logger.info(
            "Keyboard cleanup: done. Cleared %d/%d group chat(s).", cleared, len(chat_ids)
        )
    except Exception as exc:
        logger.warning("Keyboard cleanup: aborted early: %s", exc)


# ──────────────────────────────────────────────────
# First-sight clearing (groups the startup sweep cannot know about yet)
# ──────────────────────────────────────────────────

#: Groups already handled by THIS process. Checked-and-set synchronously
#: before the first ``await``, so a burst of messages in the same group can
#: never schedule two clears at once. Deliberately not persisted: after a
#: restart the sweep at startup takes over (the row is already in
#: ``group_chats``), and re-clearing once more is harmless anyway.
_seen_this_process: set[int] = set()


async def remember_group(chat_id: int, title: str | None = None) -> None:
    """Record a group so every future startup sweep includes it.

    Best effort on purpose: failing to remember must never break whatever
    update triggered the call — the next sight of the group simply tries
    again.
    """
    try:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(GroupChat).where(GroupChat.chat_id == chat_id)
            )
            if row is None:
                session.add(GroupChat(chat_id=chat_id, title=title))
            elif title and row.title != title:
                row.title = title
            await session.commit()
    except Exception as exc:
        logger.debug("Keyboard cleanup: could not remember %s: %s", chat_id, exc)


async def clear_group_keyboard_once(
    bot: Bot,
    chat_id: int,
    title: str | None = None,
) -> None:
    """Remember ``chat_id`` and clear its cached reply keyboard — once.

    This is the "I just added the bot to a group" path. The startup sweep
    only knows groups already stored in the database; a group the bot has
    never seen before is invisible to it, so a keyboard left behind by an
    older build would stay stuck there for every member until someone
    happened to restart *after* the first whisper was posted. Triggering a
    clear on the first update from a group closes that gap immediately,
    without a restart and without waiting for a whisper.

    Everything is fire-and-forget-safe: the caller spawns this as a task, so
    a slow send or the cleanup delay never holds up the update being handled.
    """
    if chat_id in _seen_this_process:
        return
    _seen_this_process.add(chat_id)  # sync check-and-set: no race below

    await remember_group(chat_id, title)

    if not settings.group_cleanup_enabled:
        return

    try:
        if await _clear_chat(bot, chat_id):
            logger.info("Keyboard cleanup: cleared group %s on first sight.", chat_id)
    except Exception as exc:
        # A background clear must never take down the task that spawned it.
        logger.warning("Keyboard cleanup: first-sight clear of %s failed: %s", chat_id, exc)


__all__ = [
    "clear_group_keyboard_once",
    "cleanup_group_keyboards",
    "known_chat_ids",
    "remember_group",
    "rewrite_chat_id",
]
