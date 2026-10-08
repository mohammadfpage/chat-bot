"""
Secure Group Whisper (نجوا) through Telegram Inline Mode + callback alerts
=========================================================================

**The secret never enters the chat.**

Flow:

 1. ``@bot 🆔 <id> 💬 <secret_message>`` typed in ANY chat → ``inline_query``.
      The two emoji markers are what tell the user which half of the query is
      the recipient and which half is the text; they are part of the syntax, not
      decoration. A UUID4 ``whisper_id`` is minted and the row
      ``{whisper_id: {sender_id, target_id, secret_text, created_at}}`` is
      COMMITTED right here, in the ``inline_whispers`` table, BEFORE the answer
      is sent. Exactly ONE ``InlineQueryResultArticle`` comes back — the
      actionable one: ``📬 ارسال نجوا به <target>``.

      Its body is the **anchor** (``⏳ در حال ارسال پیام ناشناس…``): a neutral,
      content-free placeholder carrying nothing but the ``👁️ مشاهده پیام``
      button. The anchor is not the card — it is the bot's handle on the chat
      (see step 1b).

1b. ``on_inline_result_message`` sees that anchor arrive (Telegram delivers the
      message it posted from our inline result, and it carries ``chat.id``) and
      the anchor is REWRITTEN IN PLACE into the real card:
      ``📩 یک پیام ناشناس برای <target> رسید — فقط او می‌تواند آن را بخواند`` +
      the read button.

      **The anchor is never deleted.** It is the sender's own message and stays
      in the chat history exactly as they sent it; the rewrite only swaps the
      ⏳ placeholder for the finished card. Nothing is ever removed from a chat
      on a user's behalf.

      **Why the card lives on that message.** A message published from an inline
      result is authored by the person who picked the result, so the whole group
      sees the sender's name and avatar next to it. The rewrite therefore puts
      the sender's name on the card — the one honest price of never deleting
      their message. The secret still never enters the chat: that is what the
      row and the button are for. It also means the card is an ordinary group
      message: its button arrives as a normal ``callback_query`` carrying
      ``callback.message``, which is what lets an ordinary (non-admin) member
      open it.

      The markers are optional in practice. ``@bot @Sara راز ماست``,
      ``@bot 123456789 راز ماست`` and ``@bot راز ماست @Sara`` all produce that
      same card — the three orders are exactly what the three buttons of the
      inline menu pre-fill into the picker, and a button that typed a query the
      parser rejected would teach the user that the button is broken. The
      guide/menu fallback therefore only appears when there is genuinely
      nothing to send: an empty picker, or a query with no secret behind it.

      **An inline query handler never messages the sender.** A bot cannot DM
      anyone who has not started it — which is exactly the population a guide
      is for. Both exits out of the picker therefore use Telegram's own
      affordances:

        * ``button=InlineQueryResultsButton(start_parameter="help")`` renders
          «📖 راهنمای ارسال نجوا» ABOVE the results. Telegram opens
          ``/start help`` in the bot's own chat, so the conversation is started
          by the user and the guide is then a legal reply.
        * an empty or unparsable query answers with the **placeholder** article,
          whose title IS the pattern (``🆔 @Username_or_ID  💬 متن نجوا``). The
          instructions are visible without a single tap; tapping it posts a
          harmless reminder that :func:`on_inline_result_message` leaves in
      place.

      **The RECIPIENT is messaged, though, and that is a different case.** The
      sender is the one person whose ``/start`` the picker can assume — they just
      typed into the bot. The recipient is a group member who may never have
      started it, and the card alone is not enough: a modal is a popup that
      vanishes, and a whisper nobody is told about is a whisper nobody reads.
      So the send path DMs the recipient a durable «👁️ یک پیام مخفی دارید»
      notice (:func:`whisper_notice_text`), records its id on the row, and edits
      it to «✅ پیام خوانده شد» the first time they open the whisper. When that
      DM is impossible the whisper is NOT failed — the row is live and the card
      works — so the sender is told the recipient has to start the bot
      (:func:`target_not_started_text`) instead of being handed an error.

      One box, never two: there is nothing to choose between, so a picker with
      two identical-purpose entries was only noise.

   2. ``chosen_inline_result`` — the request is re-validated (target, block
     list, whisper limits, forced-join, token economy) and the ALREADY
     COMMITTED row is activated. A query the sender never picks leaves an
     inactive reservation that is swept after ``ORPHAN_MINUTES``.

     This update is no longer the sender of the card: step 1b already finished
     it. What it does now is *reconcile* — record the card id so the orphan
     sweep cannot take a row that backs a live button, then wait a few seconds
     for the anchor handler to finish. A pick whose anchor never reached us
     (the bot was restarting, the update was pruned) leaves nothing published
     anywhere, so the row is dropped and the sender is told in a DM rather than
     being charged for a whisper no one can ever open.

  3. ``callback_query`` on ``read_whisper:<whisper_id>``:
       • ``callback.from_user`` is the TARGET **or the SENDER** →
         ``callback.answer(secret, show_alert=True)`` — a private modal
         rendered ONLY on their own client; group admins and
         everyone else see nothing.
       • anyone else → «🚫 شما اجازه خواندن این نجوا را ندارید!», and the
         press is appended to the row's ``snoopers`` list so the two parties
         can see who knocked, later, through «📊 آمار».

The same card carries four more buttons beside the read one — «📊 آمار»,
      «⚙️ گزینه‌ها», «🗑️ حذف» and «↩️ پاسخ», all built by
      ``keyboards.inline.get_whisper_action_keyboard``. None of them can be
      hidden from anyone, because Telegram freezes the markup the moment the
      message is published, so each handler asks the row who is asking: stats
      and options for the two parties only, delete for the sender only.

      The TARGET's first read does two things, in this order: it stamps
      «✅ این نجوا توسط گیرنده خوانده شد.» onto the public card, and it edits
      their own private notice to «✅ پیام خوانده شد». Same flag, same instant —
      one is the receipt for the group and one is the receipt for the person
      who was waiting, and a notice left reading «you have a secret message»
      after the secret is gone is a message that lies for ever.

     Those four appear **only after the recipient has opened the whisper** —
     until then a card carries «👁️ مشاهده پیام» and nothing else, which is the
     rule :func:`_card_keyboard` owns. Shipping them from the start put a snooper
     list, a delete button and a reply button in front of a whole group on a
     card nobody had claimed, and every one of them dead-ended: the sender has
     no delete at all until the recipient opens the card, which is the trade
     this feature makes deliberately.

     «↩️ پاسخ» is the odd one out and the only action that is not a callback:
     it is a ``switch_inline_query_current_chat`` that puts ``reply:<id>``
     into the user's own input field, in the same group the card lives in, so
     the reply begins exactly where the whisper was read — no private chat, and
     no message the bot has to be allowed to send. The card cannot put the
     recipient in that payload (the target answers the sender, the sender
     answers the target, and one markup serves both), so the query carries the
     whisper id alone and :func:`on_inline_query` resolves the opposite party
     from the row. That id is editable, copyable text, so the reply is checked
     against the row's parties like every other action on the card — see
     :func:`_reply_target`.

Who may be whispered
--------------------
The recipient is resolved by :func:`utils.target_lookup.resolve_target`, which
asks **every** source in turn: the in-memory group roster
(:mod:`utils.group_members`) — "is this person inside a group the bot can
see?" — then the ``users`` table, then Telegram itself. The roster leads
because it is the cheap one (a plain dict fed for free by ``chat_member``
updates and by ordinary group traffic, so typing a whisper costs dictionary
hits instead of a burst of SQL per keystroke), but it is *not* an alternative to
the other two. It used to be: the roster answered if it happened to be populated
and the ``users`` table answered if it was not, so a member of the very group the
whisper was typed in could be told they were in none of the bot's groups. See
:func:`_lookup_target`.

Why the row is committed in step 1 and not step 2: the anchor is published the
INSTANT the result is picked — and it already carries the ``👁️ مشاهده پیام``
button, because that button is what carries the token back to us — but
``chosen_inline_result`` is delivered 0.3–2.5s later. Writing the row there left
a window in which a live button pointed at an id the database did not have yet,
and the reader who tapped inside it was told «⛔️ این نجوا دیگر موجود نیست» about
a whisper that had been sent, charged for, and was sitting in the table a moment
later. Committing first removes the window entirely.

The id has to exist before the anchor anyway — Telegram bakes ``reply_markup``
into the message when the result is picked — and ``callback_data`` is capped at
64 bytes, hence a 32-hex ``uuid4().hex``: ``read_whisper:<id>`` is 44 bytes and
fits the existing ``token VARCHAR(32)`` column with no schema change. The
``read_whisper:`` prefix also keeps these callbacks disjoint from the
``whisper:help`` / ``whisper:check`` / ``whisper:view:<n>`` family owned by
``handlers/whisper.py`` — this router is registered FIRST in ``bot.py``, so its
filters must never swallow those. Cards published under the old
``whisper:<16 hex>`` shape keep working: both are routed to the same handler,
and :func:`cb_read_whisper` still handles a callback that arrives without a
``message`` (the old inline-sent cards) as well as one that has it.

Telegram caps alert text at 200 characters: longer secrets are truncated in
the modal and the full text is then DM'd to the recipient, who has already
proven ownership by pressing the button.

Requires inline mode in @BotFather (``/setinline``) — without it Telegram
never sends ``inline_query`` updates. aiogram derives ``allowed_updates``
from the registered handlers, so ``chosen_inline_result`` is requested
automatically.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from html import escape
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Message,
)
from sqlalchemy import delete, select, update

from config import settings
from database import (
    BlockList,
    InlineWhisper,
    async_session_factory,
)
from handlers.inline_menu import (
    MENU_CARD_PREFIXES,
    build_menu_results,
    tutorial_guide_text,
)
from keyboards import (
    ANON_REQUEST_CB_PREFIX,
    HELP_START_PARAM,
    TUTORIAL_PHOTO_CALLBACK,
    WHISPER_DELETE_CB_PREFIX,
    WHISPER_OPTIONS_CB_PREFIX,
    WHISPER_REPLY_QUERY_PREFIX,
    WHISPER_STATS_CB_PREFIX,
    get_whisper_action_keyboard,
    main_menu_kb,
    whisper_join_kb,
    whisper_read_kb,
    whisper_verify_kb,
)
from utils.economy import (
    authorize_chat_message,
    can_afford_whisper,
    check_and_deduct_balance,
    fmt_coins,
    no_balance_text,
    get_policy,
    peek_policy,
    rate_limit_text,
    refund_balance,
    round_coins,
    should_warn,
)
from utils.group_members import member_label, stats as registry_stats
from utils.membership import (
    clear_membership_cache,
    force_sub_enabled,
    join_bullet,
    missing_required_chats,
    required_chats,
)
from utils.roster_sync import spawn_miss_sync
from utils.target_lookup import (
    MISS_UNKNOWN,
    UNKNOWN_HINT,
    VERDICT_BAD_ID,
    VERDICT_NOT_HERE,
    VERDICT_OK,
    has_started_bot,
    recipient_verdict,
    resolve_target,
)
from utils.whisper_config import get_whisper_config

logger = logging.getLogger(__name__)
router = Router()

# ──────────────────────────────────────────────────
# Callback-data contract
# ──────────────────────────────────────────────────

#: 32 hex chars — a UUID4 in canonical hex form (``uuid4().hex``).
#: ``read_whisper:<id>`` is 12 + 32 = 44 bytes, comfortably inside the
#: 64-byte ``callback_data`` ceiling, and the id fits the existing
#: ``token VARCHAR(32)`` column exactly, so widening the schema is not needed.
#: The new ``read_whisper:`` prefix also keeps this router's callbacks
#: lexically disjoint from ``whisper:help`` / ``whisper:check`` /
#: ``whisper:view:<n>`` owned by ``handlers/whisper.py``.
_WHISPER_ID_RE = "[0-9a-f]{32}"

#: The pre-rename id shape, still accepted for the join callback of rows that
#: were parked by an older build.
_LEGACY_TOKEN_RE = "[0-9a-f]{16}"

_READ_PREFIX = "read_whisper:"
_READ_RE = rf"^{_READ_PREFIX}{_WHISPER_ID_RE}$"

#: Cards published before the rename still carry ``whisper:<16 hex>`` and sit
#: in chats forever. They must keep working, so BOTH shapes are routed to the
#: same handler — the row behind either id lives in the same table.
_LEGACY_READ_RE = rf"^whisper:{_LEGACY_TOKEN_RE}$"

_JOIN_RE = rf"^whisper:join:{_WHISPER_ID_RE}$"

#: The three ACTION callbacks of the card, all sharing the read callback's id
#: shape. Their prefixes are imported from :mod:`keyboards` — the buttons that
#: publish them live there, and a prefix typed twice is a prefix that eventually
#: stops matching. At 16 + 32 bytes ``whisper_options:`` is the longest of the
#: three, still well inside the 64-byte ``callback_data`` ceiling.
_STATS_RE = rf"^{WHISPER_STATS_CB_PREFIX}{_WHISPER_ID_RE}$"
_OPTIONS_RE = rf"^{WHISPER_OPTIONS_CB_PREFIX}{_WHISPER_ID_RE}$"
_DELETE_RE = rf"^{WHISPER_DELETE_CB_PREFIX}{_WHISPER_ID_RE}$"

#: Legacy ``whisper:<16 hex>`` cards predate the action buttons entirely — they
#: were published with the single read button and still only ever carry it. They
#: are therefore deliberately NOT routed to the action callbacks: there is no
#: button on those cards that could fire them.

#: A row minted at inline-query time whose result the sender never picked has
#: no card pointing at it and can never be read — it is unreachable garbage.
#: ``chosen_inline_result`` carries the ``inline_message_id`` of the card it
#: created, so a row still missing it after this grace period was abandoned.
ORPHAN_MINUTES = 60

#: Telegram renders ``callback.answer`` as PLAIN TEXT with no parse mode and
#: refuses anything longer than 200 characters.
MAX_ALERT_LENGTH = 200

#: The error Telegram raises when an edit asks for exactly what the message
#: already says. Matched case-insensitively against the exception text because
#: it is a localised, undocumented string rather than a stable error code — see
#: :func:`_edit_card` for why it counts as success.
_NOT_MODIFIED = "message is not modified"

#: Rows older than this are purged opportunistically — the card referencing
#: them is long gone by then.
RETENTION_DAYS = 7

#: How long :func:`cb_read_whisper` waits for an in-flight send to be
#: activated. The card is live from the moment the bot posts it, a beat before
#: the gates have finished, so a reader who taps in that window finds a
#: reserved-but-inactive row. Polling for a few seconds turns that race into a
#: successful read instead of a wrong answer. A whisper can only be activated
#: once, so the wait ends the moment the anchor handler commits and never runs
#: longer than this cap.
ACTIVATION_WAIT_SECONDS = 5.0
ACTIVATION_POLL_SECONDS = 0.4

#: How long :func:`on_chosen_inline_result` waits for the anchor handler to own
#: up to the pick. The two updates describe ONE action seen from two sides —
#: the anchor is published first, the chosen-result 0.3–2.5s later — so this is
#: not a race to win but a handshake to let finish. Only when the row is still
#: untouched after this cap is the anchor considered lost.
DELIVERY_WAIT_SECONDS = 12.0
DELIVERY_POLL_SECONDS = 0.3

#: Tokens the anchor handler is delivering RIGHT NOW. ``chosen_inline_result``
#: reads it so a pick that is merely in flight is never mistaken for a pick
#: whose anchor never arrived — the difference between "wait" and "give up and
#: tell the sender", which must not be decided by a fixed sleep.
_delivering: set[str] = set()

#: Prefix of the group card, used as a fallback marker so the stub-deleting
#: handler below never removes the card that holds the read button.
_GROUP_CARD_PREFIX = "📩"

#: Prefix of the ANCHOR — the placeholder Telegram publishes from the inline
#: result, which the anchor handler then replaces with the real, bot-authored
#: card. It carries no name, no recipient and no secret: everything the group
#: is allowed to know is in the card the bot posts a moment later.
_ANCHOR_CARD_PREFIX = "⏳"
_ANCHOR_CARD_TEXT = f"{_ANCHOR_CARD_PREFIX} <b>در حال ارسال پیام ناشناس…</b>"

# ──────────────────────────────────────────────────
# Query syntax
# ──────────────────────────────────────────────────

#: Markers that introduce the recipient. ``🆔`` is the one the guide prints and
#: the one users are told to type; the rest are accepted because Telegram
#: clients swap emoji around freely (an old ⌨️ paste, a different keyboard
#: layout) and rejecting a whisper over an equivalent icon helps nobody.
ID_MARKER = "🆔"
TEXT_MARKER = "💬"

#: Accepted synonyms, compared after :func:`_fold` strips the invisible
#: variation selectors clients sprinkle into emoji.
_ID_MARKERS = frozenset({ID_MARKER, "🔢", "🔑", "#️⃣", "🔖"})
_TEXT_MARKERS = frozenset({TEXT_MARKER, "📝", "✉️", "📩", "📄", "🗨️"})

#: U+FE0F variation selector-16 and U+200D zero-width joiner: both are invisible
#: and both change the codepoint sequence of the same emoji.
_INVISIBLE = ("️", "‍")


def _fold(text: str) -> str:
    """Emoji-comparison form: strip the invisible joiners, case-fold."""
    for mark in _INVISIBLE:
        text = text.replace(mark, "")
    return text.casefold()


_FOLDED_ID_MARKERS = frozenset(_fold(m) for m in _ID_MARKERS)
_FOLDED_TEXT_MARKERS = frozenset(_fold(m) for m in _TEXT_MARKERS)


# ──────────────────────────────────────────────────
# Texts
# ──────────────────────────────────────────────────

#: The canonical one-line syntax. Every guide, every rejection and the picker
#: title are built from this single string, so the documented shape and the
#: shape :func:`_split_query` accepts cannot drift apart.
QUERY_EXAMPLE = f"{ID_MARKER} 123456789 {TEXT_MARKER} متن نجوا"

#: Shown when the query is empty or unparsable. It doubles as the "how to
#: use" guide, because a rejected query still has to give the user something
#: tappable instead of an empty list.
#:
#: ``{id}`` / ``{text}`` are filled from :data:`ID_MARKER` / :data:`TEXT_MARKER`
#: rather than pasted in, so the guide can never print a marker the parser does
#: not actually accept.
_USAGE_GUIDE = (
    "🔐 <b>ارسال نجوا</b>\n\n"
    "در هر گروه یا چتی این را تایپ کنید:\n"
    "<code>{bot} {example}</code>\n\n"
    "{id} شناسهٔ عددی یا @یوزرنیم گیرنده — فقط کسی که عضو یکی از "
    "گروه‌های ربات است.\n"
    "{text} متن نجوا.\n\n"
    "در گفتگو فقط یک کارت بدون محتوا می‌نشیند که دکمهٔ "
    "«👁️ مشاهده پیام» دارد و آن را فقط خودِ گیرنده باز می‌کند.\n\n"
    "🔒 متن نجوا هرگز وارد گفتگو نمی‌شود."
)

# ── Deep-linked help ──
# The picker must never message the user itself: a bot may not DM anyone who
# has not started it, so the old «📖 راهنما» card failed for exactly the
# audience that needs a guide most — someone who only ever saw the bot in a
# group. Telegram's own answer is `InlineQueryResultsButton`, which sits ABOVE
# the results and opens `/start <param>` in the bot's own chat. The user presses
# it, Telegram starts the conversation, and only then can anything be said.
#
# ``HELP_START_PARAM`` itself lives in ``keyboards.inline`` — it is baked into
# the button above the results and into the group onboarding card, and handlers
# import ``keyboards``, never the other way round. Re-exported here because
# ``handlers/start.py`` (and the guide texts below) read it from this module.

_HELP_BUTTON_TEXT = "📖 راهنمای ارسال نجوا"

# ── Empty query → the 3-option menu ──
# A bare ``@bot`` used to answer with ONE article: the whisper pattern. That
# taught the syntax and nothing else, in a bot whose other two entry points —
# a whisper addressed to you, and a private anonymous chat — were undiscoverable
# from the picker. The answer is now the menu of :mod:`handlers.inline_menu`,
# which is three articles: the tutorial, "whisper me", and "chat with me
# anonymously". Every other branch of :func:`on_inline_query` is unchanged.

_SENT_OK_GROUP = (
    "✅ <b>نجوای شما ثبت شد!</b>\n\n"
    "گیرنده: {target}\n\n"
    "کارت «👁️ مشاهده پیام» در همان گفتگو منتشر شد و یک اعلان خصوصی هم "
    "برای گیرنده ارسال شد.\n\n"
    "🔒 نام شما روی کارت نمی‌آید و متن فقط چکشی باز می‌شود؛ وقتی طرف مقابل "
    "بخواند، روی کارت گروه «خوانده شد» می‌نشیند."
)

_JOIN_PROMPT = (
    "🔐 <b>عضویت اجباری</b>\n\n"
    "برای فعال شدن نجوا ابتدا در این موارد عضو شوید:\n"
    "{lines}\n\n"
    "پس از عضویت روی «عضو شدم» بزنید تا نجوا فعال شود."
)

_NEUTRALIZED_TEXT = "⛔ <b>نجوا ارسال نشد.</b>"

#: And this is what the sender is told when the DM cannot carry a specific
#: reason — the whisper died before any rule could name the fault.
_SEND_FAILED_DM = (
    "⚠️ <b>نجوا ثبت نشد.</b>\n\n"
    "خطایی پیش آمد و پیام شما ذخیره نشد. لطفاً دوباره تلاش کنید."
)

#: Shown when the send really IS parked: the forced-join requirement is
#: configured AND the sender is still outside it.
_JOIN_PENDING_TEXT = (
    "🔐 این نجوا هنوز فعال نشده است؛ فرستنده باید ابتدا عضویت اجباری "
    "را کامل کند."
)

#: Shown to a reader who taps inside the one-second window between the card
#: appearing and the gates finishing. Not an error and not a refusal — the
#: whisper is on its way in, and the alternative (letting the reader finalise
#: it, which is what this branch used to do) charges the sender twice.
_SENDING_TEXT = "⏳ پیام ناشناس در حال ثبت است؛ لطفاً کمی بعد دوباره تلاش کنید."

#: Every refusal to open a whisper that has no row behind it any more — a send
#: that was rejected after its card went out, or a row a retention sweep took.
#: One string for all of them: they are the same failure with the same remedy,
#: and the copy used to be pasted in three places.
_GONE_ALERT = (
    "⛔️ این نجوا دیگر موجود نیست یا منقضی شده است.\n\n"
    "از فرستنده بخواهید پیام را دوباره بفرستد."
)

# ── The action buttons' answers ──
#
# Four callbacks, four access rules, and one rule underneath all of them: the
# card is public, so EVERY tap is a stranger's tap until the row says otherwise.
# That is why a refusal here is a refusal and not a hint — nobody learns who a
# whisper was for, or that it exists at all, from a card they were not part of.

#: Pressed «👁️ نمایش پیام» while being neither the sender nor the target. The press
#: is also RECORDED on the row (see :func:`_note_snooper`), because a card that
#: silently refuses gives the two parties no way to know it is being fished.
_NO_READ_ACCESS = "🚫 شما اجازه خواندن این نجوا را ندارید!"

#: «📊 آمار» — the snooper list names people, so only the two people it is
#: about may see it. An outsider gets the same answer whether the list is empty
#: or not, which is also what keeps the button from confirming the whisper is
#: real.
_NO_STATS_ACCESS = "🚫 فقط فرستنده و گیرنده به آمار دسترسی دارند"

#: «🗑️ حذف» — sender only. The target is refused even though they can read the
#: message: unsending somebody else's message is not theirs to decide.
_NO_DELETE_ACCESS = "🚫 فقط فرستنده می‌تواند پیام را پاک کند"

#: «⚙️ گزینه‌ها」 — sender and target only, for the same reason as the stats: the
#: card in front of a stranger is not a place to advertise this feature.
_NO_OPTIONS_ACCESS = "🚫 فقط فرستنده و گیرنده به گزینه‌ها دسترسی دارند"

#: What the card becomes once its sender deletes it. The row is gone, so every
#: other button on it is a dead end — the text has to say what happened or the
#: group is left staring at a card that silently stopped working.
_DELETED_CARD_TEXT = "🗑 <b>این نجوا توسط فرستنده حذف شد.</b>"

#: The «📊 آمار」 body. One line per press, newest last, because the order the
#: names appear in is the order the attempts were made.
_SNOOPERS_HEAD = "لیست کسانی که فضولی کردند:"
_SNOOPERS_EMPTY = "هنوز کسی به‌جز فرستنده و گیرنده روی «👁️ نمایش» نزده است."

#: Rejection reasons shown as the (only) inline result, so the spinner always
#: resolves to something tappable instead of an empty list.
_REJECTIONS: dict[str, tuple[str, str]] = {
    "usage": ("ارسال نجوا در هر گفتگو", f"{ID_MARKER} شناسه و {TEXT_MARKER} متن را بنویسید"),
    "syntax": (
        "قالب نوشتن کامل نیست",
        f"دقیقاً به این شکل بنویسید: {ID_MARKER} شناسهٔ گیرنده {TEXT_MARKER} متن نجوا",
    ),
    "malformed": ("شناسهٔ گیرنده نامعتبر است", "یک عدد یا @یوزرنیم بنویسید"),
    "unknown": ("گیرنده پیدا نشد", "این یوزرنیم برای ربات شناخته‌شده نیست"),
    "not_here": (
        "گیرنده در همین گفتگو نیست",
        "کارت در همین گفتگو می‌نشیند، پس او هم باید عضو همین گفتگو باشد",
    ),
    "bad_id": (
        "این آیدی در این گفتگو وجود ندارد",
        "شماره اشتباه است، یا آن کاربر عضو این گفتگو نیست",
    ),
    "self": ("ارسال به خودتان ممکن نیست", "این شناسه متعلق به خود شماست"),
    "bot": ("ارسال به ربات ممکن نیست", "ربات نمی‌تواند گیرندهٔ نجوا باشد"),
    "blocked": (
        "ارسال نجوا به این کاربر ممکن نیست",
        "یکی از طرفین دیگر طرف مقابل را بلاک کرده است",
    ),
    "no_body": ("متن نجوا را بنویسید", f"بعد از شناسه، متن را پشت {TEXT_MARKER} بنویسید"),
    "too_long": ("متن نجوا طولانی است", "حداکثر مجاز: {limit} کاراکتر"),
    # ``{cost}`` is filled in at RENDER time by :func:`_rejection_pair`: the
    # price of a whisper is admin-tunable, so a number baked in here would go
    # stale the first time the panel repriced it.
    "no_balance": (
        "اعتبار کافی نیست",
        "هر نجوا {cost} سکه است؛ موجودی را از «🏆 امتیازات و سکه» پر کن",
    ),
    "disabled": ("قابلیت نجوا غیرفعال است", "ادمین ربات این قابلیت را بسته است"),
    # ── «↩️ پاسخ」 (reply:) ──
    #
    # None of these is a shape rejection, and that is deliberate: they must NOT
    # re-print the 🆔/💬 pattern, because the reply query is not that pattern.
    # Printing it here would tell the user to retype something the button had
    # already filled in correctly.
    "reply_bad_id": (
        "پاسخ نامعتبر است",
        "دکمهٔ «پاسخ» را روی همان کارت نجوا بزنید تا پاسخ درست شروع شود",
    ),
    "reply_gone": (
        "این نجوا دیگر موجود نیست",
        "کارت نجوا پاک شده یا منقضی شده است؛ از فرستنده بخواهید دوباره بفرستد",
    ),
    "reply_denied": (
        "فقط طرفین نجوا می‌توانند پاسخ بدهند",
        "شما فرستنده یا گیرندهٔ این پیام نیستید",
    ),
    "reply_empty": (
        "متن پاسخ را بنویسید",
        "پاسخ خود را همان‌جا که این متن را می‌بینید بنویسید و بفرستید",
    ),
}

#: The rejections that answer "your query has no usable shape" — an empty
#: picker, a malformed pattern, or a target with no message behind it. Only
#: these re-print the syntax in :func:`_rejection_message`; every other
#: refusal already has a complete, actionable answer of its own.
#:
#: The ``reply_*`` keys are the reason this is a whitelist and not "everything
#: except…": an empty REPLY looks like an empty query but the two are answered
#: differently, because the reply's next move is not to type the 🆔/💬 pattern.
_SHAPE_REJECTIONS = frozenset({"usage", "syntax", "no_body"})


def _usage_text(bot_username: str) -> str:
    return _USAGE_GUIDE.format(
        bot=f"@{escape(bot_username)}",
        example=QUERY_EXAMPLE,
        id=ID_MARKER,
        text=TEXT_MARKER,
    )


def _whisper_price_text() -> str:
    """«1 سکه» / «رایگان» — the live price of one whisper.

    Sync on purpose: every text builder that quotes it is sync too, and the
    policy row is cached (see ``utils.economy.peek_policy``), so this costs no
    query and can never disagree with what the balance gate will actually
    charge a moment later.
    """
    cost = round_coins(peek_policy().whisper_cost)
    return "رایگان" if cost <= 0 else f"{fmt_coins(cost)} سکه"


def inline_help_text(bot_username: str) -> str:
    """The ``/inline``, ``/start help`` and main-menu help card.

    Lives next to the query handler so the documented syntax, the length
    ceiling and the bot username can never drift away from what
    :func:`on_inline_query` actually accepts.

    This is what :data:`HELP_START_PARAM` resolves to: the button above the
    inline results opens ``/start help``, so the deep link lands somewhere
    that can actually answer.
    """
    bot = f"@{escape(bot_username or 'bot')}"
    return (
        "🔐 <b>نجوا در هر گفتگو</b>\n\n"
        "در هر گروه یا چتی تایپ کنید:\n"
        f"<code>{bot} {QUERY_EXAMPLE}</code>\n\n"
        f"{ID_MARKER} شناسهٔ گیرنده (عدد یا @یوزرنیم) — فقط عضو گروه‌های ربات.\n"
        f"{TEXT_MARKER} متن نجوا.\n\n"
        "یک کارت با دکمهٔ «👁️ مشاهده پیام» در همان گفتگو می‌نشیند؛ متن "
        "را فقط گیرنده می‌بیند (هشدار خصوصی روی گوشی او).\n\n"
        "🔒 متن نجوا هرگز وارد گفتگو نمی‌شود و فرستنده ناشناس می‌ماند.\n"
        f"🪙 هر نجوا {_whisper_price_text()} است؛ کاربران اشتراکی و معافان رایگان. عضویت اجباری مانند /نجواست."
    )


def _target_mention(_target_id: int, target_name: str, target_username: str) -> str:
    """How the card names the receiver, rendered for the published message.

    The receiver is shown the way the sender can recognise them:
    ``@username`` when there is one, otherwise their first name as PLAIN text.
    Never a ``tg://user?id=…`` link and never the raw numeric id — this string
    lands in the GROUP, where either one would publish the recipient's
    identifier for everyone to copy, and this card is the anonymous delivery
    that is supposed to reveal nothing. Without a resolvable identity the card
    says only that the recipient is a member of the group: the right person
    still recognises the arrival and opens it with the button, because the
    verdict check never depended on the displayed text.
    """
    if target_username:
        return f"@{escape(target_username)}"
    if target_name:
        return escape(target_name)
    return "یکی از اعضای این گروه"


def _target_label(target_username: str, target_name: str) -> str:
    """The same receiver, as PLAIN TEXT — for the picker's title and description.

    Those two render outside the chat, with no parse mode at all, so a mention
    link would be shown to the user as raw ``<a href=…>`` markup. A public
    @username is the best label, the resolved first name the next best, and
    neither carries a numeric id: the screen never shows one, not even in the
    sender's own picker — whatever they typed is still in the query box above
    to confirm with.
    """
    if target_username:
        return f"@{target_username}"
    if target_name:
        return target_name
    return "عضو گروه"


#: The ONE line the whisper card puts into the chat — the secret stays in the
#: row written when the result is chosen, so there is nothing else to print.
#:
#: It keeps the old card's most important property: it names the receiver, so
#: the one person who is allowed to press the button can tell the card is for
#: them. The ``📩`` is :data:`_GROUP_CARD_PREFIX` and is load-bearing, not
#: decoration: it is the marker :func:`_is_published_card` falls back on to
#: recognise this card as content the user asked for rather than a stub it may
#: delete — so the card still survives even if its buttons were ever stripped.
#:
#: The privacy promise itself is not repeated here — it is on the guide, and a
#: card that keeps re-explaining itself is a card people stop reading.
_WHISPER_CARD_TEXT = (
    f"{_GROUP_CARD_PREFIX} یک پیام ناشناس برای {{target}} رسید"
    " — فقط او می‌تواند آن را بخواند"
)

#: Description of the actionable row in the picker. Fixed, because by the time
#: the row is on screen there is nothing left to explain: the recipient is in
#: the title, and the card already carries the button that does the sending.
_WHISPER_RESULT_DESCRIPTION = "🔒 برای ارسال پیام کلیک کنید"


def _whisper_card_text(
    target_id: int, target_name: str, target_username: str
) -> str:
    return _WHISPER_CARD_TEXT.format(
        target=_target_mention(target_id, target_name, target_username)
    )


#: The read receipt — the one line the group gets to see once the person the
#: whisper was written for has actually opened it.
#:
#: This is deliberately NOT a notification to the sender. The sender already
#: knows the instant the alert modal appears on the recipient's screen, because
#: their phone buzzes; what the GROUP cannot know is whether the message was ever
#: collected, and an unread card sitting in a chat forever is exactly the doubt
#: this line removes. So it goes on the card, in public, in the past tense —
#: «خوانده شد» says the event is over, and nobody has to keep wondering.
#:
#: The ✅ is ``keyboards.inline.ICON_CONFIRM`` — the same icon every other
#: confirmation in the bot wears: this line is a confirmation, so it wears the
#: confirmation's icon and the eye finds it in a chat full of cards.
_READ_RECEIPT_LINE = "✅ این نجوا توسط گیرنده خوانده شد."


def _card_text_after_read(row) -> str:
    """The whisper card as it looks once the recipient has read it.

    Rebuilt from the row rather than read back from Telegram, because an
    inline-mode card arrives at the callback with no ``Message`` attached — there
    is nothing to read the current text from, and ``editMessageText`` insists on
    the full new value. The inputs are the ones the card was published with (see
    :func:`_deliver_in_place`), so the result is the original line plus the
    receipt and nothing else.
    """
    base = _whisper_card_text(row.target_id, _user_name(row.target_id), "")
    return f"{base}\n\n{_READ_RECEIPT_LINE}"


# ── The private notice ────────────────────────────────────────────────────
#
# A whisper does NOT normally tell the recipient anything privately. The card in
# the group is the whole delivery: it names them, and they open the message from
# it. A «you have an anonymous message» DM on top of that was a second copy of
# the same news in the one chat a person reads most, for no gain.
#
# One case still needs it. The ``/نجوا`` road posts its own card, so that send
# can fail on permissions — and when it does, a committed row with no card has no
# route to its reader at all. :mod:`handlers.whisper` therefore keeps this notice
# as a genuine fallback, and imports it from here.
#
# It used to be sent on both roads and defined on both, which is how the two
# drifted into explaining the same alert differently to the same person; one
# definition, imported, cannot drift. The inline side keeps ownership only
# because it is the older of the two — :data:`_READ_RECEIPT_LINE` already works
# this way, and moving the strings was not worth churning every import for.
#
# What it must do, in order:
#   * say there IS a message, in the second person, because this lands in the
#     recipient's own chat with no context — «یک پیام ناشناس برای X ارسال شد»
#     is written for a group audience and means nothing here;
#   * say where it came from, so a message from a group they are sitting in is
#     recognisable rather than looking like spam from the bot;
#   * restate the promise ONCE, because the whole feature rests on it and this
#     is the only screen where the recipient reads anything;
#   * point at the button, so the alert is obviously actionable;
#   * mention the anonymous chat, because a one-way whisper is a dead end and
#     the reply path is what turns a reader into a conversation. It goes LAST:
#     it is the optional extra, and anything after the actionable line reads as
#     an afterthought.
_NOTICE_TEXT = (
    "{eye} <b>یک پیام مخفی برای شماست</b>\n\n"
    "در گروه «{chat}» برای شما نجوا نوشته‌اند.\n\n"
    "🔒 فرستنده ناشناس است؛ متن فقط در گفتگوی خصوصی خودتان باز می‌شود و "
    "هیچ‌کس دیگری آن را نمی‌بیند.\n\n"
    "{eye} روی «مشاهده پیام» بزنید تا متن را بخوانید.\n\n"
    "💬 خواستید جواب بدهید؟ کارت «درخواست پیام ناشناس» را در همان گروه "
    "بفرستید؛ اگر طرف مقابل بپذیرد، ناشناس با هم چت می‌کنید."
)


def whisper_notice_text(chat_title: str) -> str:
    """The private «you have a secret message» alert a whisper starts with.

    Args:
        chat_title: the group the whisper was sent in, shown so the alert is
            recognisable. Escaped here because this is the one place the title
            arrives from user-controlled data on a path with no other escaping.
    """
    return _NOTICE_TEXT.format(chat=escape(chat_title or "یک گروه"), eye="👁️")


#: What that alert becomes once the message has actually been opened.
#:
#: The notice is the one thing in this flow that lives permanently in the
#: recipient's chat, so leaving it reading «you have a secret message» after the
#: message is gone would be a permanent lie — they would keep tapping a button
#: that shows them the same text again. It therefore turns into its own receipt,
#: mirroring what the group card does (:data:`_READ_RECEIPT_LINE`), and the
#: button is dropped along with it: the secret has already been delivered, and
#: the card in the group remains the way back to it.
_NOTICE_READ_TEXT = (
    "✅ <b>پیام خوانده شد</b>\n\n"
    "متن این پیام مخفی باز شد و فرستنده برای همیشه ناشناس می‌ماند.\n\n"
    "💬 برای جواب دادن، کارت «درخواست پیام ناشناس» را در همان گروه بفرستید."
)

#: Sent to the SENDER when the notice could not be delivered.
#:
#: The failure is not the bot's and not the sender's, so the wording must not
#: read like one: it names the actual cause (Telegram refuses to message
#: somebody who never started the bot) and gives the one action that fixes it.
#: Crucially it does NOT claim the whisper was lost — the row exists and the
#: card is live, so the message is waiting and readable the moment they start
#: the bot. Telling the sender it failed would send them to re-send a message
#: that is already delivered, which is how you get the same whisper twice.
_TARGET_NOT_STARTED = (
    "⚠️ <b>گیرنده ربات را استارت نکرده است</b>\n\n"
    "{target} هنوز این ربات را استارت نکرده، برای همین اعلان خصوصی به او نرسید "
    "و دکمهٔ «مشاهده پیام» در چت خودش باز نمی‌شود.\n\n"
    "از او بخواهید یک‌بار ربات را استارت کند؛ همین نجوا باقی است و بعد از "
    "استارت کردن، دکمهٔ کارت گروه برایش کار می‌کند.\n\n"
    "🔒 هویت شما همچنان ناشناس مانده است."
)


def target_not_started_text(target_label: str) -> str:
    """:data:`_TARGET_NOT_STARTED` with the recipient's name filled in."""
    return _TARGET_NOT_STARTED.format(target=escape(target_label or "گیرنده"))


