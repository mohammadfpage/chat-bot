"""
The one command guide, and the source both menus are built from.

Why a single source
-------------------
Four different entry points used to answer "what can this bot do here?" with
their own hand-written text: ``/start`` in a group, ``/help`` in a group, the
bare ``/`` hint, and the command list Telegram renders from
``set_my_commands``. They drifted apart — different shortcuts, different
wording, one of them advertising ``/nj`` while another did not — so a member
who read one card could not trust the next.

This module is the single place the guide is written. ``bot.py`` takes the
*descriptions* from :data:`GROUP_COMMANDS` / :data:`PRIVATE_COMMANDS` when it
registers the Telegram menu, and the private handlers take the *text* from
:func:`private_commands_text`. Same source, so the menu and the answer can
never disagree.

A group is silent now: ``/start``, ``/help``, ``/menu``, a bare ``/`` and an
unknown command all get nothing there (see ``handlers.start`` and
``handlers.whisper``), and the only card a group ever sees is the one posted
when the bot is added. So :func:`group_commands_text` has no caller — it is
kept because it is the prose form of :data:`GROUP_COMMANDS`, and the specs are
the machine-readable statement of what a group supports.

Why it is syntax-only
---------------------
This is an anonymous-chat bot. In a group the only things that exist are
"send a private message" and "read one" — there is no settings page, no
dashboard, no admin panel to browse. Prose descriptions ("راهنما و قوانین",
"ارسال پیام ناشناس (نجوا)") burned the two lines a client shows per command
and, worse, described a feature instead of showing how to type it. So every
line here is a command plus the exact thing to type after it.
"""

from __future__ import annotations

from dataclasses import dataclass

from html import escape

from utils.economy import fmt_coins, round_coins


@dataclass(frozen=True)
class CommandSpec:
    """One entry of a command menu.

    Attributes:
        command:    ASCII name Telegram accepts in its own menu.
        usage:      what to type, shown as the menu description AND echoed in
                    the guide. Must be short — Telegram truncates the menu
                    description, and a group member reads it on a phone.
        whisper:    whether the command only exists while نجوا is enabled.
    """

    command: str
    usage: str
    whisper: bool = False


#: Commands a group *supports*. Deliberately NOT registered with Telegram as a
#: group menu — see :func:`group_command_rows` — and not printed as a guide any
#: more either, because a group is silent. Kept as the spec of what the group
#: side of the bot can do, so the feature list still has one home.
GROUP_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "شروع"),
    CommandSpec("help", "راهنما"),
    CommandSpec("w", "نجوا", whisper=True),
    CommandSpec("inline", "نجوا اینلاین", whisper=True),
    CommandSpec("fix_keyboard", "رفع کیبورد"),
)

#: The private chat adds ``/menu`` (the same escape hatch as «🏠 منوی اصلی»)
#: and explains ``/w`` instead of running it — there is no group to whisper in.
PRIVATE_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "منوی ربات"),
    CommandSpec("menu", "منوی اصلی"),
    CommandSpec("help", "راهنما"),
    CommandSpec("w", "نجوا در گروه", whisper=True),
    CommandSpec("inline", "نجوا اینلاین", whisper=True),
)

#: Extra entry only admins get in their own private chat.
ADMIN_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("admin", "پنل مدیریت"),
)


def visible(commands: tuple[CommandSpec, ...], *, whisper: bool) -> list[CommandSpec]:
    """Drop the whisper entries when the feature is switched off."""
    return [c for c in commands if whisper or not c.whisper]


def _specs_to_rows(commands: list[CommandSpec]) -> list[tuple[str, str]]:
    """``(command, usage)`` pairs ready for ``set_my_commands``."""
    return [(c.command, c.usage) for c in commands]


def group_command_rows(*, whisper: bool) -> list[tuple[str, str]]:
    """Menu rows for ``BotCommandScopeAllGroupChats``.

    **Not called any more.** ``bot.py`` deletes that scope instead of filling it,
    because the ``/`` popup inside a group advertised a row of commands for a
    conversation that was not about the bot. And the guide that answering ``/``
    used to print is gone with it: a group now gets nothing at all.

    Kept because it is the only machine-readable statement of what a group
    supports, and the prose is that same statement in another form.
    """
    return _specs_to_rows(visible(GROUP_COMMANDS, whisper=whisper))


def private_command_rows(*, whisper: bool) -> list[tuple[str, str]]:
    """Menu rows for the default (private) scope."""
    return _specs_to_rows(visible(PRIVATE_COMMANDS, whisper=whisper))


def admin_command_rows(*, whisper: bool) -> list[tuple[str, str]]:
    """Menu rows for an admin's own chat: everything private, plus /admin."""
    return _specs_to_rows(visible(PRIVATE_COMMANDS, whisper=whisper)) + [
        (c.command, c.usage) for c in ADMIN_COMMANDS
    ]


