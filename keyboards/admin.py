"""
Admin panel inline keyboards.

Every button follows the dual-fallback emoji strategy from
``utils/emojis.py``:

  1. ``get_plain_emoji(key)`` injects the plain Unicode emoji into ``text``
     (visible today without a Premium subscription).
  2. ``icon_custom_emoji_id=get_premium_id(key)`` always passes the premium
     ID if present; it safely returns ``None`` today, which aiogram ignores.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.emojis import get_plain_emoji, get_premium_id
from utils.economy import fmt_coins

# ──────────────────────────────────────────────────────────────────────────
# Constants — shared callback prefixes/values
# ──────────────────────────────────────────────────────────────────────────
BACK_TO_PANEL = "admin:panel"


# ──────────────────────────────────────────────────────────────────────────
# Internal helper — builds a single, consistently-styled inline button.
# ──────────────────────────────────────────────────────────────────────────
def _button(
    text: str,
    callback_data: str,
    emoji: str,
    *,
    style: str = "primary",
) -> InlineKeyboardButton:
    """Create an inline button with an emoji and native styling."""
    return InlineKeyboardButton(
        text=f"{get_plain_emoji(emoji)} {text}",
        callback_data=callback_data,
        style=style,
        icon_custom_emoji_id=get_premium_id(emoji),
    )


# ──────────────────────────────────────────────────────────────────────────
# Admin Panel — main menu
# ──────────────────────────────────────────────────────────────────────────
def admin_panel_kb(*, is_root: bool = False) -> InlineKeyboardMarkup:
    """
    Main admin panel — grouped by what the admin is actually doing.

    The rows are sections, not leftovers of insertion order:

        [📊 آمار زنده]        [📢 ارسال همگانی]   monitoring
        [👥 مدیریت کاربران]   [🎁 هدیه دادن]      people
        [🪙 هزینهٔ سرویس‌ها]  [🏆 پاداش‌ها]        economy — what things COST
        [🛡️ ضداسپم]           [🔒 تنظیمات نجوا]    rules & features
        [📢 عضویت اجباری]     [📈 گزارش سکه]       entry gate + reports
        ([💾 پشتیبان‌گیری] [👑 ادمین‌ها])          root only

    Two per row keeps every label inside Telegram's truncation width, and the
    last ``.adjust`` value repeats for the final row — so the keyboard re-flows
    to 10 buttons (no root) or 11 (root) without the layout being written
    twice.

    Args:
        is_root: ``True`` when the viewer is a Root (Super) Admin.
    """
    builder = InlineKeyboardBuilder()

    # ── Monitoring ──
    builder.button(
        text=f"{get_plain_emoji('stats')} آمار زنده",
        callback_data="admin:stats",
        style="primary",
        icon_custom_emoji_id=get_premium_id("stats"),
    )
    builder.button(
        text=f"{get_plain_emoji('broadcast')} ارسال همگانی",
        callback_data="admin:broadcast",
        style="primary",
        icon_custom_emoji_id=get_premium_id("broadcast"),
    )

    # ── People ──
    builder.button(
        text=f"{get_plain_emoji('users')} مدیریت کاربران",
        callback_data="admin:users",
        style="primary",
        icon_custom_emoji_id=get_premium_id("users"),
    )
    builder.button(
        text=f"{get_plain_emoji('gift')} هدیه دادن",
        callback_data="admin:gift",
        style="success",
        icon_custom_emoji_id=get_premium_id("gift"),
    )

    # ── Economy: what costs, what pays out ──
    builder.button(
        text=f"{get_plain_emoji('coins')} هزینهٔ سرویس‌ها",
        callback_data="admin:costs",
        style="success",
        icon_custom_emoji_id=get_premium_id("coins"),
    )
    builder.button(
        text=f"{get_plain_emoji('points')} پاداش‌ها و جوایز",
        callback_data="admin:rewards",
        style="success",
        icon_custom_emoji_id=get_premium_id("points"),
    )

    # ── Rules & features ──
    builder.button(
        text=f"{get_plain_emoji('blocked')} ضداسپم و محدودیت",
        callback_data="admin:limits",
        style="primary",
        icon_custom_emoji_id=get_premium_id("blocked"),
    )
    builder.button(
        text=f"{get_plain_emoji('locked')} تنظیمات نجوا",
        callback_data="admin:whisper",
        style="primary",
        icon_custom_emoji_id=get_premium_id("locked"),
    )
    # Force-join is a global gate, not a whisper setting, so it gets its own
    # section on the panel instead of hiding inside «تنظیمات نجوا».
    builder.button(
        text=f"{get_plain_emoji('broadcast')} عضویت اجباری",
        callback_data="admin:forcejoin",
        style="primary",
        icon_custom_emoji_id=get_premium_id("broadcast"),
    )

    # ── Reports ──
    builder.button(
        text=f"{get_plain_emoji('chart')} گزارش سکه و رفرال",
        callback_data="admin:report",
        style="primary",
        icon_custom_emoji_id=get_premium_id("chart"),
    )
    if is_root:
        builder.button(
            text=f"{get_plain_emoji('database')} پشتیبان‌گیری",
            callback_data="admin:backup",
            style="primary",
            icon_custom_emoji_id=get_premium_id("database"),
        )
        builder.button(
            text=f"{get_plain_emoji('crown')} مدیریت ادمین‌ها",
            callback_data="admin:admins",
            style="danger",
            icon_custom_emoji_id=get_premium_id("crown"),
        )
    builder.adjust(2, 2, 2, 2, 2)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Live Stats
# ──────────────────────────────────────────────────────────────────────────
def admin_stats_kb() -> InlineKeyboardMarkup:
    """Keyboard under live stats — refresh or go back."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('refresh')} به‌روزرسانی",
        callback_data="admin:stats",
        style="primary",
        icon_custom_emoji_id=get_premium_id("refresh"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="primary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Broadcast (confirmation step)
# ──────────────────────────────────────────────────────────────────────────
def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Confirm or cancel the broadcast before it is actually sent."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} ارسال کن",
        callback_data="admin:broadcast:confirm",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:broadcast:cancel",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.adjust(2)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# User management (ban / unban)
# ──────────────────────────────────────────────────────────────────────────
def admin_users_kb() -> InlineKeyboardMarkup:
    """Sub-menu: ban / unban, special flags, and the report inbox."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('ban')} بلاک‌کردن کاربر",
        callback_data="admin:user:ban",
        style="danger",
        icon_custom_emoji_id=get_premium_id("ban"),
    )
    builder.button(
        text=f"{get_plain_emoji('unban')} رفع بلاک کاربر",
        callback_data="admin:user:unban",
        style="success",
        icon_custom_emoji_id=get_premium_id("unban"),
    )
    builder.button(
        text=f"{get_plain_emoji('crown')} وضعیت ویژهٔ کاربر",
        callback_data="admin:user:flags",
        style="primary",
        icon_custom_emoji_id=get_premium_id("crown"),
    )
    builder.button(
        text=f"{get_plain_emoji('queue')} ورودی گزارش‌ها",
        callback_data="admin:reports",
        style="primary",
        icon_custom_emoji_id=get_premium_id("queue"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="primary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def admin_user_flags_kb(
    tg_id: int,
    *,
    is_vip: bool,
    has_subscription: bool,
    is_exempt: bool,
) -> InlineKeyboardMarkup:
    """Toggle switches for one user's paywall flags.

    The check mark is part of the label so the card reads as a status AND a
    control: the admin sees the current value without opening anything.
    Callback data carries the target id — these buttons can outlive the FSM
    state that produced them, and a stale state would retoggle the wrong user.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'✅' if is_vip else '❌'} اشتراک ویژه (VIP)",
        callback_data=f"admin:flags:toggle:is_vip:{tg_id}",
        style="success" if is_vip else "danger",
    )
    builder.button(
        text=f"{'✅' if has_subscription else '❌'} اشتراک خریداری‌شده",
        callback_data=f"admin:flags:toggle:has_subscription:{tg_id}",
        style="success" if has_subscription else "danger",
    )
    builder.button(
        text=f"{'✅' if is_exempt else '❌'} معاف از هزینهٔ اتصال",
        callback_data=f"admin:flags:toggle:is_exempt:{tg_id}",
        style="success" if is_exempt else "danger",
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به مدیریت کاربران",
        callback_data="admin:users",
        style="primary",
    )
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Root-only: manage admins
# ──────────────────────────────────────────────────────────────────────────
def admin_manage_admins_kb() -> InlineKeyboardMarkup:
    """Promote / demote admins (Root Admins only)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} ارتقا به ادمین",
        callback_data="admin:admins:promote",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} برکناری ادمین",
        callback_data="admin:admins:demote",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.button(
        text=f"{get_plain_emoji('users')} لیست ادمین‌ها",
        callback_data="admin:admins:list",
        style="primary",
        icon_custom_emoji_id=get_premium_id("users"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="primary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(2, 1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Cancel helper — used inside FSM "enter an ID / username" flows
# ──────────────────────────────────────────────────────────────────────────
def admin_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel the current admin input flow and return to the panel."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:cancel_input",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Bot policy — the sectioned control surface
# ──────────────────────────────────────────────────────────────────────────
# Three sections, because the old screen put 11 undifferentiated numbers in
# front of the admin and made them find one by reading. Each dict maps a
# ``bot_policy`` column to ``(callback suffix, emoji key, button label)``; a key
# in here is a column ``handlers/admin.py`` will write, so adding a field is all
# that is required to make it tunable. There is no hardcoded reward anywhere
# else — «دعوت دوستان» reads whatever ``referral_coin_reward`` holds at the
# moment it is paid.

#: What a service costs — free (0) or a price the admin sets.
COST_FIELDS: dict[str, tuple[str, str, str]] = {
    "random_chat_cost": ("random", "dice", "اتصال شانسی"),
    "chat_girl_cost": ("girl", "female", "چت با دختر"),
    "chat_boy_cost": ("boy", "male", "چت با پسر"),
    "whisper_cost": ("whisper", "locked", "نجوا"),
}

#: What the bot pays out.
REWARD_FIELDS: dict[str, tuple[str, str, str]] = {
    "welcome_coins": ("welcome", "coins", "سکهٔ خوش‌آمد"),
    "daily_bonus_coins": ("daily", "calendar", "جایزهٔ روزانه"),
    "referral_coin_reward": ("ref_coins", "gift", "پاداش رفرال"),
    "referral_premium_days": ("ref_days", "premium", "روز اشتراک رفرال"),
    "referral_invitee_coins": ("ref_invitee", "users", "هدیهٔ دعوت‌شده"),
}

#: Rate limits and chat rules — nothing here charges anybody.
LIMIT_FIELDS: dict[str, tuple[str, str, str]] = {
    "messages_per_minute": ("per_min", "fire", "پیام در دقیقه"),
    "messages_per_hour": ("per_hour", "clock", "پیام در ساعت"),
    "vip_multiplier": ("vip_mult", "premium", "ضریب کاربر ویژه"),
    "chat_lifetime_hours": ("chat_hours", "calendar", "مدت مجاز چت (ساعت)"),
}

#: section name -> its fields. The edit handlers key off this to know which
#: screen to come BACK to after a value is saved.
SECTION_FIELDS: dict[str, dict[str, tuple[str, str, str]]] = {
    "costs": COST_FIELDS,
    "rewards": REWARD_FIELDS,
    "limits": LIMIT_FIELDS,
}

#: Every editable column in one place (suffixes are unique across sections).
POLICY_EDIT_FIELDS: dict[str, tuple[str, str, str]] = {
    **COST_FIELDS,
    **REWARD_FIELDS,
    **LIMIT_FIELDS,
}

_POLICY_SUFFIX_TO_FIELD = {v[0]: k for k, v in POLICY_EDIT_FIELDS.items()}


def policy_field_from_suffix(suffix: str) -> str | None:
    """Map a callback suffix back to the policy attribute name."""
    return _POLICY_SUFFIX_TO_FIELD.get(suffix)


def policy_field_label(field_name: str) -> str:
    """Human label for a policy field name."""
    entry = POLICY_EDIT_FIELDS.get(field_name)
    return entry[2] if entry else field_name


def policy_field_emoji(field_name: str) -> str:
    """Registry key of the icon that stands for a policy field."""
    entry = POLICY_EDIT_FIELDS.get(field_name)
    return entry[1] if entry else "edit"


def policy_section_of(field_name: str) -> str | None:
    """Which section (``costs`` / ``rewards`` / ``limits``) owns a field."""
    for section, fields in SECTION_FIELDS.items():
        if field_name in fields:
            return section
    return None


def _price(value) -> str:
    """«رایگان» for 0, otherwise «N سکه» — a free service must not look broken."""
    value = value or 0
    return "رایگان" if value <= 0 else f"{fmt_coins(value)} سکه"


def _back_button(builder: InlineKeyboardBuilder) -> None:
    """The way out, always on its own row at the bottom."""
    builder.row(_button("بازگشت به منوی مدیریت", BACK_TO_PANEL, "back"))


def admin_costs_kb(policy) -> InlineKeyboardMarkup:
    """Per-service prices, each button showing what it currently costs.

    The value lives IN the button because that is the question the admin opened
    this screen to answer; a paid service is ``primary`` and a free one
    ``success``, so «which of these are free?» is answerable at a glance.
    """
    builder = InlineKeyboardBuilder()
    for field_name, (suffix, emoji_key, label) in COST_FIELDS.items():
        value = max(getattr(policy, field_name, 0) or 0, 0)
        builder.button(
            text=f"{get_plain_emoji(emoji_key)} {label}: {_price(value)}",
            callback_data=f"admin:costs:edit:{suffix}",
            style="success" if value <= 0 else "primary",
            icon_custom_emoji_id=get_premium_id(emoji_key),
        )
    _back_button(builder)
    # 4 prices (2+2) with the way out alone on the last row.
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def admin_rewards_kb(policy) -> InlineKeyboardMarkup:
    """What the bot pays out: welcome, daily, and the three referral numbers."""
    builder = InlineKeyboardBuilder()
    for field_name, (suffix, emoji_key, label) in REWARD_FIELDS.items():
        builder.button(
            text=f"{get_plain_emoji(emoji_key)} {label}: {getattr(policy, field_name, 0)}",
            callback_data=f"admin:rewards:edit:{suffix}",
            style="primary",
            icon_custom_emoji_id=get_premium_id(emoji_key),
        )
    _back_button(builder)
    # 5 rewards: two pairs, then the fifth alone, then the way out — an
    # adjustment whose last size repeats would otherwise strand the back
    # button in a row with a reward.
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def admin_limits_kb(policy) -> InlineKeyboardMarkup:
    """Anti-spam caps and chat rules — the screen that never charges anything."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=(
            f"{get_plain_emoji('cross')} غیرفعال کردن محدودیت"
            if policy.enabled
            else f"{get_plain_emoji('check')} فعال کردن محدودیت"
        ),
        callback_data="admin:limits:toggle",
        style="danger" if policy.enabled else "success",
        icon_custom_emoji_id=get_premium_id("cross" if policy.enabled else "check"),
    )
    for field_name, (suffix, emoji_key, label) in LIMIT_FIELDS.items():
        builder.button(
            text=f"{get_plain_emoji(emoji_key)} {label}: {getattr(policy, field_name, 0)}",
            callback_data=f"admin:limits:edit:{suffix}",
            style="primary",
            icon_custom_emoji_id=get_premium_id(emoji_key),
        )
    _back_button(builder)
    # The master switch gets a row of its own: it changes every number below
    # it, and sharing a row made it read as a peer of the caps.
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()


