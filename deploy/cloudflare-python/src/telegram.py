"""Minimal Telegram Bot API client for Python Workers.

Outbound HTTP goes through the runtime's JS `fetch` via the FFI, which is the
supported way to make network requests from a Python Worker.
"""

import json


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, bot_token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def answer_callback(self, callback_query_id: str) -> dict:
        return await self._call(
            "answerCallbackQuery", {"callback_query_id": callback_query_id}
        )

    async def _call(self, method: str, payload: dict) -> dict:
        from js import Object, fetch
        from pyodide.ffi import to_js

        options = to_js(
            {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload, ensure_ascii=False),
            },
            dict_converter=Object.fromEntries,
        )
        response = await fetch(f"{self.base_url}/{method}", options)
        result = await response.json()
        if hasattr(result, "to_py"):
            result = result.to_py()
        result = dict(result)
        if not response.ok or not result.get("ok"):
            raise TelegramAPIError(
                f"Telegram {method} failed: {result.get('description')}"
            )
        return result