#: Reasons :func:`_split_query` can refuse a query.
_PARSE_EMPTY = "empty"
_PARSE_SYNTAX = "syntax"
_PARSE_NO_BODY = "no_body"


def _looks_like_recipient(token: str) -> bool:
    """True when ``token`` is shaped like an id or an ``@username``.

    The same test :func:`_lookup_target` applies before it touches the roster,
    so "is this a recipient" is decided by ONE rule and the parser can never
    hand the resolver something it will refuse.
    """
    text = (token or "").strip()
    if not text:
        return False
    if text.startswith("@") and len(text) > 1:
        return True
    return text.lstrip("-").isdigit()


def _split_query(raw: str) -> tuple[str, str, str]:
    """``"@Sara راز ماست"`` → ``("@Sara", "راز ماست", "")``.

    Two families are accepted, and between them they cover every order a person
    would type:

    ``🆔 <id> 💬 <secret>``
        The marked form. The markers are structural, not decoration: they are
        what tells the user which half is the recipient and which is the message,
        and they make the shape readable in the picker instead of something to
        memorise. Unambiguous — the recipient may itself contain digits.

    ``<id> <secret>`` / ``<secret> <id>``
        The marker-less forms. Both exist because they are literally what the
        two buttons of the inline menu type into the picker, and a button that
        filled in a query the parser rejects is the worst kind of tutorial — the
        user presses it, sees the syntax again, and learns nothing. Position
        carries the meaning here, so the secret may contain digits, spaces and
        punctuation freely; see :func:`_split_marked_less` for what happens when
        both ends look like a recipient.

    Everything the secret is made of is rejoined with single spaces: the user's
    own whitespace is not worth rejecting a whisper over, and rejoining keeps
    the payload byte-identical to what the guide showed.
    """
    tokens = (raw or "").split()
    if not tokens:
        return "", "", _PARSE_EMPTY

    if _fold(tokens[0]) not in _FOLDED_ID_MARKERS:
        # Marker-less form (or something we cannot read at all).
        return _split_marked_less(tokens)

    if len(tokens) < 2:
        return "", "", _PARSE_SYNTAX

    id_token = tokens[1]
    if _fold(id_token) in _FOLDED_ID_MARKERS or _fold(id_token) in _FOLDED_TEXT_MARKERS:
        # Two markers in a row: the id is missing.
        return "", "", _PARSE_SYNTAX

    if len(tokens) < 3 or _fold(tokens[2]) not in _FOLDED_TEXT_MARKERS:
        return id_token, "", _PARSE_SYNTAX

    secret = " ".join(tokens[3:]).strip()
    if not secret:
        return id_token, "", _PARSE_NO_BODY
    return id_token, secret, ""