def group_commands_text(bot_username: str = "", *, whisper: bool = True) -> str:
    """The group command guide, in prose.

    **Not called any more.** ``/``, ``/start``, ``/help``, ``/menu`` and an
    unknown command are all answered with silence in a group; the one card a
    group sees is the onboarding card posted when the bot is added. Kept as the
    prose form of :data:`GROUP_COMMANDS`, should a group guide ever come back.

    Kept to the two things that exist in a group. Every line is a command and
    the literal text to type after it, so a member can copy it without reading
    a paragraph first.

    An emoji marks a *section*, never an individual row. Decorating every command
    separately made a five-line card look like a wall of icons and buried the
    only thing a member needs from it — the text to type.
    """
    bot = escape(bot_username or "bot")

    lines: list[str] = ["<b>دستورهای ربات</b>"]

    if whisper:
        lines += [
            "",
            "🔇 <b>نجوا</b> — پیام خصوصی در همین گروه",
            "روی پیام طرف ریپلای کنید و بنویسید:",
            "<code>/w متن پیام</code>",
            "بدون ریپلای (با یوزرنیم):",
            "<code>/w @یوزرنیم متن پیام</code>",
            "میان‌بر: <code>/nj</code> · <code>/نجوا</code>",
            "",
            "📨 <b>نجوا اینلاین</b> — از هر چتی، حتی خارج از این گروه",
            "<code>/inline</code>",
            f"<code>@{bot} 🆔 123456789 💬 متن نجوا</code>",
        ]

    lines += [
        "",
        "<code>/help</code> — همین راهنما",
        "<code>/start</code> — شروع",
        "<code>/fix_keyboard</code> — رفع کیبورد گیرکردهٔ پایین گروه",
    ]

    if whisper:
        lines += ["", "🔒 متن نجوا هرگز وارد گفتگو نمی‌شود."]

    return "\n".join(lines)


def private_commands_text(
    bot_username: str = "", *, whisper: bool = True, policy=None
) -> str:
    """The ``/help`` card for a private chat — commands, then the rules.

    ``policy`` is the ``bot_policy`` row, passed in rather than awaited so a
    caller already holding it does not pay for a second round-trip. It is what
    keeps the numbers on this card (chat length, message cap) equal to the ones
    the bot actually enforces; ``None`` falls back to the shipped defaults.
    """
    bot = escape(bot_username or "bot")

    lifetime = max(getattr(policy, "chat_lifetime_hours", None) or 24, 1)
    per_minute = getattr(policy, "messages_per_minute", None) or 20
    girl = max(round_coins(getattr(policy, "chat_girl_cost", 1)), 0.0)
    boy = max(round_coins(getattr(policy, "chat_boy_cost", 1)), 0.0)
    # Both read live: the admin can price اتصال شانسی and نجوا from the panel,
    # and a /help card that still says «رایگان» after that would be a lie.
    random_cost = max(round_coins(getattr(policy, "random_chat_cost", 0)), 0.0)
    whisper_cost = max(round_coins(getattr(policy, "whisper_cost", 1)), 0.0)

    def price(value: float) -> str:
        return "رایگان" if value <= 0 else f"{fmt_coins(value)} سکه"

    lines: list[str] = [
        "<b>دستورها</b>",
        "",
        "<code>/start</code> — منوی ربات",
        "<code>/menu</code> — منوی اصلی",
        "<code>/help</code> — همین راهنما",
    ]

    if whisper:
        lines += [
            "",
            "🔇 <b>نجوا</b> — پیام خصوصی داخل گروه",
            "<code>/w</code> — راهنمای نجوا",
            f"<code>@{bot} 🆔 123456789 💬 متن نجوا</code> — از هر چتی",
        ]

    # Written per-mode rather than as one number: the three costs are
    # independent policy fields, so collapsing them into a single figure would
    # quote the wrong price the moment an admin sets them differently.
    if girl == boy:
        targeted = f"«👩 چت با دختر» و «👨 چت با پسر» {price(girl)}"
    else:
        targeted = (
            f"«👩 چت با دختر» {price(girl)} · «👨 چت با پسر» {price(boy)}"
        )

    lines += [
        "",
        "<b>اتصال</b>",
        f"«🔀 اتصال شانسی» {price(random_cost)} · {targeted}",
        "هزینهٔ اتصال فقط یک بار و فقط وقتی وصل شوید کسر می‌شود؛",
        "پیام‌های داخل چت رایگان است.",
    ]
    if whisper:
        lines += [f"«🔇 نجوا» {price(whisper_cost)} به ازای هر پیام."]

    lines += [
        "",
        "<b>قوانین</b>",
        f"هر چت حداکثر <b>{lifetime} ساعت</b> باز می‌ماند و بعد خودکار بسته می‌شود.",
        f"حداکثر <b>{per_minute} پیام</b> در دقیقه — ارسال بیشتر هشدار می‌گیرد.",
        "اسپم و مزاحمت ممنوع؛ درخواست‌ها به <code>/admin</code> می‌رود.",
    ]

    return "\n".join(lines)


__all__ = [
    "CommandSpec",
    "GROUP_COMMANDS",
    "PRIVATE_COMMANDS",
    "ADMIN_COMMANDS",
    "visible",
    "group_command_rows",
    "private_command_rows",
    "admin_command_rows",
    "group_commands_text",
    "private_commands_text",
]
