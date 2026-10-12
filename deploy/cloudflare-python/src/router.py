"""Manual Telegram update router (no aiogram).

Handles the prototype surface only. Every user-facing string is sent with
parse_mode=HTML (set in the TelegramAPI client) and contains no unescaped
user-provided text.
"""

WELCOME = (
    "<b>سلام!</b>\n"
    "به ربات چت ناشناس خوش آمدید.\n\n"
    "این نسخه روی Cloudflare Workers اجرا می‌شود (نمونهٔ آزمایشی)."
)

HELP = (
    "<b>راهنما</b>\n"
    "/start - شروع\n"
    "/help - همین راهنما"
)


class Router:
    def __init__(self, users, telegram) -> None:
        self.users = users
        self.telegram = telegram

    async def handle(self, update: dict) -> None:
        message = update.get("message")
        if message:
            await self._handle_message(message)
            return

        callback = update.get("callback_query")
        if callback:
            await self.telegram.answer_callback(callback.get("id"))
            return

    async def _handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return

        user = message.get("from") or {}
        if user.get("id"):
            await self.users.upsert(user)

        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""
        chat_id = chat.get("id")

        if command == "/start":
            await self.telegram.send_message(chat_id, WELCOME)
        elif command == "/help":
            await self.telegram.send_message(chat_id, HELP)
