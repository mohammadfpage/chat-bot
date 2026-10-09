"""Webhook-mode smoke: config normalisation, URL/secret rules, setWebhook
arguments, and the aiohttp app's route + secret-token gate.

Everything runs OFFLINE: the app is mounted with aiohttp's TestClient and
posted at directly. build_webhook_app() is used WITHOUT setup_application(),
so the startup hooks (group keyboard sweep, roster sync, retention) never
fire — they would message real groups and sweep the real database.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

os.environ["RUN_MODE"] = "webhook"
os.environ["WEBHOOK_BASE_URL"] = "https://bot.example.com/"
os.environ["WEBHOOK_PATH"] = "tg-hook"  # no leading slash → normalised
os.environ["WEBHOOK_SECRET"] = "unit-test_secret-123"
os.environ["LOG_FILE"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

checks = []
def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)

import config
from config import Settings, settings

# ── config normalisation ──
check("RUN_MODE=webhook honoured", settings.run_mode == "webhook" and settings.webhook_mode)
check("webhook_path got leading slash", settings.webhook_path == "/tg-hook")
check("base url trailing slash stripped", settings.webhook_base_url == "https://bot.example.com")
check("secret kept from env", settings.webhook_secret == "unit-test_secret-123")
check("unknown RUN_MODE falls back to polling", Settings(run_mode="banana").run_mode == "polling")
check("RUN_MODE 'hook' alias accepted", Settings(run_mode="hook").run_mode == "webhook")
check("default run_mode is polling", Settings(run_mode="polling").webhook_mode is False)

import bot as bot_mod

# ── build_webhook_url ──
check("url join", bot_mod.build_webhook_url("https://x.example", "/p") == "https://x.example/p")
check("trailing-slash base", bot_mod.build_webhook_url("https://x.example/", "p") == "https://x.example/p")
check("path without slash", bot_mod.build_webhook_url("https://x.example", "p") == "https://x.example/p")
for bad, why in [
    ("", "empty"),
    ("   ", "blank"),
    ("http://x.example", "http scheme"),
    ("ftp://x.example", "ftp scheme"),
    ("x.example", "no scheme"),
]:
    try:
        bot_mod.build_webhook_url(bad, "/p")
        ok = False
    except ValueError:
        ok = True
    check(f"rejects base url ({why})", ok)

# ── secret rules ──
check("configured secret accepted", bot_mod.webhook_secret_token() == "unit-test_secret-123")
settings.webhook_secret = "has spaces!"
try:
    bot_mod.webhook_secret_token()
    ok = False
except ValueError:
    ok = True
check("invalid secret charset rejected", ok)
settings.webhook_secret = ""
generated = bot_mod.webhook_secret_token()
check(
    "empty secret generates a valid token",
    bool(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", generated)) and len(generated) >= 16,
)
settings.webhook_secret = "unit-test_secret-123"

# ── async: dispatcher kwargs + app behaviour ──
async def main():
    bot, dp = await bot_mod.build_bot_and_dispatcher()

    kwargs = bot_mod.build_set_webhook_kwargs(
        dp, url="https://bot.example.com/tg-hook", secret="s"
    )
    check("drop_pending_updates=False", kwargs["drop_pending_updates"] is False)
    check("url passed through", kwargs["url"] == "https://bot.example.com/tg-hook")
    check("secret passed through", kwargs["secret_token"] == "s")
    au = kwargs["allowed_updates"]
    check("allowed_updates is a list", isinstance(au, list) and bool(au))
    check("allowed_updates includes message", "message" in au)
    check("allowed_updates includes callback_query", "callback_query" in au)
    check("allowed_updates includes chat_member (roster)", "chat_member" in au)
    check("allowed_updates includes my_chat_member (onboarding)", "my_chat_member" in au)
    check("allowed_updates includes inline_query", "inline_query" in au)
    check("allowed_updates includes chosen_inline_result", "chosen_inline_result" in au)

    app = bot_mod.build_webhook_app(bot, dp, "s3cret")
    route_methods = set()
    route_paths = set()
    for route in app.router.routes():
        route_paths.add(route.resource.canonical)
        route_methods.add(getattr(route, "method", None))
    check(
        "webhook POST route registered",
        settings.webhook_path in route_paths and "POST" in route_methods,
    )
    check("/health GET route registered", "/health" in route_paths)

    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/health")
        check("/health returns 200", r.status == 200)
        body = await r.json()
        check("/health body status=ok", body.get("status") == "ok")

        r = await client.post(settings.webhook_path, json={"update_id": 1})
        check("no secret token → 401", r.status == 401)

        r = await client.post(
            settings.webhook_path,
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        check("wrong secret token → 401", r.status == 401)

        r = await client.post(
            settings.webhook_path,
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        check("valid update + secret → 200", r.status == 200)
        # handle_in_background answers before the dispatcher runs; give the
        # spawned task a beat to finish so it cannot outlive the loop.
        await asyncio.sleep(0.1)
    finally:
        await client.close()
        # client.close() → app.shutdown → handler.close already closed the
        # session; closing twice is harmless but keeps the intent explicit.
        await bot.session.close()

asyncio.run(main())

print(f"\n{sum(1 for _, ok in checks if ok)}/{len(checks)} checks passed")
sys.exit(0 if all(ok for _, ok in checks) else 1)