def _split_marked_less(tokens: list[str]) -> tuple[str, str, str]:
    """The marker-less shapes → ``(id_token, secret, "")``.

        ``<secret> <id>``    the tutorial card teaches this (bot, message,
                            recipient), and it is also what the «➡️ ارسال به …»
                            button pre-fills into the picker
        ``<id> <secret>``    recipient first — ``@Sara راز ماست``
        ``<id>``             nothing left to send

    A single token can never be a whisper, so it is refused rather than
    half-parsed: with one token there is nothing left for the message, and
    guessing (``"@Sara"`` → target ``@Sara``, body ``@Sara``) would publish a
    card addressed to a stranger carrying the sender's own words as the secret.

    Which end wins when BOTH ends look like a recipient
    ---------------------------------------------------
    ``راز 12 به @Sara`` and ``@Sara جلسه 12`` are both well-formed readings of a
    query whose first and last tokens are recipient-shaped, and no rule can tell
    them apart — the two forms genuinely overlap. The recipient-**last** reading
    wins, because that is the order the tutorial card teaches and therefore the
    one already in users' fingers; flipping the preference would silently
    mis-address every message whose body happens to begin with a number.

    The cost is the mirror-image corner (``@Sara 12``, where the user meant
    recipient-first and the message ends in a bare number): it is addressed to
    user 12. That is why every target still has to clear
    :func:`_lookup_target` — group membership, the block list, and the recipient
    having to press the button before the text is shown — before a single word
    of the secret is visible to anyone.
    """
    if len(tokens) < 2:
        return "", "", _PARSE_SYNTAX

    # Recipient last, then recipient first, then give up. Never "search" for a
    # recipient somewhere in the middle: that would let a secret containing a
    # bare number be silently addressed to whoever owns that number.
    if _looks_like_recipient(tokens[-1]):
        id_token, body = tokens[-1], tokens[:-1]
    elif _looks_like_recipient(tokens[0]):
        id_token, body = tokens[0], tokens[1:]
    else:
        return "", "", _PARSE_SYNTAX

    secret = " ".join(body).strip()
    if not secret:
        return "", "", _PARSE_SYNTAX
    return id_token, secret, ""


def _parse_result_id(result_id: str) -> tuple[str, str]:
    """``w-<token>`` → ``(token, "group")``. ``("", "")`` for anything else.

    Only the whisper card mints a real id. The placeholder result and every
    rejection reuse the ``w-disabled-*`` prefix, which matches no token shape —
    so they resolve to ``("", "")`` here and their (empty) message is simply
    deleted. There is no orphan reservation to sweep, because a card that
    stores nothing mints no row.
    """
    prefix, sep, rest = (result_id or "").partition("-")
    if not sep or not re.fullmatch(_WHISPER_ID_RE, rest):
        return "", ""
    if prefix == "w":
        return rest, "group"
    return "", ""


# ── «↩️ پاسخ» — the reply query ──
#
# The card's reply button is a ``switch_inline_query_current_chat``, so what
# lands in the picker is ``reply:<32-hex whisper id> `` with the cursor parked
# after the space and the user typing the answer. Everything below reads that
# one shape.


def _is_reply_query(raw_query: str) -> bool:
    """True when this query came from the card's «↩️ پاسخ」 button.

    The menu branch in :func:`on_inline_query` is guarded by this, so a reply
    the user has not typed into yet is still answered as a reply (with the
    "write the text" refusal) instead of dropping them into the tutorial menu,
    while a picker they cleared completely still falls back to the menu.
    """
    return (raw_query or "").startswith(WHISPER_REPLY_QUERY_PREFIX)


def _split_reply_query(raw_query: str) -> tuple[str, str, str]:
    """``reply:<id> متن پاسخ`` → ``(whisper_id, secret, reject_key)``.

    Split at a FIXED offset rather than by shape, which is the one thing this
    parser must not do: the text after the id is the user's own words, so it may
    contain digits, spaces and punctuation freely, and looking for "the part
    that looks like a token" would swallow any message that happens to contain a
    32-hex run. The id is exactly :data:`_WHISPER_ID_RE` characters wide — that
    is why ids are minted as 32 hex chars and the column is ``VARCHAR(32)`` — so
    the cut is unambiguous.

    The one shape check is the character right after the id: it has to be
    whitespace or nothing at all, or the id was followed by a word the button
    never typed and the whole query is not one of ours.
    """
    rest = (raw_query or "")[len(WHISPER_REPLY_QUERY_PREFIX) :]
    whisper_id, tail = rest[:32], rest[32:]
    if not re.fullmatch(_WHISPER_ID_RE, whisper_id):
        return "", "", "reply_bad_id"
    if tail and not tail[0].isspace():
        return "", "", "reply_bad_id"
    secret = tail.strip()
    if not secret:
        return whisper_id, "", "reply_empty"
    return whisper_id, secret, ""


def join_prompt_text(missing) -> str:
    """The forced-join prompt body, from the chats the user still misses.

    Each line carries the channel's @handle too (``join_bullet``), so the ID
    is readable and copyable from the message itself when the URL button
    will not open.
    """
    lines = "\n".join(join_bullet(chat) for chat in missing)
    return _JOIN_PROMPT.format(lines=lines)


#: Internal alias — the prompt is used on several paths inside this module and
#: the public name exists for `handlers.start`.
_join_prompt_text = join_prompt_text


# ──────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────

async def _send_dm(bot: Bot, user_id: int, text: str, kb=None) -> bool:
    """Send a private message. Returns False when the user blocked the bot."""
    sent = await _dm_message(bot, user_id, text, kb)
    return sent is not None


async def _dm_message(bot: Bot, user_id: int, text: str, kb=None) -> Message | None:
    """Send one private message and hand the message back, or ``None``.

    :func:`_send_dm` only reports success, which is all a caller needs when the
    content is a status line. The whisper notice is different: its message id
    has to survive until the recipient opens the whisper, because that is what
    lets the notice be EDITED into «پیام خوانده شد» instead of sitting on their
    phone for ever claiming they still have an unread message. Hence a second
    entry point rather than changing the return type under every other caller.
    """
    try:
        return await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception as exc:
        logger.info("Inline whisper DM to %s failed: %s", user_id, exc)
        return None


#: Logged once, not per keystroke: the miss path fires on every query while a
#: reference stays unresolvable, and a warning per character typed would bury the
#: one line that matters.
_roster_warning_done = False


async def _lookup_target(raw: str, bot: Bot | None = None) -> tuple[int, str, str, str]:
    """Resolve the ``🆔`` token to ``(telegram_id, first_name, username, error)``.

    A thin adapter over :func:`utils.target_lookup.resolve_target`, which owns the
    whole resolution policy. Two things changed here and both were reported as
    "the bot is broken":

    * this used to be an *either/or* — roster if it happened to be populated,
      ``users`` table if not — so a member of the very group the whisper was typed
      in could be told they were in none of the bot's groups;
    * a numeric id nobody recognises used to be refused outright. It is not
      refused any more: the card is revealed through a callback alert that only
      the tapper's client renders, so the recipient never has to have started
      the bot, and whether such a person is really in the group is settled
      authoritatively at the landing point by :func:`_target_in_chat` — before
      the sender is charged.

    A genuine miss (``@username`` nobody has ever mentioned) still schedules one
    throttled re-scan (:func:`utils.roster_sync.spawn_miss_sync`) and still
    refuses, because a handle with no relationship to the bot cannot be turned
    into a user id at all.
    """
    global _roster_warning_done

    lookup = await resolve_target(bot, raw)
    if lookup.found:
        return lookup.user_id, lookup.first_name, lookup.username, ""

    if lookup.miss == MISS_UNKNOWN:
        if bot is not None:
            spawn_miss_sync(bot)
        if not _roster_warning_done:
            _roster_warning_done = True
            logger.info(
                "Whisper target %r is unknown to every source. The roster holds "
                "%s member(s) across %s group(s) — promote the bot in a group to "
                "widen it (getChatAdministrators needs admin rights).",
                raw,
                registry_stats()["members"],
                registry_stats()["chats"],
            )
        return 0, "", "", MISS_UNKNOWN

    # The shape misses — ``@`` on its own, or a token that is neither a number
    # nor a handle. Four values, like every other return here: this used to hand
    # back five and raised ``ValueError`` on unpack for a query the picker
    # reaches on every keystroke of a half-typed handle.
    return 0, "", "", lookup.miss