def admin_costs_edit_kb(suffix: str) -> InlineKeyboardMarkup:
    """Under the "enter a number" prompt: one-tap «رایگان» plus the way out.

    Typing ``0`` and pressing «رایگان» do the same thing, but the admin who
    wants a free service should not have to know that.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} رایگان کن (۰ سکه)",
        callback_data=f"admin:costs:free:{suffix}",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:cancel_input",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.adjust(1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Coin / referral report
# ──────────────────────────────────────────────────────────────────────────
def admin_report_kb() -> InlineKeyboardMarkup:
    """Under the coin report: search one user, refresh, or leave."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('search')} جستجوی کاربر",
        callback_data="admin:report:user",
        style="success",
        icon_custom_emoji_id=get_premium_id("search"),
    )
    builder.button(
        text=f"{get_plain_emoji('refresh')} به‌روزرسانی",
        callback_data="admin:report",
        style="primary",
        icon_custom_emoji_id=get_premium_id("refresh"),
    )
    _back_button(builder)
    builder.adjust(2, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Admin gifting
# ──────────────────────────────────────────────────────────────────────────
def admin_gift_kb() -> InlineKeyboardMarkup:
    """Choose what to gift: coins or a premium subscription.

    Two currencies used to be listed here (coins and tokens). Coins are the only
    one left, so the token row is gone rather than pointing at a column that no
    longer exists.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('coins')} هدیه سکه",
        callback_data="admin:gift:coins",
        style="primary",
        icon_custom_emoji_id=get_premium_id("coins"),
    )
    builder.button(
        text=f"{get_plain_emoji('premium')} هدیه اشتراک",
        callback_data="admin:gift:premium",
        style="success",
        icon_custom_emoji_id=get_premium_id("premium"),
    )
    builder.button(
        text=f"{get_plain_emoji('back')} بازگشت به منوی مدیریت",
        callback_data=BACK_TO_PANEL,
        style="primary",
        icon_custom_emoji_id=get_premium_id("back"),
    )
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def admin_gift_scope_kb(gift_type: str, user_count: int) -> InlineKeyboardMarkup:
    """Choose WHO receives the gift of ``gift_type`` ("coins" / "premium").

    Three scopes, one per row:

        [👤 کاربر مشخص]           → the original flow, keyed by telegram id
        [👥 همه کاربران (n نفر)]  → every non-banned account
        [🎲 n کاربر تصادفی]       → n sampled from that same population
        [🔙 تغییر نوع هدیه]        → back to the type menu

    ``user_count`` (non-banned accounts) is shown on the «همه» button so the
    admin sees the blast radius *before* committing to an amount.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('profile')} کاربر مشخص",
        callback_data=f"admin:gift:{gift_type}:one",
        style="primary",
    )
    builder.button(
        text=f"{get_plain_emoji('users')} همه کاربران ({user_count} نفر)",
        callback_data=f"admin:gift:{gift_type}:all",
        style="success",
    )
    builder.button(
        text=f"{get_plain_emoji('dice')} n کاربر تصادفی",
        callback_data=f"admin:gift:{gift_type}:random",
        style="primary",
    )
    builder.button(
        text=f"{get_plain_emoji('back')} تغییر نوع هدیه",
        callback_data="admin:gift",
        style="primary",
    )
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def admin_gift_confirm_kb() -> InlineKeyboardMarkup:
    """Confirm/cancel pair for a BULK gift — the last step before coins move.

    Single-recipient gifts still skip this (the amount is the whole risk and
    it was double-checked while typing); a mass credit cannot be undone, so
    «همه»/«تصادفی» must show the total and wait for an explicit yes.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{get_plain_emoji('check')} تأیید و اهداء",
        callback_data="admin:gift:confirm",
        style="success",
        icon_custom_emoji_id=get_premium_id("check"),
    )
    builder.button(
        text=f"{get_plain_emoji('cross')} لغو",
        callback_data="admin:cancel_input",
        style="danger",
        icon_custom_emoji_id=get_premium_id("cross"),
    )
    builder.adjust(1, 1)
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Whisper (نجوا) settings
# ──────────────────────────────────────────────────────────────────────────
def admin_whisper_kb(config) -> InlineKeyboardMarkup:
    """Keyboard for the group-whisper settings screen.

    Args:
        config: a ``WhisperConfig`` row.

    Layout — ONE BUTTON PER ROW, on purpose:
        [🔓/🔒 فعال/غیرفعال کردن نجوا]
        [🪙 تنظیم هزینهٔ نجوا]
        [✏️ حداکثر طول متن]
        [📊 آمار نجوا]
        [🔙 بازگشت به منوی مدیریت]

    Force-join (عضویت اجباری) used to live on this screen too. It does not
    anymore: the requirement is a GLOBAL gate that applies to far more than
    the whisper flow, so it moved to its own «عضویت اجباری» section on the
    panel. Everything that decides WHO must join where now lives there and
    nowhere else.

    The price button points at the shared «هزینهٔ سرویس‌ها» screen rather than
    a whisper-only editor: نجوا is one of four prices and an admin who wants to
    compare them should not have to bounce between two screens that each know
    half the answer.

    ``InlineKeyboardBuilder`` packs up to ``max_width`` (8) buttons into a single
    row, and every ``.button()`` call here used to land in that one row — six
    labelled buttons across a phone's width, so Telegram truncated each to an
    ellipsis and the screen became unreadable. ``.row()`` per button is used
    rather than ``.adjust(1, 1, …)`` because the count is CONDITIONAL: an
    ``adjust`` arity that stops matching the buttons re-flows the layout without
    an error, whereas an explicit ``.row()`` per button cannot drift.
    """
    builder = InlineKeyboardBuilder()

    builder.row(
        _button(
            "غیرفعال کردن نجوا" if config.enabled else "فعال کردن نجوا",
            "admin:whisper:toggle",
            "locked" if config.enabled else "unlocked",
            style="danger" if config.enabled else "success",
        )
    )
    builder.row(
        _button(
            "تنظیم هزینهٔ نجوا",
            "admin:costs",
            "coins",
        )
    )
    builder.row(
        _button("حداکثر طول متن", "admin:whisper:maxlen", "edit")
    )
    builder.row(_button("آمار نجوا", "admin:whisper:stats", "stats"))
    builder.row(
        _button("بازگشت به منوی مدیریت", BACK_TO_PANEL, "back")
    )
    return builder.as_markup()


# ──────────────────────────────────────────────────────────────────────────
# Force-join (عضویت اجباری) — its own section
# ──────────────────────────────────────────────────────────────────────────
def admin_forcejoin_kb(config, channels) -> InlineKeyboardMarkup:
    """Keyboard for the force-join management screen.

    Args:
        config: a ``WhisperConfig`` row (owns the ``require_join`` master
            switch — the flag predates this section and still lives there).
        channels: ordered ``RequiredChannel`` rows, used only for the count.

    Layout — ONE BUTTON PER ROW:
        [🔓/🔒 فعال/غیرفعال کردن عضویت اجباری]
        [📋 مدیریت کانال‌ها (N)]
        [➕ افزودن کانال]
        [🔙 بازگشت به منوی مدیریت]

    The direct «افزودن کانال» button is deliberate: adding the first channel
    is the whole reason an admin opens this section, and making them pass
    through the (empty) list screen first is one tap of pure friction.
    """
    builder = InlineKeyboardBuilder()

    builder.row(
        _button(
            "غیرفعال کردن عضویت اجباری"
            if config.require_join
            else "فعال کردن عضویت اجباری",
            "admin:forcejoin:toggle",
            "locked" if config.require_join else "unlocked",
            style="danger" if config.require_join else "success",
        )
    )
    count = len(list(channels))
    label = f"مدیریت کانال‌ها ({count})" if count else "مدیریت کانال‌ها (خالی)"
    builder.row(_button(label, "admin:forcejoin:channels", "list"))
    builder.row(
        _button("افزودن کانال", "admin:forcejoin:channels:new", "add", style="success")
    )
    builder.row(_button("بازگشت به منوی مدیریت", BACK_TO_PANEL, "back"))
    return builder.as_markup()


def admin_forcejoin_channels_kb(channels, not_admin_ids=None) -> InlineKeyboardMarkup:
    """List the required channels with a delete button each.

    Args:
        channels: iterable of ``RequiredChannel`` rows, already ordered.
        not_admin_ids: chat ids where the bot is NOT an administrator (or not
            even a member). Those rows get a ⚠️ marker so the admin sees which
            entries are currently unable to verify anybody.

    Layout (one row per button — the default builder packing truncates labels):
        [🗑️ <title>]        (red, one per channel; ⚠️ when unchecked)
        [➕ افزودن کانال]     (green)
        [🔙 بازگشت به عضویت اجباری]   (primary)

    The label is NOT ``escape``d: Telegram renders inline-button text as plain
    text, never as HTML, so escaping would show a literal ``&amp;`` for a title
    like "News & Updates".
    """
    bad = set(not_admin_ids or ())
    builder = InlineKeyboardBuilder()
    for row in channels:
        label = row.title or row.username or str(row.chat_id)
        if len(label) > 30:
            label = label[:30] + "…"
        flag = " ⚠️" if row.chat_id in bad else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{get_plain_emoji('trash')} {label}{flag}",
                callback_data=f"admin:forcejoin:channels:del:{row.id}",
                style="danger",
                icon_custom_emoji_id=get_premium_id("trash"),
            )
        )
    builder.row(
        _button("افزودن کانال", "admin:forcejoin:channels:new", "add", style="success")
    )
    builder.row(_button("بازگشت به عضویت اجباری", "admin:forcejoin", "back"))
    return builder.as_markup()


__all__ = [
    "BACK_TO_PANEL",
    "admin_panel_kb",
    "admin_stats_kb",
    "broadcast_confirm_kb",
    "admin_users_kb",
    "admin_user_flags_kb",
    "admin_manage_admins_kb",
    "admin_cancel_kb",
    "admin_costs_kb",
    "admin_costs_edit_kb",
    "admin_rewards_kb",
    "admin_limits_kb",
    "admin_report_kb",
    "COST_FIELDS",
    "REWARD_FIELDS",
    "LIMIT_FIELDS",
    "SECTION_FIELDS",
    "POLICY_EDIT_FIELDS",
    "policy_field_label",
    "policy_field_emoji",
    "policy_field_from_suffix",
    "policy_section_of",
    "admin_gift_kb",
    "admin_gift_scope_kb",
    "admin_gift_confirm_kb",
    "admin_whisper_kb",
    "admin_forcejoin_kb",
    "admin_forcejoin_channels_kb",
]