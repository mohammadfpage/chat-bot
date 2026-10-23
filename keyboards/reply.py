"""
Reply keyboards for all bot states.

Uses the native keyboard button color style feature (Bot API 8.2+):
  - style="primary"   → Blue (main actions)
  - style="success"   → Green (positive actions)
  - style="danger"    → Red (cancel / report)

The ``style`` field accepts plain strings in current aiogram versions.

NOT persistent on purpose — and now *literally* so: every markup here sets
``is_persistent=False`` instead of leaving it unset, so no client can decide on
its own that the keyboard should refuse to go away. A persistent keyboard traps
Android's system back button: the panel cannot be dismissed, and every later
screen keeps the old buttons visible.

Even a non-persistent keyboard still costs a back press (Android closes the
keyboard first, only the second press leaves the chat), which reads as "the
back button does nothing" to somebody who just wants out — so the main menu
carries its own one-tap exit, :data:`CLOSE_MENU_LABEL`, which answers with a
``ReplyKeyboardRemove``. Each wizard also REPLACES it with its own escape row,
so no screen is ever a dead end.
"""

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ──────────────────────────────────────────────────
# Universal escape hatch
# ──────────────────────────────────────────────────

#: Label of the button that drops the user back on the main menu from any
#: wizard page. A ``ReplyKeyboardMarkup`` fully REPLACES the previous one, so
#: without this row a user who walks into a sub-page has no way back.
BACK_TO_MENU = "🏠 منوی اصلی"

#: The one-tap way to take the whole reply keyboard off the screen. Android's
#: back gesture only *hides* a reply keyboard (a second press leaves the chat),
#: which users read as a broken back button — and the collapsed bar at the
#: bottom of the screen is not something everybody trusts either. This button
#: gives an unambiguous exit that works from every page it is offered on,
#: including from inside a wizard (it drops the FSM the same way
#: :data:`BACK_TO_MENU` does).
CLOSE_MENU_LABEL = "❌ بستن منو"

#: Everything the escape handler answers to, so the button keeps working even
#: if Telegram or a later edit changes its exact glyphs.
BACK_TO_MENU_TEXTS = frozenset(
    {BACK_TO_MENU, "🏠منوی اصلی", "منوی اصلی", "/start", "/menu"}
)

#: Label of the main-menu button that opens the inline-mode help card.
#: Declared here (not in the handler) so the keyboard and the
#: ``F.text == …`` match can never drift apart — and so whisper.py can add
#: it to its own "abandon the parked draft" escape set.
INLINE_HELP_LABEL = "📨 پیام ناشناس اینلاین"

#: Label of the main-menu button that opens the «🎧 پشتیبانی» ticket screen.
#: Declared here for the same two reasons as ``INLINE_HELP_LABEL``: the
#: ``F.text == …`` matcher in ``handlers.support`` and this keyboard must read
#: one string, and whisper.py folds it into its parked-draft escape set so a
#: user who taps it instead of sending the draft is not swallowed by the relay.
SUPPORT_LABEL = "🎧 پشتیبانی"

# ──────────────────────────────────────────────────
# The three ways to get matched
# ──────────────────────────────────────────────────
#
# Declared as constants so ``keyboards`` (which draws them), ``handlers.chat``
# (which matches on them) and ``utils.group_commands`` (which documents them) all
# read the same string. These are matched with ``F.text ==`` throughout, so a
# label typed by hand here and a label typed by hand in a handler is exactly the
# kind of duplication that silently disables a button.

#: The one main-menu entry that starts a match. It does NOT search by itself:
#: it opens :func:`match_mode_kb` so the user first says who they are looking
#: for instead of being dropped into a random queue by a button they misread.
CONNECT_LABEL = "💬 به یه ناشناس وصلم کن"

#: Free, matches anyone. This is an ANSWER to «با کی می‌خوای چت کنی؟», not the
#: main-menu entry, which is why it can never loop back into the chooser.
#: Short and spoken — the question is already on screen, so the button only
#: has to name the option («شانسی»), the way the other two name theirs.
RANDOM_CONNECT_LABEL = "🎲 شانسی"

#: What that button used to say. Kept as a real, still-matching value: a reply
#: keyboard already on a user's screen is not re-sent until the next tap, so
#: the old label has to keep working for one more round — it maps to the same
#: mode rather than to the "unknown text" hole.
LEGACY_RANDOM_CONNECT_LABEL = "🎲 فرقی نداره"

