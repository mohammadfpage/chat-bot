"""Support-ticket status values and the user-facing message templates.

The support section is a small, answerable ticketing system, and every line a
user reads from it is written HERE — one source, so the greeting a user sees,
the reply the team sends and the status the panel shows can never drift apart.

Layout of a team reply (the shape the product asked for):
    an opening that greets the user, the admin's own text in the middle, and a
    closing that thanks them and leaves the door open. Only the middle part is
    free text; the greeting and the thanks are fixed so every reply feels like
    it came from the same respectful team.
"""

from __future__ import annotations

from html import escape

# ── Status values ─────────────────────────────────────────
#: The user wrote last — the ticket is waiting for a team answer.
STATUS_OPEN = "open"
#: The team replied; the user may still follow up (which flips it back to open).
STATUS_ANSWERED = "answered"
#: Resolved and filed away. A further user message starts a NEW ticket.
STATUS_CLOSED = "closed"

#: ``status`` column value -> the Persian badge shown on every panel card.
STATUS_LABELS: dict[str, str] = {
    STATUS_OPEN: "🆕 در انتظار پاسخ",
    STATUS_ANSWERED: "✅ پاسخ داده شده",
    STATUS_CLOSED: "🗂 بسته شده",
}


def status_label(status: str) -> str:
    """Persian badge for a ticket status (falls back to the raw value)."""
    return STATUS_LABELS.get(status, status)


def status_from_filter(value: str) -> str | None:
    """Map a panel filter token (``open``/``answered``/``all``) to a column value.

    ``all`` (and anything unknown) returns ``None``, meaning "no status WHERE".
    """
    if value in (STATUS_OPEN, STATUS_ANSWERED, STATUS_CLOSED):
        return value
    return None


# ── Templates ─────────────────────────────────────────────
def support_home_text(open_ticket_id: int | None = None) -> str:
    """The «🎧 پشتیبانی» welcome card.

    ``open_ticket_id`` is the user's still-open ticket, when they have one, so
    the card can tell them their last message is already being looked at
    instead of inviting a duplicate.
    """
    lines = [
        "🎧 <b>پشتیبانی</b>",
        "",
        "سلام 👋 خوش آمدید!",
        "هر سؤال، مشکل یا انتقادی دارید، همین‌جا برای تیم پشتیبانی بنویسید.",
        "ما در سریع‌ترین زمان ممکن پاسخ می‌دهیم و می‌توانید گفتگو را ادامه دهید.",
        "",
        "📌 <b>چند نکته:</b>",
        "• پیام را واضح و کامل بنویسید.",
        "• اگر مشکل فنی است، نام بخش و زمان آن را ذکر کنید.",
        "• پاسخ تیم در همین چت برای شما ارسال می‌شود.",
    ]
    if open_ticket_id is not None:
        lines += [
            "",
            f"⏳ پیام قبلی شما (پیگیری <code>#{open_ticket_id}</code>) "
            "در حال بررسی است؛ اگر نکتهٔ تازه‌ای دارید همین‌جا بنویسید.",
        ]
    return "\n".join(lines)


def support_prompt_text() -> str:
    """Shown right after «✍️ نوشتن پیام» — what to type."""
    return (
        "✍️ <b>پیام خود را بنویسید</b>\n\n"
        "می‌توانید با یک «سلام» شروع کنید و سپس مشکل یا سؤال‌تان را توضیح دهید.\n"
        "برای انصراف روی «🏠 منوی اصلی» بزنید."
    )


def support_submitted_text(ticket_id: int, *, followup: bool = False) -> str:
    """Confirmation after the user's message is stored."""
    lead = (
        "💬 <b>پیام شما به تیم پشتیبانی اضافه شد</b>"
        if followup
        else "✅ <b>پیام شما ثبت شد</b>"
    )
    return (
        f"{lead}\n\n"
        f"🎫 شماره پیگیری: <code>#{ticket_id}</code>\n"
        "پیام شما در صف بررسی قرار گرفت. به‌محض پاسخ، همین‌جا به شما اطلاع "
        "می‌دهیم.\n\n"
        "🙏 از صبوری شما سپاسگزاریم."
    )


def support_reply_text(admin_text: str) -> str:
    """The DM a user receives when the team answers.

    Fixed greeting + the admin's escaped text + a fixed, respectful closing.
    """
    return (
        "👋 سلام،\n"
        "پاسخ تیم پشتیبانی به پیام شما:\n\n"
        f"{escape(admin_text)}\n\n"
        "🙏 از اینکه با ما در ارتباط هستید سپاسگزاریم.\n"
        "اگر هنوز سؤالی دارید، همین‌جا بنویسید تا در خدمت شما باشیم."
    )


def support_user_followup_notice(admin_text: str) -> str:
    """Same reply, worded for a message the user sent from inside the ticket.

    Kept separate so the two delivery paths can diverge later without touching
    the template the admin panel uses.
    """
    return support_reply_text(admin_text)


__all__ = [
    "STATUS_OPEN",
    "STATUS_ANSWERED",
    "STATUS_CLOSED",
    "STATUS_LABELS",
    "status_label",
    "status_from_filter",
    "support_home_text",
    "support_prompt_text",
    "support_submitted_text",
    "support_reply_text",
    "support_user_followup_notice",
]
