"""Free-tier Cloudflare Python Worker entry point (prototype).

This is a from-scratch re-implementation of the bot's webhook entry, built to
run on the Workers Free plan. It deliberately:

  * does NOT import aiogram (too heavy for the 128 MB Worker limit),
  * does NOT import SQLAlchemy (no greenlet in the runtime),
  * does NOT touch the Render deployment or any shared project file.

Scope of this prototype: validate the Telegram webhook secret, answer /start
and /help in private chats, record users in D1, and expose GET /health.
"""

import hmac
import traceback
from urllib.parse import urlparse

from workers import Response, WorkerEntrypoint

from config import Settings
from db import UserRepository
from router import Router
from telegram import TelegramAPI

WEBHOOK_PATH = "/telegram/webhook"


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlparse(str(request.url)).path
        method = request.method

        if method == "GET" and path == "/health":
            return Response.json(
                {"status": "ok", "mode": "webhook", "runtime": "python-worker"}
            )

        if method != "POST" or path != WEBHOOK_PATH:
            return Response("Not found", status=404)

        settings = Settings.from_env(self.env)

        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(str(supplied), settings.webhook_secret):
            return Response("Unauthorized", status=401)

        update = await request.json()
        if hasattr(update, "to_py"):
            update = update.to_py()
        if not isinstance(update, dict):
            return Response("Bad request", status=400)

        telegram = TelegramAPI(settings.bot_token)
        router = Router(UserRepository(self.env.DB), telegram)

        try:
            await router.handle(update)
        except Exception:  # noqa: BLE001 - log and fail loud for observability
            traceback.print_exc()
            raise

        return Response("OK")