#: The other two spellings of the main-menu connect entry that older builds
#: installed. They are no longer drawn, but a keyboard already sitting on a
#: user's phone keeps them until the bot replaces it, so both must still start
#: a search (and must still escape a parked whisper) instead of falling into
#: the "unknown text" hole.
OLD_CONNECT_LABEL = "🔀 اتصال شانسی"
OLD_ANON_CONNECT_LABEL = "🔗 اتصال به ناشناس"

#: Costs one coin per established connection; matches women.
CHAT_WITH_GIRL_LABEL = "👩 چت با دختر"

#: Costs one coin per established connection; matches men.
CHAT_WITH_BOY_LABEL = "👨 چت با پسر"

#: The two gender answers offered during profile setup. Kept as constants for
#: the same reason the matching labels are: they are ``F.text``-matched, so the
#: keyboard and the handler must not spell them independently.
GENDER_MALE_LABEL = "👨 مرد"
GENDER_FEMALE_LABEL = "👩 زن"


def _back_row() -> list[KeyboardButton]:
    """The single "back to main menu" row appended to every sub-page."""
    return [KeyboardButton(text=BACK_TO_MENU)]

# ──────────────────────────────────────────────────
# Main Menu  (Idle state)
# ──────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    """
    Six rows, grouped by what the user is trying to do:

        Row 1: [💬 به یه ناشناس وصلم کن (success)]  — the ONE primary action
        Row 2: [👤 پروفایل من]  [📩 پیام‌های ناشناس من]
        Row 3: [🔗 لینک ناشناس من]  [🏆 امتیازات و سکه]
        Row 4: [🎁 دعوت دوستان]  [📨 پیام ناشناس اینلاین]
        Row 5: [📖 راهنما و قوانین]  [🎧 پشتیبانی]
        Row 6: [⛔️ لیست مسدودی‌ها (Danger)]

    Only four buttons carry a colour: the connect action is green, the paid
    choices inside its chooser are blue, and the blocked list is red. Tinting
    every ordinary menu row made the panel look like a warning screen and hid
    the one action that matters — so navigation rows stay uncoloured.

    Row 6 doubles the blocked list with the menu's own close button: both are
    "get this screen off me" actions, and giving the close its own row would
    make a seven-row keyboard out of a six-row one.
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text=CONNECT_LABEL, style="success")],
            [
                KeyboardButton(text="👤 پروفایل من"),
                KeyboardButton(text="📩 پیام‌های ناشناس من"),
            ],
            [
                KeyboardButton(text="🔗 لینک ناشناس من"),
                KeyboardButton(text="🏆 امتیازات و سکه"),
            ],
            [
                KeyboardButton(text="🎁 دعوت دوستان"),
                KeyboardButton(text=INLINE_HELP_LABEL),
            ],
            [
                KeyboardButton(text="📖 راهنما و قوانین"),
                KeyboardButton(text=SUPPORT_LABEL),
            ],
            [
                KeyboardButton(text="⛔️ لیست مسدودی‌ها", style="danger"),
                KeyboardButton(text=CLOSE_MENU_LABEL),
            ],
        ],
    )


def match_mode_kb() -> ReplyKeyboardMarkup:
    """Who do you want to talk to?

    The three answers are exactly the three modes ``handlers.chat`` already
    understands. «شانسی» is free unless the admin prices ``random_chat_cost``;
    the other two cost one connection, and are the only rows tinted — so the
    choice that spends coins is visible before it is tapped, while the free
    one is the obvious first row.
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text=RANDOM_CONNECT_LABEL, style="success")],
            [
                KeyboardButton(text=CHAT_WITH_GIRL_LABEL, style="primary"),
                KeyboardButton(text=CHAT_WITH_BOY_LABEL, style="primary"),
            ],
            _back_row(),
        ],
    )


# ──────────────────────────────────────────────────
# In-Queue Menu  (Searching state)
# ──────────────────────────────────────────────────

def queue_menu_kb() -> ReplyKeyboardMarkup:
    """Cancel-search keyboard with a danger-styled button."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text="❌ لغو جستجو", style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# In-Chat Menu  (Connected state)
# ──────────────────────────────────────────────────