# ──────────────────────────────────────────────────
# Memoisation for the checks that DO need the database
#
# The block test is the remaining query on the typing path, and it is expensive
# precisely because admins send constantly: one picker session repeats the same
# pair dozens of times. The answer is therefore cached in-process — including
# the negative one, which is the common case — and re-checked authoritatively
# when the result is actually chosen, so a stale cache can never let anything
# through.
#
# The old 24-hour send counter used to sit beside it and needed a cache of its
# own. The balance replaced it with a single column on a row that is already
# loaded, so there is nothing left here to memoise.
# ──────────────────────────────────────────────────

_BLOCK_CACHE_TTL = 120.0

_block_cache: dict[tuple[int, int], tuple[bool, float]] = {}


async def _are_blocked(a: int, b: int, *, authoritative: bool = False) -> bool:
    """True when either side blocked the other.

    ``authoritative=True`` skips the cache; only the chosen-result handler uses
    it, and only so the commit is decided on fresh data.
    """
    key = (a, b) if a < b else (b, a)
    now = time.monotonic()
    if not authoritative:
        hit = _block_cache.get(key)
        if hit is not None and (now - hit[1]) < _BLOCK_CACHE_TTL:
            return hit[0]

    async with async_session_factory() as session:
        result = await session.execute(
            select(BlockList)
            .where(
                ((BlockList.blocker_id == a) & (BlockList.blocked_id == b))
                | ((BlockList.blocker_id == b) & (BlockList.blocked_id == a))
            )
            .limit(1)
        )
        blocked = result.scalar_one_or_none() is not None

    _block_cache[key] = (blocked, now)
    return blocked


async def _precheck(
    bot: Bot, sender_id: int, raw_query: str, config, *, authoritative: bool = False
) -> tuple[int, str, str, str, str]:
    """Validate a raw inline query.

    Returns ``(target_id, target_name, target_username, secret, reject_key)``
    — ``reject_key`` is empty on success and otherwise a key of
    :data:`_REJECTIONS`. Everything checked here is side-effect free, so it
    is safe to run while the picker is still open. The checks that cost the
    sender something (forced-join, the coin, tokens, rate limit) happen only
    when the result is actually chosen.

    ``authoritative=True`` (set by :func:`_handle_chosen`) is what turns off
    the memoised answers, so the picker is fast and the commit is exact. The
    balance is deliberately NOT memoised and not re-checked here either: it is
    a column on a row that is already loaded, so it costs nothing to read fresh,
    and reading it fresh is the whole point — a stale verdict is what let people
    send a whisper they could not pay for.
    """
    token, secret, parse_error = _split_query(raw_query)
    if parse_error == _PARSE_EMPTY or not token:
        return 0, "", "", secret, "usage"
    if parse_error == _PARSE_SYNTAX:
        return 0, "", "", secret, "syntax"

    target_id, target_name, target_username, error = await _lookup_target(token, bot)
    if error in ("empty", "malformed"):
        return 0, "", "", secret, "malformed"
    if error == "unknown":
        return 0, "", "", secret, "unknown"
    if target_id == sender_id:
        return 0, "", "", secret, "self"
    if target_id == bot.id:
        return 0, "", "", secret, "bot"
    if await _are_blocked(sender_id, target_id, authoritative=authoritative):
        return 0, "", "", secret, "blocked"
    if not secret:
        return target_id, target_name, target_username, secret, "no_body"
    if len(secret) > (config.max_length or 700):
        return target_id, target_name, target_username, secret, "too_long"
    # The balance, not a counter. Read-only here (:func:`can_afford_whisper`) on
    # purpose: the picker may open a query the user never sends, and charging a
    # coin for it would be indefensible. The charge happens in
    # :func:`_run_send_gates`, which only ever runs once a send is real.
    affordable, _ = await can_afford_whisper(sender_id)
    if not affordable:
        return target_id, target_name, target_username, secret, "no_balance"
    return target_id, target_name, target_username, secret, ""


def _rejection_pair(reject: str, config) -> tuple[str, str]:
    """``(title, description)`` with the placeholders filled in.

    Two values are interpolated here rather than at import time: the length
    ceiling lives on the whisper config, and the whisper price lives on
    ``bot_policy`` — both are admin-editable, so quoting them once at module
    load would freeze the first answer they ever had.
    """
    title, description = _REJECTIONS[reject]
    if "{limit}" in description or "{cost}" in description:
        cost = round_coins(peek_policy().whisper_cost)
        description = description.format(
            limit=config.max_length or 700,
            # A refusal can only happen when there IS a price — a free whisper
            # is affordable by definition — so the fallback is never shown.
            cost=(f"{fmt_coins(cost)} سکه" if cost > 0 else "رایگان"),
        )
    return title, description


def _rejection_message(reject: str, bot_username: str, config) -> str:
    """The card body of a refused query: what was wrong, plus the syntax.

    The syntax is re-printed for SHAPE problems only — an empty query, a
    malformed one, or one with no message in it. Those are the three cases
    where the user's next move is to type something different, and re-printing
    the pattern is the answer.

    Every other refusal — recipient not found, blocked, daily cap hit, feature
    switched off by an admin — leaves the query alone. Appending the pattern
    there told people who had already typed it correctly that they had typed it
    wrongly, which is both noise and actively misleading: the picker showing
    «🔒 ارسال نجوا در هر گفتگو / 🆔 شناسه و 💬 متن را بنویسید» to someone whose
    only problem was a full quota is what made a working query look broken.
    """
    title, description = _rejection_pair(reject, config)
    head = f"⚠️ <b>{escape(title)}</b>\n{escape(description)}"
    if reject == "unknown":
        # The only rejection whose one-line description is not the whole answer.
        # "Not found in any of the bot's groups" used to be printed here, which
        # blamed group membership for a failure that is far more often a typo or
        # a recipient who never started the bot — and the two are
        # indistinguishable over the Bot API, so both have to be named.
        return f"{head}\n{escape(UNKNOWN_HINT)}"
    if reject not in _SHAPE_REJECTIONS:
        return head
    return f"{head}\n\n{_usage_text(bot_username)}"


# ──────────────────────────────────────────────────
# 1. The inline query
# ──────────────────────────────────────────────────

def _article(
    result_id: str,
    title: str,
    description: str,
    message_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> InlineQueryResultArticle:
    """Build the article, attaching the configured thumbnail when there is one.

    ``thumbnail_url`` is only set when ``INLINE_THUMBNAIL_URL`` is filled in:
    the Bot API validates the URL, and an empty string is a hard error, so the
    field has to be omitted rather than blanked.
    """
    article = InlineQueryResultArticle(
        id=result_id,
        title=title[:256],
        description=description[:256],
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
        ),
        reply_markup=reply_markup,
    )
    thumbnail = settings.inline_thumbnail_url.strip()
    if thumbnail:
        article.thumbnail_url = thumbnail
        article.thumbnail_width = 64
        article.thumbnail_height = 64
    return article


async def _reserve_row(
    *,
    whisper_id: str,
    sender_id: int,
    target_id: int,
    secret: str,
) -> None:
    """Commit ``{whisper_id: {sender, target, secret}}`` BEFORE we answer.

    This is the fix for «این نجوا دیگر موجود نیست». The card is published by
    Telegram the instant the sender taps a result, while the
    ``chosen_inline_result`` update that used to write the row was still in
    flight (0.3–2.5s in the logs). Anything the reader did in that window hit
    a table that did not hold the id yet, and the row was then written only to
    be found, or missed, at a moment the sender could do nothing about.

    Persisting here — ahead of ``inline_query.answer()`` — means the row is
    already committed when the button becomes reachable in any chat. There is
    no window left to lose.

    The row lands INACTIVE on purpose: it must exist to back the button, but it
    must not be readable (nor charge anyone) before the chosen-result handler
    has re-validated the send and passed the forced-join and economy gates.
    """
    async with async_session_factory() as session:
        row = await session.scalar(
            select(InlineWhisper).where(InlineWhisper.token == whisper_id)
        )
        if row is None:
            session.add(
                InlineWhisper(
                    token=whisper_id,
                    sender_id=sender_id,
                    target_id=target_id,
                    secret_text=secret,
                    delivery="group",
                    inline_message_id=None,
                    is_active=False,
                )
            )
        else:
            row.sender_id = sender_id
            row.target_id = target_id
            row.secret_text = secret
            row.is_active = False
        await session.commit()


async def _reply_target(whisper_id: str, sender_id: int) -> tuple[int, str]:
    """``(opposite_party_id, reject_key)`` for a reply to ``whisper_id``.

    Answers the one question the card's shared markup cannot: «who am I talking
    back to?». The target answers the sender and the sender answers the target,
    the button is identical for both, and the row is the only thing that knows —
    which is why the reply query carries the whisper id and NOT a recipient, and
    why the identity has to be read from ``inline_query.from_user`` here instead
    of being baked into the button.

    The parties are checked against the row rather than trusted. The id travels
    inside a query the user can edit, copy and forward, so a token found in a
    screenshot or a paste would otherwise be a standing invitation to send
    unsolicited whispers to the two people behind it — with their ids handed
    over, which the picker never otherwise gives away. The check costs one
    indexed row, and the refusal says nothing the card does not already say in
    the group it was published in.
    """
    row = await _get_row(whisper_id)
    if row is None:
        return 0, "reply_gone"
    if sender_id == row.target_id:
        return row.sender_id, ""
    if sender_id == row.sender_id:
        return row.target_id, ""
    return 0, "reply_denied"


async def _whisper_results(
    bot: Bot,
    inline_query: InlineQuery,
    raw_query: str,
    config,
    bot_username: str,
) -> list[InlineQueryResultArticle]:
    """The ONE article the picker answers a whisper query with — send or refusal.

    Both query families end up here: the plain ``🆔 <id> 💬 <text>`` one, and the
    ``reply:<id> …`` one the card's «↩️ پاسخ» button starts. A reply is not given
    a send path of its own — it is rewritten into the marked form addressed to
    the OPPOSITE party and then put through the very same
    :func:`_precheck`, so every rule that can refuse a whisper refuses a reply
    too (block list, sending to yourself, the length ceiling, the daily cap) and
    there is exactly one place where those rules are written down.

    The two failure modes of that sharing are worth naming. A reply inherits
    refusals the user did not cause — «سقف روزانهٔ نجوا تکمیل شد» is about them,
    not about the card — which is why those articles say what happened instead of
    re-printing a syntax (see :func:`_rejection_message`). And it inherits the
    row-level gates: the reply is a NEW whisper from the replier to the other
    party, not an edit of the old one, so it is charged, quota-counted and
    published as a card of its own.
    """
    sender_id = inline_query.from_user.id

    reject = ""
    if _is_reply_query(raw_query):
        reply_token, reply_secret, reject = _split_reply_query(raw_query)
        if not reject:
            opposite_id, reject = await _reply_target(reply_token, sender_id)
            if not reject:
                # The marked form, not a marker-less one: the recipient here is a
                # bare numeric id followed by text the user wrote, and the
                # marker-less parser reads the last word of a reply ending in a
                # number as the recipient.
                raw_query = f"{ID_MARKER} {opposite_id} {TEXT_MARKER} {reply_secret}"

    if reject:
        target_id, target_name, target_username, secret = 0, "", "", ""
    else:
        target_id, target_name, target_username, secret, reject = await _precheck(
            bot, sender_id, raw_query, config
        )

    if reject:
        title, description = _rejection_pair(reject, config)
        return [
            _article(
                result_id=f"w-disabled-{reject}",
                title=title,
                description=description,
                message_text=_rejection_message(reject, bot_username, config),
            )
        ]

    whisper_id = uuid4().hex
    # BEFORE the answer — see _reserve_row.
    await _reserve_row(
        whisper_id=whisper_id,
        sender_id=sender_id,
        target_id=target_id,
        secret=secret,
    )
    # The ANCHOR, not the card: see the module docstring. Its only job is
    # to exist in the conversation the sender picked it in — Telegram hands that
    # conversation to us with ``chat.id`` on the message update, which is the one
    # thing ``inline_query`` itself never carries. The read button rides along so
    # the token comes back with the anchor, and so a reader who taps inside the
    # one-second window before the bot replaces it simply waits and then reads.
    return [
        _article(
            result_id=f"w-{whisper_id}",
            title=f"📬 ارسال نجوا به {_target_label(target_username, target_name)}",
            description=_WHISPER_RESULT_DESCRIPTION,
            message_text=_ANCHOR_CARD_TEXT,
            reply_markup=whisper_read_kb(whisper_id),
        )
    ]


# ──────────────────────────────────────────────────
# 3. Debounce — one answer per keystroke burst, not per keystroke
# ──────────────────────────────────────────────────
#
# Telegram fires a fresh ``inline_query`` on every single character, and every
# one of them used to run the whole pipeline: a bot-profile lookup, a block test,
# a balance read and a result reservation. Typing a twelve-character handle
# therefore did twelve lookups, and eleven of them were for text the user had
# already replaced.
#
# The fix is to let the LAST query of a burst do the work. Each arrival takes a
# sequence number for its user and waits; when it wakes up, a newer number
# means somebody else already spoke for this user and this update has nothing
# left to contribute — so it returns without touching the database at all.
# The newest query waits the full window and then answers normally, which is why
# the delay is invisible: it lands exactly when the user stopped typing.
#
# 0.8s is chosen to sit just past a normal inter-keystroke gap and well under
# the ~1s pause people actually take to think about the next word. Waiting
# longer would feel laggy; waiting less would let a fast typist's mid-word
# queries through.

#: How long the last query of a burst waits before it is allowed to answer.
DEBOUNCE_SECONDS = 0.8

#: ``user_id -> sequence number of the most recent query seen for them``.
#:
#: Only ever read and written from inside :func:`_debounce`, and both operations
#: happen without an ``await`` between them, so the single-threaded event loop
#: makes this atomic. That is what lets the counter be a bare dict instead of a
#: lock: nothing can interleave between the check and the write.
_debounce_seq: dict[int, int] = {}


async def _debounce(user_id: int) -> bool:
    """Claim the right to answer for ``user_id``. ``True`` means "you are current".

    Takes a sequence number, waits out the window, then reports whether a newer
    query for the same user turned up in the meantime.

    ``True`` for the last arrival of a burst, ``False`` for every arrival that
    was overtaken while waiting. Callers must treat ``False`` as "answer nothing
    at all" — not "answer something small" — because whatever the overtaken
    query would have said is, by construction, about text the user no longer
    has in the box.

    The entry is dropped when this query wins, so the map holds one int per user
    who has a query in flight rather than one per user who has ever opened the
    picker. Losing queries deliberately leave theirs behind: the winner is the
    only one that reads the entry, and it has to find it.
    """
    seq = _debounce_seq.get(user_id, 0) + 1
    _debounce_seq[user_id] = seq

    await asyncio.sleep(DEBOUNCE_SECONDS)

    # ``!=`` and not ``==``: the counter only ever grows, so any difference at
    # all means somebody newer exists.
    if _debounce_seq.get(user_id) != seq:
        return False
    _debounce_seq.pop(user_id, None)
    return True


