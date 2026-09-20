"""
Reply keyboards for all bot states.

Uses the native keyboard button color style feature (Bot API 8.2+):
  - style="primary"   → Blue (main actions)
  - style="success"   → Green (positive actions)
  - style="danger"    → Red (cancel / report)

The ``style`` field accepts plain strings in current aiogram versions.
"""

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ──────────────────────────────────────────────────
# Main Menu  (Idle state)
# ──────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    """
    Row 1: [🔗 اتصال به ناشناس (Primary)] [👤 پروفایل من (Primary)]
    Row 2: [📬 پیام‌های ناشناس من (Primary)] [📬 لینک ناشناس من (Primary)]
    Row 3: [🏆 امتیازات و سکه (Primary)] [📋 راهنما و قوانین (Primary)]
    Row 4: [⛔️ لیست مسدودی‌ها (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [
                KeyboardButton(text="🔗 اتصال به ناشناس", style="primary"),
                KeyboardButton(text="👤 پروفایل من", style="primary"),
            ],
            [
                KeyboardButton(text="📬 پیام‌های ناشناس من", style="primary"),
                KeyboardButton(text="📬 لینک ناشناس من", style="primary"),
            ],
            [
                KeyboardButton(text="🏆 امتیازات و سکه", style="primary"),
                KeyboardButton(text="📋 راهنما و قوانین", style="primary"),
            ],
            [
                KeyboardButton(text="⛔️ لیست مسدودی‌ها", style="danger"),
            ],
        ],
    )


# ──────────────────────────────────────────────────
# In-Queue Menu  (Searching state)
# ──────────────────────────────────────────────────

def queue_menu_kb() -> ReplyKeyboardMarkup:
    """Cancel-search keyboard with a danger-styled button."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [KeyboardButton(text="❌ لغو جستجو", style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# In-Chat Menu  (Connected state)
# ──────────────────────────────────────────────────

def chat_menu_kb() -> ReplyKeyboardMarkup:
    """
    Row 1: [🔴 لغو چت (Danger)]
    Row 2: [🛑 گزارش کاربر / بلاک (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [KeyboardButton(text="🔴 لغو چت", style="danger")],
            [KeyboardButton(text="🛑 گزارش کاربر / بلاک", style="danger")],
        ],
    )


# ──────────────────────────────────────────────────
# Profile Setup keyboards (FSM step-by-step)
# ──────────────────────────────────────────────────

def age_kb() -> ReplyKeyboardMarkup:
    """Age selection grid (16–50, green) + cancel (red)."""
    ages = [KeyboardButton(text=str(a), style="success") for a in range(16, 51)]
    rows = [ages[i : i + 5] for i in range(0, len(ages), 5)]
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=rows,
    )


def city_kb() -> ReplyKeyboardMarkup:
    """Common cities quick-select (blue) + custom + cancel (red)."""
    cities = [
        "تهران", "اصفهان", "شیراز", "تبریز", "مشهد",
        "کرج", "اهواز", "قم", "کرمان", "ارومیه",
    ]
    rows = [
        [KeyboardButton(text=c, style="primary") for c in cities[i : i + 2]]
        for i in range(0, len(cities), 2)
    ]
    rows.append([KeyboardButton(text="✏️ شهر دیگر", style="primary")])
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=rows,
    )


def height_kb() -> ReplyKeyboardMarkup:
    """Height selection grid (green) + cancel (red)."""
    heights = [
        KeyboardButton(text=f"{h} سانتی‌متر", style="success")
        for h in range(150, 210, 5)
    ]
    rows = [heights[i : i + 2] for i in range(0, len(heights), 2)]
    rows.append([KeyboardButton(text="❌ انصراف", style="danger")])
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=rows,
    )


def confirm_profile_kb() -> ReplyKeyboardMarkup:
    """Confirm (green) or edit profile (blue)."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [
                KeyboardButton(text="✅ تایید", style="success"),
                KeyboardButton(text="✏️ ویرایش", style="primary"),
            ],
        ],
    )


# ──────────────────────────────────────────────────
# Admin Dual-Panel Menu  (Admin /start)
# ──────────────────────────────────────────────────

def admin_dual_panel_kb() -> ReplyKeyboardMarkup:
    """Dual-panel keyboard shown to admins on /start."""
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [KeyboardButton(text="👑 پنل مدیریت", style="primary")],
            [KeyboardButton(text="👤 پنل کاربری", style="primary")],
        ],
    )


# ──────────────────────────────────────────────────
# Anonymous Inbox Chat Menu  (Inbox live chat)
# ──────────────────────────────────────────────────

def anonymous_chat_menu_kb() -> ReplyKeyboardMarkup:
    """
    Keyboard shown during a live anonymous inbox chat session.
    Row 1: [❌ پایان چت / بازگشت (Danger)]
    """
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=True,
        keyboard=[
            [KeyboardButton(text="❌ پایان چت / بازگشت", style="danger")],
        ],
    )