def chat_menu_kb() -> ReplyKeyboardMarkup:
    """
    Row 1: [❌ لغو چت (Danger)]
    Row 2: [🛑 گزارش کاربر / بلاک (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text="❌ لغو چت", style="danger")],
            [KeyboardButton(text="🛑 گزارش کاربر / بلاک", style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# Chat Ended Menu  (after «لغو چت» / expiry)
# ──────────────────────────────────────────────────

#: Label of the "start a new match right away" button shown under a chat-ended
#: card. Declared here so ``handlers.chat`` (the F.text matcher),
#: ``handlers.whisper`` (the parked-draft escape set) and this keyboard all
#: read the same string — a drifted label is exactly the dead button this
#: keyboard exists to prevent.
NEXT_CHAT_LABEL = "🔍 چت بعدی"

#: Label of the "reconnect with the partner I just finished with" button.
#: Reply rather than inline on purpose: the target lives in handlers.chat's
#: ``last_partner`` map, because a reply button cannot carry a user id and the
#: end card must not print one either.
REMATCH_LABEL = "🔁 اتصال مجدد"


def chat_end_kb(has_rematch: bool = True) -> ReplyKeyboardMarkup:
    """The keyboard every chat-ended card is sent with.

    Layout:
        Row 1: [🔍 چت بعدی (success)]   — the habit: one tap, next partner
        Row 2: [🔁 اتصال مجدد (primary)] — only while a previous partner exists
        Row 3: [🏠 منوی اصلی (primary)]  — the same bottom row as every page

    This REPLACES the in-chat keyboard. The previous design attached an inline
    rematch button to the card and left «لغو چت»/«بلاک» on screen below it —
    both dead once the state went idle, so a user who tapped them got nothing
    at all and no visible way to start the next chat.
    """
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=NEXT_CHAT_LABEL, style="success")],
    ]
    if has_rematch:
        rows.append([KeyboardButton(text=REMATCH_LABEL)])
    rows.append(_back_row())
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=rows,
    )


# ──────────────────────────────────────────────────
# Profile Setup keyboards (FSM step-by-step)
# ──────────────────────────────────────────────────

def age_kb() -> ReplyKeyboardMarkup:
    """Age selection grid (16–50, green) + cancel (red) + back to main menu."""
    ages = [KeyboardButton(text=str(a)) for a in range(16, 51)]
    rows = [ages[i : i + 5] for i in range(0, len(ages), 5)]
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    rows.append(_back_row())
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=rows,
    )


def city_kb() -> ReplyKeyboardMarkup:
    """Common cities quick-select (blue) + custom + cancel (red) + back."""
    cities = [
        "تهران", "اصفهان", "شیراز", "تبریز", "مشهد",
        "کرج", "اهواز", "قم", "کرمان", "ارومیه",
    ]
    rows = [
        [KeyboardButton(text=c) for c in cities[i : i + 2]]
        for i in range(0, len(cities), 2)
    ]
    rows.append([KeyboardButton(text="✏️ شهر دیگر")])
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    rows.append(_back_row())
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=rows,
    )


def height_kb() -> ReplyKeyboardMarkup:
    """Height selection grid (green) + cancel (red) + back to main menu."""
    heights = [
        KeyboardButton(text=f"{h} سانتی‌متر")
        for h in range(150, 210, 5)
    ]
    rows = [heights[i : i + 2] for i in range(0, len(heights), 2)]
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    rows.append(_back_row())
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=rows,
    )


def gender_kb() -> ReplyKeyboardMarkup:
    """Gender choice during profile setup.

    Not decoration: «چت با دختر» and «چت با پسر» match against this field, so a
    profile that omits it can never be matched by either of them.
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [
                KeyboardButton(text=GENDER_MALE_LABEL),
                KeyboardButton(text=GENDER_FEMALE_LABEL),
            ],
            [KeyboardButton(text="❌ انصراف", style="danger")],
            _back_row(),
        ],
    )


def set_gender_kb() -> ReplyKeyboardMarkup:
    """Shown on the profile card of a user whose profile predates gender.

    Users who completed their profile before gender existed have no value here,
    which means the two paid matching buttons could never pair them. This is the
    one-tap fix offered directly on their own profile rather than making them
    re-run the whole wizard.
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [
                KeyboardButton(text=GENDER_MALE_LABEL),
                KeyboardButton(text=GENDER_FEMALE_LABEL),
            ],
            _back_row(),
        ],
    )


def confirm_profile_kb() -> ReplyKeyboardMarkup:
    """Confirm (green), edit profile (blue) or leave the wizard entirely."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [
                KeyboardButton(text="✅ تایید", style="success"),
                KeyboardButton(text="✏️ ویرایش"),
            ],
            [
                KeyboardButton(text="❌ لغو", style="danger"),
                KeyboardButton(text=BACK_TO_MENU),
            ],
        ],
    )