@router.inline_query()
async def on_inline_query(inline_query: InlineQuery, bot: Bot) -> None:
    """Answer the inline picker: the menu, or ONE actionable whisper row.

    Four shapes of query, three answers:

    * nothing typed at all → the three-option menu of
      :mod:`handlers.inline_menu` (tutorial / whisper me / anonymous chat),
    * ``@bot 🆔 <id> 💬 <secret>``, or any marker-less order of
      ``<id>`` and ``<secret>`` → ONE row that posts the ANCHOR, which the
      bot then replaces with the real card (see :func:`_handle_anchor`),
    * ``reply:<whisper_id> <text>``, the shape the card's «↩️ پاسخ» button types —
      the same answer, addressed to the opposite party (see :func:`_whisper_results`),
    * a query we cannot read, or that the gates refuse → ONE article saying
      which rule was broken. The syntax is re-printed ONLY for the first two of
      those (see :func:`_rejection_message`).

    A result is ALWAYS returned (Telegram shows a spinner forever otherwise), and
    the guide is deliberately NOT what a typed query falls back to: the picker
    used to answer every malformed query with the usage guide, so typing
    ``@bot @Sara راز ماست`` — the most natural thing in the world — printed
    "here is how to type a query" and taught the user nothing. A query that
    names a recipient and a message IS the send; it gets the send button.

    The whisper id is minted by :func:`_whisper_results` because the read button
    has to travel inside the result — and the row is committed there too,
    *before* the answer goes out, so the button can never point at an id the
    database has not heard of yet.

    **This handler never messages the user.** A bot cannot DM anyone who has
    not started it, and the people who need a guide most are exactly the ones
    who have only ever seen the bot inside a group. Every route out of the
    picker therefore goes through Telegram's own affordances instead of
    ``bot.send_message``:

    * ``button=`` puts «📖 راهنمای ارسال نجوا» above the results; Telegram
      opens ``/start help`` in the bot's own chat, which starts the
      conversation and hands us a user we are allowed to write to.
    * an empty query answers with the menu, whose second row is the syntax —
      the instructions are visible without a single tap.

    (The one message this module does post into a chat is the card itself, and
    it is posted from the ``message`` update of the anchor — see
    :func:`_handle_anchor`.)

    **Debounced.** Telegram raises one of these per keystroke, so :func:`_debounce`
    lets only the last query of a burst reach the code below; the overtaken ones
    return before the first database call. The empty query skips the wait.
    """
    sender_id = inline_query.from_user.id

    # Debounce FIRST, before the config read, the bot profile and the target
    # lookup. Everything below this line is a database round trip, and eleven of
    # the twelve queries in a burst would be for text the user has already
    # replaced — so the overtaken ones must not run any of it.
    #
    # The bare ``@bot`` menu is exempt from the wait, and deliberately so: it is
    # the answer to "what is this thing", the one question where being half a
    # second slower to appear is the whole difference between a bot that feels
    # instant and one that feels broken, and it costs nothing to answer.
    raw_query = inline_query.query or ""
    if raw_query.strip() and not await _debounce(sender_id):
        # A newer query for this user is already on its way and will answer.
        # Returning WITHOUT calling ``answer`` is what makes this a debounce
        # rather than a delay: Telegram drops this update's spinner, and the one
        # update that does answer owns the results.
        return

    bot_username = (await bot.me()).username or "bot"
    config = await get_whisper_config()

    # A bare ``@bot`` is "what is this thing and what can it do" — answered
    # with the menu. Anything else goes through the whisper parser and, if it
    # does not parse, through the rejection article.
    #
    # The reply query is matched on its PREFIX rather than on being non-empty,
    # which is what keeps «↩️ پاسخ» out of the menu branch: the button leaves a
    # bare ``reply:<id> `` in the box, and by the time the user deletes the
    # prefilled id to start over what is left is a query about a reply.
    if not raw_query.strip() and not _is_reply_query(raw_query):
        results = build_menu_results(
            bot_username=bot_username,
            user=inline_query.from_user,
            # «📤 ارسال نجوا به من» must open the picker on a query the parser
            # accepts, so it is built here — where the syntax lives — with the
            # recipient already filled in and nothing else typed.
            whisper_prefill=f"{ID_MARKER} {sender_id} {TEXT_MARKER} ",
            recipient_ref=(
                f"@{inline_query.from_user.username}"
                if inline_query.from_user.username
                else str(sender_id)
            ),
        )
    elif not config.enabled:
        results = [
            _article(
                result_id="w-disabled-disabled",
                title=_REJECTIONS["disabled"][0],
                description=_REJECTIONS["disabled"][1],
                message_text=_rejection_message("disabled", bot_username, config),
            )
        ]
    else:
        results = await _whisper_results(
            bot, inline_query, raw_query, config, bot_username
        )

    await inline_query.answer(
        results=results,
        # One second, not zero. The debounce above already guarantees that what
        # goes out answers the text in the box when this update was raised, but
        # the user may have typed one more character in the meantime — and
        # Telegram does not send us that update while a previous answer is still
        # cached. One second is long enough to swallow the keystroke that
        # followed, short enough that nobody notices the difference between it
        # and none.
        cache_time=1,
        is_personal=True,
        button=InlineQueryResultsButton(
            text=_HELP_BUTTON_TEXT,
            start_parameter=HELP_START_PARAM,
        ),
    )


# ──────────────────────────────────────────────────
# 2. The chosen result → persist {whisper_id: target, secret}
# ──────────────────────────────────────────────────

async def _store_row(
    *,
    token: str,
    sender_id: int,
    target_id: int,
    secret: str,
    delivery: str,
    inline_message_id: str | None,
    is_active: bool,
) -> None:
    """Fill in the row reserved by :func:`_reserve_row`.

    Still an upsert, not an insert: the reserved row is the normal case, but a
    result id that survived from an older build (or a re-picked result) must
    still land somewhere rather than raise a unique-constraint error. What this
    call adds on top of the reservation is the ``delivery`` mode and the
    ``inline_message_id`` of the card Telegram has by now published.
    """
    async with async_session_factory() as session:
        row = await session.scalar(
            select(InlineWhisper).where(InlineWhisper.token == token)
        )
        if row is None:
            session.add(
                InlineWhisper(
                    token=token,
                    sender_id=sender_id,
                    target_id=target_id,
                    secret_text=secret,
                    delivery=delivery,
                    inline_message_id=inline_message_id,
                    is_active=is_active,
                )
            )
        else:
            row.sender_id = sender_id
            row.target_id = target_id
            row.secret_text = secret
            row.delivery = delivery
            row.inline_message_id = inline_message_id
            row.is_active = is_active
        await session.commit()


async def _set_active(token: str) -> bool:
    """Flip ``is_active`` on; ``True`` only for the call that actually flipped it.

    The conditional WHERE makes activation a race-safe one-shot: two callers
    (a double-tapped «عضو شدم», the anchor handler and a reader's tap landing
    together) can both charge, but only one wins the flip — the loser sees
    ``False`` and knows it must undo its own side effects (refund) instead of
    believing it activated the row.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            update(InlineWhisper)
            .where(
                InlineWhisper.token == token,
                # is_not(True), not is_(False): a legacy NULL must still count
                # as "not yet active", or those rows could never flip.
                InlineWhisper.is_active.is_not(True),
            )
            .values(is_active=True)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return bool(result.rowcount)


async def _get_row(token: str) -> InlineWhisper | None:
    """Fetch one row by token (attributes stay readable after the close)."""
    async with async_session_factory() as session:
        return await session.scalar(
            select(InlineWhisper).where(InlineWhisper.token == token)
        )


async def _await_activation(token: str) -> InlineWhisper | None:
    """Wait for the send to be activated, while someone is still doing it.

    The card is live from the moment the anchor handler posts it — before it
    has finished validating the send, applied the forced-join gate and charged
    the sender. A reader who taps inside that window used to be told the whisper
    was not active yet, which is both wrong (the send is fine, it is merely still
    in flight) and a dead end. Worse, the fallback that followed it finalised
    the send from the reader's tap, which is a SECOND run of the economy gate
    for one whisper. Re-reading the row until it settles closes the window.

    ``_delivering`` is what makes "settles" mean "done" rather than "inactive":
    an inactive row is only as good as its last writer, and while the anchor
    handler holds the token the answer is not written yet.

    Returns the settled row: active once the send completes, still inactive when
    the cap is hit, or ``None`` when the send was rejected and took the row with
    it. Polling is skipped entirely when the row is already active, so the
    ordinary path costs nothing.
    """
    row = await _get_row(token)
    if row is None or row.is_active:
        return row

    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACTIVATION_WAIT_SECONDS
    while not row.is_active:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(ACTIVATION_POLL_SECONDS, remaining))
        try:
            settled = await _get_row(token)
        except Exception as exc:
            # A transient database lock must not abort the wait: the row that
            # brought us here is still the best answer we have.
            logger.debug("Activation poll for %s failed: %s", token, exc)
            break
        if settled is None:
            # The send was rejected while we waited and the row is gone.
            return None
        row = settled
    return row


async def _send_parked_behind_join(bot: Bot, sender_id: int) -> bool:
    """True when the send is parked because the SENDER is not in the required chats.

    Membership is only ever blamed when the admin BOTH switched the requirement
    on AND named a chat for it, because ``missing_required_chats`` short-circuits
    to an empty list otherwise: with nothing to join, no send can ever be parked
    and any membership wording would describe a rule that does not exist.

    The check exists so a row that is legitimately waiting behind the join gate
    keeps waiting (the sender clears it with «عضو شدم», see
    :func:`cb_inline_whisper_join`) instead of being re-finalised — and re-prompted
    — on every single tap.
    """
    if not await force_sub_enabled() or not await required_chats():
        return False
    return bool(await missing_required_chats(bot, sender_id, force_refresh=True))


#: Tokens whose sender has already been prompted about in THIS process. Keeps a
#: card parked behind the join gate from re-sending the same DM on every tap.
#: Deliberately not persisted: one extra prompt after a restart is harmless,
#: whereas a stored set would need purging forever.
_join_prompted: set[str] = set()


async def _prompt_sender_to_join(bot: Bot, token: str, sender_id: int) -> None:
    """Tell the SENDER what is holding their whisper back — once per process.

    The card is public and its reader is already being told, so without this the
    sender would sit in the dark until the recipient complained: the
    ``chosen_inline_result`` that normally sends the join prompt never arrived.
    The «عضو شدم» button it carries activates the row through
    :func:`cb_inline_whisper_join`, which closes the loop.
    """
    if token in _join_prompted:
        return
    missing = await missing_required_chats(bot, sender_id, force_refresh=True)
    if not missing:
        return
    _join_prompted.add(token)
    await _send_dm(
        bot,
        sender_id,
        _join_prompt_text(missing),
        whisper_join_kb(missing, check_callback=f"whisper:join:{token}"),
    )


#: Outcomes of :func:`_run_send_gates`.
_GATE_OK = "ok"
_GATE_JOIN = "join"
_GATE_UNDONE = "undone"


async def _run_send_gates(
    bot: Bot,
    *,
    token: str,
    sender_id: int,
    target_id: int,
    target_name: str,
    inline_message_id: str | None,
    park: bool = True,
) -> str:
    """Apply every send gate to a reserved row and switch it on.

    Forced join first, then the coin, then the token gate, then activation — the
    same order, the same checks and the same DMs whether the send is finalised
    by the anchor handler, by ``chosen_inline_result`` taking over, or by the
    recipient pressing the button (see :func:`cb_read_whisper`). One
    implementation is what makes "the gates cannot be skipped" a property of the
    code rather than a convention three call sites have to keep agreeing on.

    That order is also the reason the coin is refunded explicitly: the charge
    and the token check are two different ledgers, and only charging first lets
    a refusal be undone. See :func:`utils.economy.refund_balance`.

    There is deliberately nothing to deliver here: the secret is read from the
    card, so a whisper has exactly one destination and this function does not
    need to be told which one it is.

    ``park`` is the one behavioural switch, and it exists because the two
    callers see the card at different moments. Whoever runs the gates *before*
    the card exists (``park=False``, :func:`_deliver`) has nowhere to park a
    whisper: the group has already been told a message is on its way, and a
    card nobody can press is worse than a refusal that explains itself. So the
    forced-join gate refuses there, with the join prompt as the reason, instead
    of leaving a public button dead. Whoever runs them *after* the card is live
    keeps the old behaviour: park the row, and let the sender's «عضو شدم» finish
    it (see :func:`cb_inline_whisper_join`).

    Returns ``_GATE_OK`` (readable now), ``_GATE_JOIN`` (row kept, parked behind
    the forced-join gate — the sender clears it; only possible when
    ``park=True``) or ``_GATE_UNDONE`` (row dropped and the button taken off the
    card).
    """
    config = await get_whisper_config()
    if not config.enabled:
        await _undo(
            bot,
            token,
            inline_message_id,
            sender_id,
            _rejection_message("disabled", (await bot.me()).username or "bot", config),
        )
        return _GATE_UNDONE

    missing = await missing_required_chats(bot, sender_id, force_refresh=True)
    if missing:
        if park:
            # Mark it prompted here too, not only in _prompt_sender_to_join: the
            # reader's tap and the reconciler both ask that helper afterwards,
            # and a parked row is asked about on every single one of them.
            _join_prompted.add(token)
            await _send_dm(
                bot,
                sender_id,
                _join_prompt_text(missing),
                whisper_join_kb(missing, check_callback=f"whisper:join:{token}"),
            )
            return _GATE_JOIN
        await _undo(
            bot,
            token,
            inline_message_id,
            sender_id,
            _join_prompt_text(missing),
        )
        return _GATE_UNDONE
    clear_membership_cache()

    # The coin, before the token gate. This ordering is the only one that can be
    # made correct: the token check is the LAST gate, so charging first and
    # refunding on refusal is possible, while the reverse order would spend a
    # token for a whisper that then fails to pay for itself. ``charged`` is the
    # exact amount taken (0.0 for a free send), which is what the refund below
    # hands back — a no-op for exempt and subscribed users, whose charge is 0.0.
    charged = await check_and_deduct_balance(sender_id)
    if charged is None:
        logger.info("Inline whisper %s rejected (economy: no balance)", token)
        _, coins = await can_afford_whisper(sender_id)
        await _undo(
            bot,
            token,
            inline_message_id,
            sender_id,
            no_balance_text(coins),
        )
        return _GATE_UNDONE

    auth = await authorize_chat_message(sender_id)
    if not auth.allowed:
        logger.warning(
            "Inline whisper %s rejected (economy: %s) — row removed",
            token,
            getattr(auth, "reason", "?"),
        )
        await refund_balance(sender_id, charged)
        await _undo(bot, token, inline_message_id, sender_id, "")
        await _dm_auth_reason(bot, sender_id, auth)
        return _GATE_UNDONE

    await _set_active(token)

    # The recipient is NOT told privately. The card in the group is the whole
    # delivery — it names them, and they open the whisper from it — so a
    # «you have an anonymous message» DM on top was a second copy of the same
    # news landing in their chat for no gain. It used to be sent from right here,
    # which also meant this function was the only reason a whisper cost an extra
    # API call and put a permanent trace in somebody's private chat.
    #
    # There is no fallback to arrange either: unlike ``/نجوا``, this card is
    # published by Telegram itself the moment the chosen result fires, so there
    # is no posting step here that could fail and strand the row.
    #
    # Whether the recipient can press the button at all is still worth telling
    # the SENDER about, so that one question is asked directly instead of being
    # inferred from a message we no longer send.
    reachable = await has_started_bot(target_id)

    await _send_dm(
        bot,
        sender_id,
        _SENT_OK_GROUP.format(target=_target_display(target_id, target_name)),
        main_menu_kb(),
    )
    if not reachable:
        # Not an error and not a refund: the whisper is live and readable, the
        # recipient simply has no private chat with the bot to read it through
        # until they start it. Telling the sender WHY is the whole job here —
        # the alternative is a card nobody in the group can explain.
        await _send_dm(bot, sender_id, target_not_started_text(target_name))
    return _GATE_OK


async def _attach_card(token: str, inline_message_id: str | None) -> None:
    """Record the card a row backs, so the orphan sweep cannot take it.

    ``_purge_old`` identifies abandoned reservations by
    ``inline_message_id IS NULL``. A whisper whose ``chosen_inline_result``
    never landed keeps that column NULL even though Telegram published its card,
    so the sweep would delete the row out from under a LIVE button an hour
    later — the reader would then be told the whisper expired.
    """
    if not inline_message_id:
        return
    try:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(InlineWhisper).where(InlineWhisper.token == token)
            )
            if row is not None and row.inline_message_id is None:
                row.inline_message_id = inline_message_id
                await session.commit()
    except Exception as exc:
        logger.debug("Could not attach card to whisper %s: %s", token, exc)


def _user_name(target_id: int) -> str:
    """Best-effort display name for ``target_id``; ``""`` when unknown.

    Served from the in-memory group roster, not the database: this sits on the
    reader's-tap path (a dead activation is finalised there) and the roster is
    where the recipient's name came from in the first place.
    """
    return member_label(target_id)


async def _delete_row(token: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            delete(InlineWhisper).where(InlineWhisper.token == token)
        )
        await session.commit()


async def _purge_old() -> None:
    """Drop READ rows older than the retention window (best effort).

    Only ``is_viewed`` rows are eligible — and that guard is the whole point.
    The card lives in the chat forever, so an unviewed row is still the only
    thing that makes its button work; purging it (the old behaviour, on every
    chosen result) turned every card older than ``RETENTION_DAYS`` into the
    dead button that answers «این نجوا دیگر قابل نمایش نیست». An unviewed
    whisper therefore outlives any retention window, which is exactly what
    «نجوا زمان‌دار نیست و منقضی نمی‌شود» promises the user.
    """
    try:
        cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        orphan_cutoff = datetime.utcnow() - timedelta(minutes=ORPHAN_MINUTES)
        async with async_session_factory() as session:
            await session.execute(
                delete(InlineWhisper).where(
                    (InlineWhisper.created_at < cutoff)
                    & (InlineWhisper.is_viewed.is_(True))
                )
            )
            # Reservations the sender never turned into a send. Since
            # _reserve_row() now runs on every inline query — including ones
            # the user abandons without tapping a result — this is what keeps
            # the table from growing by one dead row per keystroke-completed
            # query. The two guards make it safe: a row only qualifies while it
            # is still INACTIVE (nothing was ever sent, so nothing can be
            # missed) and while it has no card pointing at it.
            await session.execute(
                delete(InlineWhisper).where(
                    (InlineWhisper.created_at < orphan_cutoff)
                    & (InlineWhisper.is_active.is_(False))
                    & (InlineWhisper.inline_message_id.is_(None))
                )
            )
            await session.commit()
    except Exception as exc:
        logger.debug("Could not purge old inline whispers: %s", exc)


async def _neutralize(bot: Bot, inline_message_id: str | None) -> None:
    """Strip the read button from an inline card whose send was rejected.

    Edited via ``inline_message_id``: the reconciler carries no chat of its own
    and must never touch the chat the query was typed in except through the
    anchor, whose id it was given. (The anchor path never comes through here —
    it rewrites the sender's own message through :func:`_replace_with_notice`.)
    """
    if not inline_message_id:
        return
    try:
        await bot.edit_message_text(
            text=_NEUTRALIZED_TEXT,
            inline_message_id=inline_message_id,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except Exception as exc:
        # Warning, not debug: when this edit fails the card keeps a button
        # whose row is already gone — precisely the dead card that makes the
        # reader see «این نجوا دیگر قابل نمایش نیست». Loud on purpose.
        logger.warning("Could not neutralize inline whisper card: %s", exc)


async def _dm_auth_reason(bot: Bot, user_id: int, auth) -> None:
    """Explain a refused send in the sender's DM — never in the group.

    The flood limit is the only remaining refusal: messages are free and the
    sender is refunded whenever one is rejected.
    """
    if not should_warn(user_id):
        return
    try:
        policy = await get_policy()
        text = rate_limit_text(auth.wait, policy.messages_per_minute)
    except Exception as exc:  # noqa: BLE001 — a warning must not crash the send
        logger.info("Could not render the rate-limit warning: %s", exc)
        text = rate_limit_text(auth.wait)
    await _send_dm(bot, user_id, text)


async def _undo(
    bot: Bot,
    token: str,
    inline_message_id: str | None,
    sender_id: int,
    text: str,
    *,
    delete_row: bool = True,
) -> None:
    """Take a rejected whisper back off the board, in the only safe order.

    By the time we get here the whisper is visible in some chat — as the card
    the anchor handler posted, or as the anchor itself on the reconciler's
    fallback path — so a rejection has to do two things: drop the row *and* strip
    the button. Order matters — delete the row first, so that if neutralising
    the card fails the reader is told the whisper was never stored instead of
    being offered a button into a row that no longer exists. Neutralising first
    would leave exactly that: a live button whose row is gone, which is the dead
    card that made the recipient read "no longer available" on a whisper that had
    been sent and paid for.

    ``inline_message_id=None`` is the anchor path: there the sender's own
    message is rewritten by :func:`_replace_with_notice` instead, once the
    caller knows the delivery went wrong.

    ``delete_row=False`` exists for one case: the row already backs a LIVE
    card (the sender picked the same picker result a second time and this
    later attempt was rejected). Deleting would murder the first card's
    button, so only the duplicate card is neutralised and the row survives.
    """
    if delete_row:
        await _delete_row(token)
    await _neutralize(bot, inline_message_id)
    await _send_dm(bot, sender_id, text)


def _target_display(_target_id: int, target_name: str) -> str:
    """Who a sent whisper was addressed to — with no numeric id in sight.

    This renders into the «نجوا ثبت شد» confirmation; an id there put the
    recipient's identifier on display for anyone looking over the sender's
    shoulder (and, where the card was posted, into the chat itself). The name
    is enough to confirm what was sent; without one the recipient is just
    another member of the group.
    """
    if target_name:
        return escape(target_name)
    return "یکی از اعضای این گروه"


# ──────────────────────────────────────────────────
# 1b. The anchor → the bot-authored card
# ──────────────────────────────────────────────────

async def _target_in_chat(bot: Bot, chat_id: int, target_id: int) -> str:
    """May a card addressed to ``target_id`` land in ``chat_id``?

    The card lands HERE, so the recipient has to be HERE too: a whisper aimed at
    someone who is not in the group produces a button nobody will ever press,
    which is the anonymous equivalent of mailing it to the wrong address.

    Returns one of the ``VERDICT_*`` constants from :mod:`utils.target_lookup`,
    which is also where the rights-dependent reasoning lives. The short version:
    ``getChatMember`` decides whenever it answers at all, and a failed lookup is
    only ever read as absence when Telegram is actually able to see the person —
    being an administrator is not enough, because it hides the users who never
    started the bot from administrators too. Otherwise the send proceeds.
    """
    return await recipient_verdict(bot, chat_id, target_id)


def _anchor_token(message: Message) -> str:
    """The token an anchor carries, or ``""`` when this is not an anchor.

    Recognised by its own prefix AND a read button. :class:`OwnInlineMessage` has
    already proved the text reached the chat through our inline mode, so the two
    together leave no room for a coincidence: a user who typed «⏳ در حال ارسال
    پیام ناشناس…» by hand never reaches this handler, and an anchor is never
    mistaken for a rejection stub.
    """
    if not (message.text or "").startswith(_ANCHOR_CARD_PREFIX):
        return ""
    markup = message.reply_markup
    if not (markup and markup.inline_keyboard):
        return ""
    for keyboard_row in markup.inline_keyboard:
        for button in keyboard_row:
            data = button.callback_data or ""
            if re.fullmatch(_READ_RE, data) or re.fullmatch(_LEGACY_READ_RE, data):
                return data.rsplit(":", 1)[1]
    return ""


async def _deliver(bot: Bot, message: Message, token: str) -> None:
    """Turn one anchor into one live card (the body of :func:`_handle_anchor`).

    The anchor is NEVER deleted: it is the sender's own message and it stays in
    the chat exactly as they sent it. Every outcome below therefore rewrites it
    in place (via :func:`_deliver_in_place` / :func:`_replace_with_notice`)
    instead of removing it.
    """
    row = await _get_row(token)
    if row is None:
        # A reservation that never landed, or one a rejection already took.
        # The anchor promises a card that will not come, so it is rewritten as
        # the neutralized notice rather than deleted.
        await _replace_with_notice(bot, message)
        return
    if row.is_active:
        # Already delivered: the sender picked the same result twice, or the
        # reconciler finalised this send after both sides timed out waiting for
        # each other. One send, one card.
        await _replace_with_notice(bot, message)
        return

    sender_id, target_id = row.sender_id, row.target_id
    target_name = _user_name(target_id)

    # The anchor BECOMES the card. It must never sit next to a second copy:
    # two read buttons in one chat, one of them about to be invalidated by the
    # other, is a puzzle for everyone reading it.
    await _deliver_in_place(bot, message, token, sender_id, target_id, target_name)


async def _deliver_in_place(
    bot: Bot,
    message: Message,
    token: str,
    sender_id: int,
    target_id: int,
    target_name: str,
) -> None:
    """Deliver by rewriting the anchor, which the bot never deletes.

    The sender's own message stays in the chat exactly as they sent it; the
    rewrite only swaps the placeholder for the real card. The secret still never
    enters the chat — that is what the row and the button are for — but the card
    itself is not authored by the bot, so the sender's name is on it.
    """
    config = await get_whisper_config()
    bot_username = (await bot.me()).username or "bot"

    verdict = await _target_in_chat(bot, message.chat.id, target_id)
    if verdict != VERDICT_OK:
        # The row goes first (so a failed edit can only ever leave a button
        # whose whisper is already gone), then the anchor becomes the refusal
        # notice — it cannot be deleted, so leaving the ⏳ there would be a lie
        # about a card that is never coming. This runs BEFORE the economy gate,
        # so a wrong id costs the sender nothing.
        await _undo(
            bot,
            token,
            None,
            sender_id,
            _rejection_message(verdict, bot_username, config),
        )
        await _replace_with_notice(bot, message)
        return

    try:
        await message.edit_text(
            _whisper_card_text(target_id, target_name, ""),
            parse_mode="HTML",
            # The plain read button, NOT the action keyboard — see
            # :func:`_card_keyboard`. The card is published before anyone has
            # opened it, so the actions have nothing to act on and would only
            # advertise themselves to the whole group. They arrive on their own
            # the moment the recipient presses this button.
            reply_markup=whisper_read_kb(token),
        )
    except Exception as exc:
        logger.warning("Could not turn the anchor into a card: %s", exc)
        await _delete_row(token)
        await _send_dm(bot, sender_id, _SEND_FAILED_DM)
        return

    outcome = await _run_send_gates(
        bot,
        token=token,
        sender_id=sender_id,
        target_id=target_id,
        target_name=target_name,
        inline_message_id=None,
        park=True,
    )
    if outcome == _GATE_OK:
        # The card is live and readable right now.
        return
    if outcome == _GATE_JOIN:
        # The card is live and waiting; the sender's «عضو شدم» finishes it.
        return
    # ``_GATE_UNDONE``: the row is gone and the button must not survive it.
    await _replace_with_notice(bot, message)


async def _replace_with_notice(bot: Bot, message: Message) -> None:
    """Turn an anchor that must not be deleted into the failure notice.

    Its counterpart to :func:`_neutralize`, for the anchor path. The message is
    the sender's own and is never deleted, so the cheapest honest thing to do is
    rewrite it rather than leave a ⏳ promising a card that is never going to
    arrive.
    """
    try:
        await message.edit_text(
            _NEUTRALIZED_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except Exception as exc:
        logger.warning("Could not rewrite the anchor: %s", exc)


async def _handle_anchor(bot: Bot, message: Message, token: str) -> None:
    """Own the send for one anchor, from picking it up to letting go.

    Called from :func:`on_inline_result_message` — that is, for the placeholder
    Telegram published in the very conversation the sender picked it in. Being a
    ``message`` handler is the entire reason this function exists: neither
    ``inline_query`` nor ``chosen_inline_result`` carries a chat, so no amount of
    work on them can ever learn where the card is supposed to sit.

    ``_delivering`` holds the token for as long as this runs, so the
    ``chosen_inline_result`` arriving moments later stands down instead of
    running the economy gate a second time (see :func:`_wait_for_delivery`).
    """
    _delivering.add(token)
    try:
        await _deliver(bot, message, token)
    except Exception as exc:
        # Nothing here may escape: an uncaught error would leave the anchor in
        # the chat with a read button whose row is still there — a card that
        # would let the recipient read a whisper the sender was never charged
        # for and never completed. Drop the row and let the card die with it.
        logger.exception("Inline whisper %s: anchor delivery failed: %s", token, exc)
        row = await _get_row(token)
        await _delete_row(token)
        await _replace_with_notice(bot, message)
        if row is not None:
            await _send_dm(bot, row.sender_id, _SEND_FAILED_DM)
    finally:
        _delivering.discard(token)


async def _wait_for_delivery(token: str) -> bool:
    """Give the anchor handler the seconds it is entitled to. True ⇒ stand down.

    The anchor ``message`` and the ``chosen_inline_result`` describe ONE action
    seen from two sides, and Telegram guarantees their order but not their
    arrival. What must never happen is both of them deciding the same send: a
    second run of the economy gate, i.e. a second charge for one whisper, and
    two cards for one message. So the reconciler waits here instead of acting,
    and only takes over when the row itself says nothing happened.

    ``True`` means "someone else owns this send": the row is gone because the
    anchor handler refused it, it is active because the send went through, or a
    handler is still holding the token as the cap expires.

    ``False`` means the row is still an untouched reservation and nobody is
    working on it — the anchor update never arrived (a pruned update, a restart
    between the two), which is the one case where taking over is the only way the
    whisper still gets sent.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DELIVERY_WAIT_SECONDS
    while True:
        row = await _get_row(token)
        if row is None or row.is_active:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            # Hand back control rather than race a handler that is merely slow.
            return token in _delivering
        await asyncio.sleep(min(DELIVERY_POLL_SECONDS, remaining))


async def _replace_anchor(
    bot: Bot,
    inline_message_id: str | None,
    token: str,
    card_text: str,
) -> None:
    """Rewrite the anchor into the card by ``inline_message_id`` (best effort).

    Only the reconciler needs this — it has no ``chat``, only the id of a
    message it cannot see the text of. It is the same trade :func:`_deliver` had
    to make: the card appears, the sender's name is on it.

    It takes no party ids at all, which is the point: the card TEXT already
    names the receiver, and the keyboard is the plain read one until the
    recipient opens the whisper — so there is nothing here a party id could be
    needed for (see :func:`_card_keyboard`).
    """
    if not inline_message_id:
        return
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=card_text,
            parse_mode="HTML",
            reply_markup=whisper_read_kb(token),
        )
    except Exception as exc:
        logger.warning("Could not turn the anchor into a card: %s", exc)