def profile_photo_choice_kb() -> ReplyKeyboardMarkup:
    """
    'Show profile photo?' choice shown at the end of profile setup.
    Row 1: [🖼 آخرین عکس پروفایل تلگرام]  [📷 ارسال عکس دلخواه]
    Row 2: [🙈 بدون عکس (Danger)]
    Row 3: [🏠 منوی اصلی]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [
                KeyboardButton(text="🖼 آخرین عکس پروفایل تلگرام"),
                KeyboardButton(text="📷 ارسال عکس دلخواه"),
            ],
            [KeyboardButton(text="🙈 بدون عکس", style="danger")],
            _back_row(),
        ],
    )


def photo_upload_kb() -> ReplyKeyboardMarkup:
    """Shown while waiting for the user to send a custom profile photo."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text="❌ انصراف", style="danger")],
            _back_row(),
        ],
    )


# ──────────────────────────────────────────────────
# Anonymous Inbox Chat Menu  (Inbox live chat)
# ──────────────────────────────────────────────────

def anonymous_chat_menu_kb() -> ReplyKeyboardMarkup:
    """Keyboard shown during a live anonymous inbox chat session.
    Row 1: [❌ پایان چت / بازگشت (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text="❌ پایان چت / بازگشت", style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# Anonymous 1-on-1 Session Keyboard  (real-time secret chat)
# ──────────────────────────────────────────────────

#: Label of the button that ends a live anonymous 1-on-1 chat. Declared here
#: (not in the handler) so the keyboard and the ``F.text == …`` match — the
#: :mod:`handlers.anon_chat` router that relays everything else the user types
#: — cannot drift apart. Typing it is the escape hatch for a user whose inline
#: "end chat" button is several messages back.
ANON_SESSION_END_LABEL = "❌ پایان چت ناشناس"


def anon_session_menu_kb() -> ReplyKeyboardMarkup:
    """
    Keyboard shown during a live anonymous 1-on-1 secret chat.

    Deliberately ONE button. Every other key of this keyboard would be relayed
    to the stranger on the other side as chat text, so the keyboard must not
    offer anything that is not a control.

    Private chats only — the keyboard guard in ``middleware.keyboard_guard.py``
    rewrites this markup into a removal for any group, so it can never be used
    to talk inside one.

    Row 1: [❌ پایان چت ناشناس (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        keyboard=[
            [KeyboardButton(text=ANON_SESSION_END_LABEL, style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# Bot-control labels  (never user payload)
# ──────────────────────────────────────────────────

#: Every reply-keyboard label that is a CONTROL, not something a user would
#: mean to type as chat text. A live anonymous session relays arbitrary text to
#: a stranger, so a menu button tapped out of habit must never be delivered to
#: the other side — that is the bug this set exists to close. Values that could
#: plausibly be real content (age numbers, city names, height strings) are
#: deliberately left out so a genuine message is never mistaken for a button.
BOT_CONTROL_TEXTS = frozenset(
    {
        BACK_TO_MENU,
        CONNECT_LABEL,
        RANDOM_CONNECT_LABEL,
        LEGACY_RANDOM_CONNECT_LABEL,
        OLD_CONNECT_LABEL,
        OLD_ANON_CONNECT_LABEL,
        CLOSE_MENU_LABEL,
        CHAT_WITH_GIRL_LABEL,
        CHAT_WITH_BOY_LABEL,
        INLINE_HELP_LABEL,
        NEXT_CHAT_LABEL,
        REMATCH_LABEL,
        ANON_SESSION_END_LABEL,
        "👤 پروفایل من",
        "📩 پیام‌های ناشناس من",
        "🔗 لینک ناشناس من",
        "🏆 امتیازات و سکه",
        "🎁 دعوت دوستان",
        "📖 راهنما و قوانین",
        SUPPORT_LABEL,
        "⛔️ لیست مسدودی‌ها",
        "❌ لغو جستجو",
        "❌ لغو چت",
        "🛑 گزارش کاربر / بلاک",
        "❌ پایان چت / بازگشت",
        "👑 پنل مدیریت",
        "👤 پنل کاربری",
    }
)


def is_bot_control_text(text: str | None) -> bool:
    """True when ``text`` is a ``/command`` or a bot control label.

    Live anonymous sessions funnel every message to a stranger. The relay
    handlers call this to refuse a menu button or a command, so a user who
    forgets to close the chat cannot leak the button text (or a command) to the
    other side. Command detection is by the leading slash, which also covers
    ``/start@bot <payload>`` deep links.
    """
    if not text:
        return False
    return text.startswith("/") or text in BOT_CONTROL_TEXTS