@router.chosen_inline_result()
async def on_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    """Reconcile the pick — the anchor handler is what actually sends.

    This update no longer decides anything in the ordinary case. It carries no
    chat, so it cannot know where the card belongs, and the card is already
    being posted by :func:`_deliver` from the anchor's own ``message`` update.
    Its job is to make sure the send happens EXACTLY once:

      1. record the anchor id on the row, before waiting — the orphan sweep
         deletes inactive rows with no card pointing at them, and this
         reservation is about to spend up to ``DELIVERY_WAIT_SECONDS`` proving
         it is not abandoned;
      2. wait for the anchor handler (see :func:`_wait_for_delivery`);
      3. only if nothing happened at all, do the work itself — re-validate,
         run the gates and turn the anchor into the card by
         ``inline_message_id``.

    Step 3 is the safety net, not the design. It exists because this update and
    that message are two views of one action and Telegram does not promise both
    arrive: a pruned update, or a restart between them, used to leave a
    reserved row nothing would ever complete — a whisper the sender paid for
    that no one could open, and no button anywhere to retry from.
    """
    sender_id = chosen.from_user.id
    inline_message_id = chosen.inline_message_id

    token, kind = _parse_result_id(chosen.result_id or "")
    if not token:
        # Not one of our whisper rows — the placeholder or a rejection. Nothing
        # was promised and nothing is stored; the empty message Telegram posted
        # is deleted by `on_inline_result_message`, which needs no help from us.
        #
        # Note there is deliberately no fallback DM here: an inline handler may
        # not message a user who has not started the bot. The «📖 راهنما» button
        # above the results is the supported way in.
        return

    logger.info(
        "Inline whisper %s chosen (anchor=%s).",
        token,
        "yes" if inline_message_id else "no",
    )
    try:
        await _attach_card(token, inline_message_id)

        if await _wait_for_delivery(token):
            return

        row = await _get_row(token)
        if row is None:
            return
        if await _send_parked_behind_join(bot, row.sender_id):
            # The anchor handler got as far as the forced-join gate and left the
            # row waiting for the sender's «عضو شدم». That row is not abandoned
            # and must not be re-validated: doing so would charge the sender a
            # second time for the whisper they are still trying to unlock.
            await _prompt_sender_to_join(bot, token, row.sender_id)
            return

        logger.warning(
            "Inline whisper %s: the anchor never reached us — finalising here.",
            token,
        )
        await _handle_chosen(bot, chosen, token, inline_message_id)
    except Exception as exc:
        logger.exception("Inline whisper %s reconcile failed: %s", token, exc)
        if token in _delivering:
            # The anchor handler is mid-delivery: its outcome is the real one,
            # and reaching in now would kill a card the sender already paid for.
            return
        await _undo(bot, token, inline_message_id, sender_id, _SEND_FAILED_DM)
    finally:
        await _purge_old()


async def _handle_chosen(
    bot: Bot,
    chosen: ChosenInlineResult,
    token: str,
    inline_message_id: str | None,
) -> None:
    """Finalise a pick whose anchor never arrived (see its only caller).

    Kept whole — the pre-check, the row write and the gates — because this is
    exactly the body :func:`on_chosen_inline_result` had before the anchor
    existed, and a whisper must still be sendable when Telegram gives us the
    picker update and drops the message. It differs in one respect: on success
    it rewrites the anchor into the card (see :func:`_replace_anchor`), because
    without a ``chat`` the anchor's own message is the only card that can exist.
    """
    sender_id = chosen.from_user.id
    bot_username = (await bot.me()).username or "bot"
    config = await get_whisper_config()

    target_id, target_name, target_username, secret, reject = await _precheck(
        bot, sender_id, chosen.query or "", config, authoritative=True
    )

    # ── Read BEFORE the upsert below ──
    # An already-active row means this send is DONE: either the sender picked
    # the same picker result twice, or the recipient's tap already finalised it
    # because this very update had not arrived by then (see ``cb_read_whisper``).
    # Falling through would park, reactivate or delete a row that a LIVE card
    # depends on — killing the first card's button — and, worse, run the economy
    # gate a second time and CHARGE THE SENDER TWICE for one whisper. So the row
    # is left exactly as it is and only the confirmation is repeated.
    existing = await _get_row(token)
    if existing is not None and existing.is_active:
        logger.info("Inline whisper %s already active — not charging again.", token)
        await _send_dm(
            bot,
            sender_id,
            _SENT_OK_GROUP.format(
                target=_target_display(target_id or existing.target_id, target_name)
            ),
            main_menu_kb(),
        )
        return

    # ── Park the row so a live card is never a dead button ──
    await _store_row(
        token=token,
        sender_id=sender_id,
        target_id=target_id or sender_id,
        # NOTE: the parameter is ``secret`` (it maps onto the
        # ``secret_text`` column). Passing ``secret_text=`` here raised
        # TypeError on EVERY send, the row was never written, and the
        # receiver was met with «این نجوا دیگر قابل نمایش نیست».
        secret=secret or "",
        delivery="group",
        inline_message_id=inline_message_id,
        is_active=False,
    )

    if reject == "disabled" or not config.enabled:
        reject = reject or "disabled"
        logger.warning("Inline whisper %s rejected (%s) — row removed", token, reject)
        await _undo(
            bot,
            token,
            inline_message_id,
            sender_id,
            _rejection_message(reject, bot_username, config),
        )
        return
    if reject:
        logger.warning("Inline whisper %s rejected (%s) — row removed", token, reject)
        await _undo(
            bot,
            token,
            inline_message_id,
            sender_id,
            _rejection_message(reject, bot_username, config),
        )
        return

    # ── Forced-join, economy and activation — shared with every other path ──
    outcome = await _run_send_gates(
        bot,
        token=token,
        sender_id=sender_id,
        target_id=target_id,
        target_name=target_name,
        inline_message_id=inline_message_id,
    )
    if outcome == _GATE_OK:
        # The card is only visible as the anchor Telegram published, so this is
        # where it becomes the card the reader is meant to open.
        await _replace_anchor(
            bot,
            inline_message_id,
            token,
            _whisper_card_text(target_id, target_name, target_username),
        )


# ──────────────────────────────────────────────────
# 3. "عضو شدم" — activate a whisper parked behind the forced-join gate
# ──────────────────────────────────────────────────

async def _edit_callback_message(callback: CallbackQuery, text: str, kb) -> None:
    """Turn the join prompt into a confirmation (best effort)."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
    except Exception as exc:
        logger.debug("Could not edit join prompt: %s", exc)


@router.callback_query(F.data.regexp(_JOIN_RE))
async def cb_inline_whisper_join(callback: CallbackQuery) -> None:
    """Re-check membership, then charge and switch the parked whisper on."""
    token = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    bot = callback.bot

    async with async_session_factory() as session:
        row = await session.scalar(
            select(InlineWhisper).where(InlineWhisper.token == token)
        )

    if row is None or row.sender_id != user_id:
        await callback.answer("⚠️ این درخواست دیگر معتبر نیست.", show_alert=True)
        return
    if row.is_active:
        await callback.answer("✅ نجوا فعال است.", show_alert=True)
        return

    missing = await missing_required_chats(bot, user_id, force_refresh=True)
    if missing:
        await callback.answer(
            f"❌ هنوز عضو «{escape(missing[0].title)}» نشده‌اید.", show_alert=True
        )
        await _edit_callback_message(
            callback,
            _join_prompt_text(missing),
            whisper_join_kb(missing, check_callback=callback.data),
        )
        return

    clear_membership_cache()

    # The coin — this path must bill exactly like ``_run_send_gates`` does.
    # The row got parked at the join prompt BEFORE the charge happens (the
    # forced-join gate runs first), so activating it here without billing was
    # a free-whisper exploit: join → «عضو شدم» → the whisper goes out having
    # never paid. ``charged`` is the exact amount (0.0 = free), so the rate
    # gate below can hand back precisely what was taken.
    charged = await check_and_deduct_balance(user_id)
    if charged is None:
        _, coins = await can_afford_whisper(user_id)
        await _undo(
            bot,
            token,
            row.inline_message_id,
            user_id,
            no_balance_text(coins),
        )
        await callback.answer(
            "❌ سکه کافی ندارید؛ توضیحات در پی‌وی.", show_alert=True
        )
        return

    auth = await authorize_chat_message(user_id)
    if not auth.allowed:
        await refund_balance(user_id, charged)
        # The card is already live in the group: drop the row FIRST, then take
        # the button away, then explain in the DM — the order _undo() documents.
        await _delete_row(token)
        await _neutralize(bot, row.inline_message_id)
        await _dm_auth_reason(bot, user_id, auth)
        await callback.answer("⛔ ارسال نجوا ممکن نشد؛ جزئیات در پی‌وی.", show_alert=True)
        return

    if not await _set_active(token):
        # Someone else activated (or deleted) it between our read and now —
        # most plausibly a double-tap of this very button. Our charge must not
        # stand for a whisper the other call already paid for.
        await refund_balance(user_id, charged)
        await callback.answer("✅ نجوا فعال است.", show_alert=True)
        return

    await _edit_callback_message(
        callback, "✅ <b>عضویت تایید شد — نجوا فعال شد.</b>", None
    )
    await callback.answer("✅ نجوا فعال شد.")


# ──────────────────────────────────────────────────
# 4. Reading the whisper — a private modal only the target ever sees
# ──────────────────────────────────────────────────

async def _retire_dead_card(callback: CallbackQuery, bot: Bot) -> None:
    """Take the button off a card whose row is gone (best effort).

    Reached only on a terminal miss now that the row is reserved before the
    card can be published: a send that was rejected after Telegram posted the
    card, or a row a retention sweep eventually took. Leaving the button in
    place makes every future tap fail the same way; replacing the card with a
    short "gone" note tells the group the truth once and stops the dead end.
    """
    try:
        if callback.inline_message_id:
            await bot.edit_message_text(
                text="⛔️ این نجوا دیگر موجود نیست یا منقضی شده است.",
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
            return
        message = callback.message
        if message is not None:
            await message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
            )
    except Exception as exc:
        logger.debug("Could not retire dead whisper card: %s", exc)


#: Prefix the modal puts in front of the secret. It is part of the 200-char
#: alert budget, which is why the cap below is measured against the WHOLE
#: rendered string rather than against the secret alone.
_SECRET_ALERT_PREFIX = "📩 پیام ناشناس شما:\n\n"


async def _reveal_secret(
    callback: CallbackQuery, bot: Bot, target_id: int, secret: str
) -> None:
    """Show the secret in a private modal, respecting Telegram's 200-char cap.

    A long secret is truncated in the modal and the full text is handed over
    in a DM addressed to the very same user, who has just proven ownership by
    passing the identity check. Nobody else ever receives a copy.
    """
    body = _SECRET_ALERT_PREFIX + secret
    if len(body) <= MAX_ALERT_LENGTH:
        await callback.answer(text=body, show_alert=True)
        return

    room = MAX_ALERT_LENGTH - len(_SECRET_ALERT_PREFIX) - 1
    if room > 0:
        await callback.answer(
            text=_SECRET_ALERT_PREFIX + secret[:room] + "…", show_alert=True
        )
    else:
        # Pathological config: the prefix alone fills the budget. Say only that
        # there is a message waiting, and put the text in the DM.
        await callback.answer(text="📩 پیام ناشناس شما (متن کامل در پی‌وی)", show_alert=True)
    await _send_dm(
        bot,
        target_id,
        f"🔐 <b>متن کامل نجوا:</b>\n\n{escape(secret)}",
    )


def _snooper_label(user) -> str:
    """One line identifying the person who pressed the button.

    Falls back through full name → first name → ``@username`` → the raw id, so
    the list is never a column of identical blanks. Whitespace is collapsed
    because the name is rendered one-per-line inside a plain-text alert, and a
    name carrying a newline could forge an extra entry.
    """
    for candidate in (
        getattr(user, "full_name", ""),
        getattr(user, "first_name", ""),
        f"@{user.username}" if getattr(user, "username", None) else "",
        str(getattr(user, "id", "")),
    ):
        flat = " ".join(str(candidate).split())
        if flat:
            return flat
    return "کاربر ناشناس"


async def _note_snooper(token: str, user) -> None:
    """Record one unauthorised press on this whisper's card (best effort).

    Deliberately NOT the reason for the refusal: the tapper is told they may not
    read the whisper whether or not this write succeeds, because a snooper list
    that decides who is allowed to press a button would be an access-control
    mechanism, and this is bookkeeping.

    De-duplicated by ``user_id`` and keeping the FIRST press of each person, so
    the list answers "who tried" instead of "who tried hardest", and so a user
    cannot bury the account of someone else by pressing the button a hundred
    times. The whole list is replaced in one assignment — SQLAlchemy only
    notices a change it can see, and mutating the list in place would go
    unnoticed until a later session happened to flush something else.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    try:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(InlineWhisper).where(InlineWhisper.token == token)
            )
            if row is None:
                return
            seen = list(row.snoopers or [])
            if any(entry.get("user_id") == user_id for entry in seen):
                return
            seen.append(
                {
                    "user_id": user_id,
                    "name": _snooper_label(user),
                    "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                }
            )
            row.snoopers = seen
            await session.commit()
    except Exception as exc:
        # A snooper ledger that cannot be written is not a reason to change the
        # answer the tapper gets.
        logger.debug("Could not record snooper on %s: %s", token, exc)


def _is_party(row, user_id: int) -> bool:
    """True when ``user_id`` is one of the two people this whisper is about."""
    return user_id in (row.sender_id, row.target_id)


def _stats_text(row) -> str:
    """The «📊 آمار» body, measured against the 200-char alert ceiling."""
    entries = [entry for entry in (row.snoopers or []) if isinstance(entry, dict)]
    if not entries:
        return f"{_SNOOPERS_HEAD}\n{_SNOOPERS_EMPTY}"

    lines = [f"{_SNOOPERS_HEAD}"]
    for entry in entries:
        name = str(entry.get("name") or entry.get("user_id") or "؟")
        lines.append(f"- {name}")
    text = "\n".join(lines)
    if len(text) <= MAX_ALERT_LENGTH:
        return text

    # Drop whole entries rather than cutting mid-name; the count tells the reader
    # the list continues, which a truncated last line never does.
    body = "\n".join(lines)
    while len(body) > MAX_ALERT_LENGTH and len(lines) > 2:
        lines.pop()
        body = "\n".join(lines) + f"\n… (+{len(entries) - len(lines) + 1} نفر)"
    return body[: MAX_ALERT_LENGTH - 1] + "…"


async def _edit_card(callback: CallbackQuery, bot: Bot, text: str, markup=None) -> bool:
    """Replace the card's text AND its keyboard, whichever way it was posted.

    Same two-way problem :func:`_apply_card_kb` solves: an inline-mode card
    arrives with no ``message`` and an ``inline_message_id``, a command-mode card
    with the opposite. Best effort — the answer the user is waiting for is
    ``callback.answer``, not the card's cosmetics.

    «message is not modified» is answered with ``True``, not ``False``. It means
    the card already says exactly what we were about to write, which is the
    outcome we wanted; two recipients tapping «مشاهده پیام» at the same moment is
    the ordinary way to produce it, and reporting failure there would log a fault
    that does not exist and make the caller retry an edit that cannot help.
    """
    try:
        if callback.inline_message_id:
            await bot.edit_message_text(
                text=text,
                inline_message_id=callback.inline_message_id,
                parse_mode="HTML",
                reply_markup=markup or InlineKeyboardMarkup(inline_keyboard=[]),
            )
            return True
        message = callback.message
        if message is not None:
            await message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=markup or InlineKeyboardMarkup(inline_keyboard=[]),
            )
            return True
    except TelegramBadRequest as exc:
        if _NOT_MODIFIED in str(exc).lower():
            logger.debug("Whisper card already carries the new text: %s", exc)
            return True
        logger.debug("Could not rewrite whisper card: %s", exc)
    except Exception as exc:
        logger.debug("Could not rewrite whisper card: %s", exc)
    return False


async def _apply_card_kb(callback: CallbackQuery, bot: Bot, markup) -> bool:
    """Swap the keyboard on the whisper card, whichever way it was posted.

    Inline-mode cards come from an ``InlineQueryResultArticle``, so Telegram
    sends the callback WITHOUT a ``message`` and identifies the card by
    ``inline_message_id`` instead — ``callback.message.edit_reply_markup()``
    is ``None.edit_reply_markup()`` there and raises. The command-mode card
    is an ordinary group message, so the other branch is the one that works
    for it. Both are needed; neither alone covers every card.

    Best effort: a card we cannot re-key still gets its alert, which is the
    part the recipient actually needs.
    """
    try:
        if callback.inline_message_id:
            await bot.edit_message_reply_markup(
                inline_message_id=callback.inline_message_id, reply_markup=markup
            )
            return True
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=markup)
            return True
    except Exception as exc:
        logger.debug("Could not update whisper card keyboard: %s", exc)
    return False


def _card_keyboard(whisper_id: str, row) -> InlineKeyboardMarkup:
    """The keyboard a whisper card should be carrying right now.

    One rule, and it is the whole reason this is a function rather than two call
    sites that build their own: a whisper carries the **read button only** until
    the person it was written for has opened it, and the full action keyboard
    («📊 آمار» / «⚙️ گزینه‌ها» / «🗑️ حذف» / «↩️ پاسخ») after that.

    Shipping the actions from the start put four buttons on every unopened
    card — in front of the entire group, including people who had no business
    knowing that a snooper list or a delete button existed — and all of them
    dead-ended anyway, because the whisper they act on had not been read by
    anyone. Waiting for the recipient also gives the buttons a reason to exist:
    by then the card is a conversation between two people rather than a stranger's
    envelope, and «📊 آمار» has something to report.

    The state lives in the row (``target_viewed``), not in the keyboard, because
    a keyboard is frozen when the message is published and every later state
    arrives as an edit — and because the sender's own read must never *unlock*
    the card, nor take the buttons away again once the recipient has opened it.
    """
    if not row.target_viewed:
        return whisper_read_kb(whisper_id)
    return get_whisper_action_keyboard(whisper_id, row.sender_id, row.target_id)


async def _mark_target_viewed(row_id: int) -> bool:
    """Flip ``target_viewed`` on one row. True when it is set afterwards.

    Best effort, but the boolean is load-bearing: the caller re-keys the card
    from it, so a failed write must leave the keyboard read-only rather than
    publish buttons the database does not agree with. That can only cost the
    user one more tap on «👁️ نمایش پیام», whereas publishing them anyway would let a
    later tap (by either party) strip them off a card the recipient had already
    unlocked.
    """
    try:
        async with async_session_factory() as session:
            managed = await session.get(InlineWhisper, row_id)
            if managed is None:
                return False
            if not managed.target_viewed:
                managed.target_viewed = True
                await session.commit()
            return True
    except Exception as exc:
        logger.info("Could not mark whisper row %s as read by its target: %s", row_id, exc)
        return False


@router.callback_query(F.data.regexp(_READ_RE))
@router.callback_query(F.data.regexp(_LEGACY_READ_RE))
async def cb_read_whisper(callback: CallbackQuery, bot: Bot) -> None:
    """``👁️ مشاهده پیام`` / ``🔄 بررسی عضویت و نمایش`` → the secret.

    The alert is rendered by the recipient's own Telegram client; nobody else
    in the group — not even an admin — receives a copy of it.

    Both buttons carry the same ``callback_data`` on purpose: the card has two
    states (default / showing join buttons) and one meaning ("try to read
    this"), so one handler serves both and the verify button needs no branch
    of its own.

    Accepts both ``read_whisper:<uuid4>`` and the legacy ``whisper:<16 hex>``
    id, because the cards carrying the old shape are already sitting in chats
    and both shapes resolve against the same table.

    Steps, in order:

      A. identity — only the two people the whisper is about (its sender and
         its target) get anything useful, and only THEY can trigger the card
         rewrites below. Anyone else is recorded on the row as a snooper
         (:func:`_note_snooper`) and told nothing beyond «you may not read this».
      B. activation — the card is published before its send is validated, so an
         inactive row is waited out (see :func:`_await_activation`) rather than
         refused. If it is STILL inactive afterwards, the send is finalised here
         through :func:`_run_send_gates` — unless the sender is genuinely parked
         behind the forced-join gate, which is left waiting for their own
         «عضو شدم», or the anchor handler is still running the gates, which is
         left alone because a second run is a second charge. An unvalidated send
         is never readable: the forced-join and economy gates still run, they
         simply run on the reader's tap when the delivery update never arrived.
      C. force-sub off (or nothing configured) — no channel check at all;
         restore the keyboard this card should be carrying and show the secret.
      D. force-sub on — collect the channels they still miss; if any, re-key
         the card to join buttons + verify and stop. Once they are all
         joined, restore the keyboard and show the secret.

    The card's action buttons («📊 آمار» / «⚙️ گزینه‌ها» / «🗑️ حذف» /
    «↩️ پاسخ») appear here, and only here — and only when the reader is the
    TARGET. Until then the card carries «👁️ مشاهده پیام» and nothing else, so
    the sender cannot delete a whisper nobody has opened yet. See
    :func:`_card_keyboard`.

    The same first read writes the read receipt onto the card
    (:data:`_READ_RECEIPT_LINE`), so the group can see the message was collected
    instead of wondering forever. It is not repeated on later reads, and never
    appears when the sender opens their own card.
    """
    whisper_id = callback.data.split(":", 1)[1]

    # A single committed lookup. The old handler retried this eight times over
    # ~3.2s, purely to out-wait a row that was written *after* Telegram had
    # already published the card. _reserve_row() now commits that row before
    # the button is ever reachable, so the wait has nothing left to wait for —
    # and a miss is therefore a real, terminal one (rejected send, or a row
    # swept long after its card) rather than a race we happened to lose.
    row = await _get_row(whisper_id)

    if row is None:
        # Nothing can ever be recovered: say so ONCE, take the dead button off
        # the card so it stops lying, and point at the only fix (the sender
        # resending). The reader can do nothing else.
        await _retire_dead_card(callback, bot)
        await callback.answer(
            _GONE_ALERT,
            show_alert=True,
        )
        return

    # ── Step A — identity ──
    # Checked before anything else, and it also bounds who may re-key the
    # card: a random member tapping this must not be able to turn a stranger's
    # whisper into a channel-ad billboard for the whole group.
    #
    # Both parties may read — the target obviously, the sender because they
    # wrote it and «👁️ نمایش پیام» is on their own card too. Everyone else is a
    # stranger's tap, and that tap is RECORDED on the row: a card that only
    # says "not for you" tells the two people it is about that somebody is
    # knocking on it, over and over, and gives them no way to find out who. The
    # write is bookkeeping and never gates the answer (see :func:`_note_snooper`).
    if not _is_party(row, callback.from_user.id):
        await _note_snooper(whisper_id, callback.from_user)
        await callback.answer(_NO_READ_ACCESS, show_alert=True)
        return

    if not row.is_active:
        # The card is live but its send has not been flipped on yet. Give the
        # anchor handler the seconds it needs instead of answering off a
        # half-finished row: this is the window between posting the card and
        # finishing the gates, and answering here turned every tap inside it into
        # a dead end.
        settled = await _await_activation(whisper_id)
        if settled is None:
            # Rejected while we waited — the row is gone for good.
            await _retire_dead_card(callback, bot)
            await callback.answer(
                "⛔️ این نجوا دیگر موجود نیست یا منقضی شده است.\n\n"
                "از فرستنده بخواهید پیام را دوباره بفرستد.",
                show_alert=True,
            )
            return
        row = settled

    if not row.is_active and whisper_id in _delivering:
        # Still in flight after the wait — the anchor handler is between API
        # calls. Finalising from here would run the economy gate a second time
        # and charge the sender twice for one whisper, which is the one thing
        # every "let the reader finish it" shortcut in this module costs.
        await callback.answer(_SENDING_TEXT, show_alert=True)
        return

    if not row.is_active:
        # Still inactive after the wait, so this is no longer the in-flight
        # window Telegram opens between publishing a card and telling us about
        # it. Two cases remain, and only one of them is a real "not yet":
        #
        #   a) the SENDER is parked behind the forced-join gate. That row is
        #      meant to wait — the sender clears it with «عضو شدم», which
        #      activates it through the same gates — so it is left alone and
        #      the reader is told the truth.
        #   b) the send was never validated AT ALL, because the
        #      ``chosen_inline_result`` that does that never reached us. That
        #      update is a best-effort post-event: one lost, or eaten by a
        #      second instance polling the same token, and the row stayed
        #      reserved forever. Answering «هنوز ثبت نشده است» there left a
        #      whisper that had been sent, and often paid for, permanently
        #      unreadable — no amount of retrying ever helped.
        #
        # In case (b) the reservation is COMPLETE (sender, target, secret,
        # delivery), so the send is finalised right here, under exactly the
        # same gates — same order, same forced-join check, same single charge.
        # The sender cannot skip a rule by having their update dropped, and the
        # reader is never stuck on a card that will never open.
        if await _send_parked_behind_join(bot, row.sender_id):
            await _prompt_sender_to_join(bot, whisper_id, row.sender_id)
            await callback.answer(_JOIN_PENDING_TEXT, show_alert=True)
            return

        logger.warning(
            "Inline whisper %s: the card is live but the send was never "
            "validated (no chosen_inline_result reached us) — finalising it now.",
            whisper_id,
        )
        # Keep the card id on the row: without it the orphan sweep would treat
        # this whisper as an abandoned reservation and delete it an hour later,
        # turning a readable card into a dead button.
        await _attach_card(whisper_id, callback.inline_message_id)
        outcome = await _run_send_gates(
            bot,
            token=whisper_id,
            sender_id=row.sender_id,
            target_id=row.target_id,
            target_name=_user_name(row.target_id),
            inline_message_id=row.inline_message_id or callback.inline_message_id,
        )
        if outcome != _GATE_OK:
            if outcome == _GATE_JOIN:
                await callback.answer(_JOIN_PENDING_TEXT, show_alert=True)
            else:
                # Rejected for good: the row is gone and the button with it.
                await _retire_dead_card(callback, bot)
                await callback.answer(
                    "⛔️ این نجوا دیگر موجود نیست یا منقضی شده است.\n\n"
                    "از فرستنده بخواهید پیام را دوباره بفرستد.",
                    show_alert=True,
                )
            return

        refreshed = await _get_row(whisper_id)
        if refreshed is not None:
            row = refreshed

    # ── Steps C & D — force-sub ──
    # ``missing_required_chats`` already returns [] when the admin has the
    # requirement switched off, so "disabled" and "nothing configured" both
    # land here as an empty list and skip the channel checks entirely.
    # force_refresh: the user just tapped «عضو شدم»/«بررسی عضویت» and expects
    # an answer now, so a stale 30s cache entry must not send them in circles.
    missing = await missing_required_chats(
        callback.bot, callback.from_user.id, force_refresh=True
    )

    if missing:
        # Re-key the card in place instead of posting to the group, and say
        # why in a private alert. Re-keying is safe here precisely because of
        # Step A: only the two parties ever reach this branch.
        await _apply_card_kb(
            callback, bot, whisper_verify_kb(missing, whisper_id)
        )
        await callback.answer(
            "⚠️ برای مشاهده پیام باید ابتدا در کانال(های) زیر عضو شوید.",
            show_alert=True,
        )
        return

    # ── All clear ──
    # THIS is where a whisper earns its action buttons, and only when the reader
    # is the person it was written for. The sender may press their own card —
    # «👁️ نمایش پیام» is on it too — and that must neither reveal the buttons nor
    # take them away again if the recipient already unlocked them, so the flag is
    # written once, here, and read from the row everywhere else.
    #
    # The same first read also stamps the receipt onto the card. Both are driven
    # off this single flag flip on purpose: the read receipt is the claim "the
    # recipient opened it", and a claim that can be made twice — or once by the
    # sender, or once without the keyboard unlocking — would stop being a claim.
    first_read = False
    if callback.from_user.id == row.target_id and not row.target_viewed:
        if await _mark_target_viewed(row.id):
            row.target_viewed = True
            first_read = True

    # Restore whatever keyboard this card should be carrying (see
    # :func:`_card_keyboard`): the card may still be showing join buttons from an
    # earlier tap, and leaving them there would advertise a channel the reader
    # has already joined.
    #
    # On the first read this is a TEXT edit as well, because the receipt has to
    # land on the card while we are already rewriting it — a second edit would be
    # a second round trip for one message. Afterwards it is a keyboard-only swap,
    # so the sender re-opening their own card never re-appends the line.
    if first_read:
        await _edit_card(
            callback,
            bot,
            _card_text_after_read(row),
            _card_keyboard(whisper_id, row),
        )
    else:
        await _apply_card_kb(callback, bot, _card_keyboard(whisper_id, row))

    secret = row.secret_text
    if not row.is_viewed:
        # Re-fetch under a session: ``row`` is detached from the closed one.
        async with async_session_factory() as session:
            managed = await session.get(InlineWhisper, row.id)
            if managed is not None and not managed.is_viewed:
                managed.is_viewed = True
                await session.commit()

    # The DM overflow of an over-long secret goes to whoever is reading, which
    # is the target in every ordinary case and the sender when they re-open their
    # own card.
    await _reveal_secret(callback, bot, callback.from_user.id, secret)


# ──────────────────────────────────────────────────
# 4b. The card's action buttons
# ──────────────────────────────────────────────────
#
# «📊 آمار», «⚙️ گزینه‌ها» and «🗑️ حذف» all sit on the SAME card the
# read button does, which means they are all pressed by the same crowd: two
# people who are meant to be there and a chat full of people who are not. None of
# them can be hidden — Telegram freezes the markup when the message is published
# — so every one of these handlers starts by asking the row who is asking.
#
# The ids come from the row, never from ``callback_data``: a button that carried
# the parties with it could disagree with the database, and the database is the
# only thing that knows who a whisper is between.
#
# «↩️ پاسخ» is not in this block because it is not a callback — its identity
# check lives at the other end of the same row, in :func:`_reply_target`.


@router.callback_query(F.data.regexp(_STATS_RE))
async def cb_whisper_stats(callback: CallbackQuery, bot: Bot) -> None:
    """«📊 آمار» → who pressed «👁️ نمایش پیام» without being one of the two parties.

    Sender AND target only. The list names people, so handing it to anyone else
    would republish the very thing the whisper protected; and refusing it to an
    outsider costs the two parties nothing. The refusal is deliberately the same
    sentence whether the list is empty or not, so the button cannot be used to
    find out that a whisper exists at all.
    """
    whisper_id = callback.data.split(":", 1)[1]
    row = await _get_row(whisper_id)
    if row is None:
        await callback.answer(_GONE_ALERT, show_alert=True)
        return
    if not _is_party(row, callback.from_user.id):
        await callback.answer(_NO_STATS_ACCESS, show_alert=True)
        return
    await callback.answer(_stats_text(row), show_alert=True)


#: What «⚙️ گزینه‌ها» says. A description rather than a second keyboard: the card
#: already carries every action, and a submenu would mean two more callbacks
#: whose only job is to undo being in a submenu. Measured against the 200-char
#: alert ceiling, like every other string answered here.
_OPTIONS_TEXT = (
    "⚙️ <b>گزینه‌های این نجوا</b>\n\n"
    "👁️ نمایش — متن پیام، فقط برای فرستنده و گیرنده\n"
    "↩️ پاسخ — نوشتن پاسخ برای طرف دیگر\n"
    "🗑️ حذف — فقط فرستنده می‌تواند\n"
    "📊 آمار — چه کسانی روی «نمایش» زده‌اند"
)


@router.callback_query(F.data.regexp(_OPTIONS_RE))
async def cb_whisper_options(callback: CallbackQuery, bot: Bot) -> None:
    """«⚙️ گزینه‌ها» → what each button on this card does, and who may press it.

    Answered to the two parties only, for the same reason as the stats: the card
    in front of a stranger is not a place to advertise that a whisper exists,
    let alone how to open one.

    Purely informational — it changes neither the row nor the keyboard, so a card
    can be read as often as its owner likes without anything moving.
    """
    whisper_id = callback.data.split(":", 1)[1]
    row = await _get_row(whisper_id)
    if row is None:
        await callback.answer(_GONE_ALERT, show_alert=True)
        return
    if not _is_party(row, callback.from_user.id):
        await callback.answer(_NO_OPTIONS_ACCESS, show_alert=True)
        return
    # Alerts render as plain text, so the tags would show up literally.
    await callback.answer(_OPTIONS_TEXT.replace("<b>", "").replace("</b>", ""),
                          show_alert=True)


@router.callback_query(F.data.regexp(_DELETE_RE))
async def cb_whisper_delete(callback: CallbackQuery, bot: Bot) -> None:
    """«🗑️ حذف» → the sender takes the whisper back, card and all.

    Sender only, and NOT the target: they can read every word of it, but
    unsending somebody else's message is not theirs to decide.

    The row goes first and the card is rewritten second — the order
    :func:`_undo` documents. Deleting last would mean a failed edit leaves a live
    button whose row is gone, so the next tap answers «no longer available»
    about a card that still looks perfectly usable. Deleting first means the
    worst case is a card whose buttons stopped working, which is at least the
    truth.
    """
    whisper_id = callback.data.split(":", 1)[1]
    row = await _get_row(whisper_id)
    if row is None:
        await callback.answer(_GONE_ALERT, show_alert=True)
        return
    if row.sender_id != callback.from_user.id:
        await callback.answer(_NO_DELETE_ACCESS, show_alert=True)
        return

    # We just proved the tapper is the sender, and a whisper to yourself is
    # refused at every entry point, so the other party is the target by
    # elimination.
    await _delete_row(whisper_id)
    await _edit_card(callback, bot, _DELETED_CARD_TEXT)
    await callback.answer("🗑 این نجوا حذف شد.")
    # The target is told in private rather than left to wonder why the card
    # stopped answering — best effort, and silent when they never started us.
    await _send_dm(
        bot,
        row.target_id,
        "🗑 <b>نجوای فرستنده حذف شد.</b>\n\n"
        "فرستنده پیام را پیش از آنکه باز کنی پاک کرد.",
    )


# ──────────────────────────────────────────────────
# 5. The stubs Telegram posted in the source chat
# ──────────────────────────────────────────────────

class OwnInlineMessage(BaseFilter):
    """True only for text messages produced from THIS bot's inline results.

    ``fsm_storage`` is no longer involved: the request lives entirely inside
    the chosen-result update, so the only thing left to gate on is authorship
    — otherwise an unrelated bot's inline message would be deleted too.
    """

    async def __call__(self, message: Message, bot: Bot) -> bool:
        via = message.via_bot
        return bool(via and via.id == bot.id)


def _is_published_card(message: Message) -> bool:
    """True for a card that came out of our inline mode ON PURPOSE.

    Three families reach this handler and only the stubs may be deleted:

    * the **whisper card** and the three **menu cards** (tutorial, "whisper me",
      "anonymous chat request"). Each of them is content the user chose to
      publish, and each carries the only handle on the feature it announces —
      deleting the tutorial would delete the tutorial button with it.
    * the **anchor**, which :func:`on_inline_result_message` has already handled
      before asking here. Listed anyway so that an error thrown in that handler
      can never turn an undelivered whisper into a deleted anchor.
    * the **rejection card** (the query parsed but a gate refused it). Pure
      noise, safe to remove, and what this handler exists for.

    Authorship is already proven by :class:`OwnInlineMessage`; what has to be
    decided here is whether the message is a card or a stub.
    """
    markup = message.reply_markup
    if markup and markup.inline_keyboard:
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data or ""
                if re.fullmatch(_READ_RE, data) or re.fullmatch(
                    _LEGACY_READ_RE, data
                ):
                    return True
                # «💬 شروع چت ناشناس» — the anonymous-chat request card. Its
                # text alone is indistinguishable from the other menu cards, so
                # the button is the reliable marker.
                if data.startswith(ANON_REQUEST_CB_PREFIX):
                    return True

    text = message.text or ""
    return (
        text.startswith(_GROUP_CARD_PREFIX)
        or text.startswith(_ANCHOR_CARD_PREFIX)
        or text.startswith(MENU_CARD_PREFIXES)
    )


@router.callback_query(F.data == TUTORIAL_PHOTO_CALLBACK)
async def cb_tutorial_guide(callback: CallbackQuery) -> None:
    """Expand the tutorial card into its written, step-by-step form.

    The «🖼️ عکس آموزشی» button is a URL whenever ``TUTORIAL_PHOTO_URL`` is
    configured. When it is not, it becomes this callback — a button that leads
    nowhere is the one thing a brand-new user must never be handed, so the
    fallback teaches the same lesson in words.
    """
    await _edit_callback_message(callback, tutorial_guide_text(), None)


@router.message(OwnInlineMessage(), F.content_type == "text")
async def on_inline_result_message(message: Message, bot: Bot) -> None:
    """Turn the anchor into the card; delete the rejection stubs.

    This is the only handler in the module that writes into a chat, and it can
    do so because it is a ``message`` handler: the anchor Telegram published
    arrives here WITH its ``chat``, which is the one piece of information the
    inline pipeline never carries at any other point (see :func:`_handle_anchor`).

    What else reaches it is the **rejection** card: the query parsed but could
    not be sent, so what it says is worth reading in the picker and worthless in
    the chat. Deleting is best-effort by design — the bot may not be a member of
    that chat at all. Even when the delete fails nothing sensitive is left
    behind (a rejection never carries the secret) — but if the sender hand-wrote
    real content into it, they are warned so they can fix their habit.

    That last warning is the one place left in this module that messages a user
    directly, and it is deliberately not in the ``inline_query`` handler: this is
    a ``message`` handler for a message that already exists, it only fires when
    the bot genuinely could not clean up, and the user it writes to has by
    definition just published something from an inline result.
    """
    token = _anchor_token(message)
    if token:
        await _handle_anchor(bot, message, token)
        return

    if _is_published_card(message):
        return

    typed = (message.text or "").strip()
    if typed:
        bot_username = (await bot.me()).username or "bot"
        await _send_dm(
            bot,
            message.from_user.id,
            "⚠️ پیامی که در آن گفتگو فرستادید باقی ماند؛ ربات در آن گفتگو دسترسی "
            "حذف پیام ندارد.\n\n"
            "برای پنهان ماندن کامل، متن نجوا را داخل اینلاین و پشت "
            f"{TEXT_MARKER} بنویسید تا هرگز وارد گفتگو نشود:\n"
            f"<code>@{escape(bot_username)} {QUERY_EXAMPLE}</code>",
        )
