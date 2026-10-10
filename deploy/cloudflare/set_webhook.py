"""Register (or clear) this bot's Cloudflare Worker URL with Telegram.

Run this ONCE after the Worker is deployed (and again whenever the public
URL or the secret changes). The bot itself re-registers its webhook on every
boot, but the *container* sits behind the Worker and only sees a request
after Telegram has already been pointed at it — so the very first
registration has to come from outside.

    python deploy/cloudflare/set_webhook.py                 # register
    python deploy/cloudflare/set_webhook.py --print-curl    # show the raw API call
    python deploy/cloudflare/set_webhook.py --remove        # back to polling

It imports the repo's own helpers (``build_webhook_url``,
``build_set_webhook_kwargs``) so the ``allowed_updates`` list can never drift
from what the dispatcher actually handles — ``chat_member`` must be included
or the group roster + onboarding card silently stop.

A plain ``curl`` equivalent (for reference) is::

    curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
      --data-urlencode "url=https://<worker>.workers.dev/telegram/webhook" \
      --data-urlencode "secret_token=<WEBHOOK_SECRET>" \
      --data-urlencode "drop_pending_updates=false" \
      --data-urlencode 'allowed_updates=["message","callback_query", ...]'

``--print-curl`` prints that command filled in (with the token redacted),
which is safer than hand-writing the ``allowed_updates`` list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

# This file lives at <root>/deploy/cloudflare/, so the repo root is parents[2].
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot as bot_mod  # noqa: E402
from config import settings  # noqa: E402


def _resolve_url(raw: str) -> str:
    """Accept either an origin (path appended) or a complete https URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(
            "No URL given. Pass --url https://<worker>.workers.dev or set "
            "WEBHOOK_BASE_URL. «آدرس وبهوک داده نشده است.»"
        )
    parts = urlsplit(raw)
    if parts.path and parts.path != "/":
        # Already a full webhook URL — validate the scheme, keep it as-is.
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError(
                f"Webhook URL must be https:// (got {raw!r}). "
                "«آدرس وبهوک باید با https شروع شود.»"
            )
        return raw.rstrip("/")
    # Origin only → append the configured path (default /telegram/webhook).
    return bot_mod.build_webhook_url(raw, settings.webhook_path)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Register/clear the Cloudflare Worker webhook with Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--url",
        default=None,
        help="Public https origin or full webhook URL. Defaults to "
        "WEBHOOK_BASE_URL (+ WEBHOOK_PATH).",
    )
    p.add_argument(
        "--secret",
        default=None,
        help="secret_token to register. Defaults to WEBHOOK_SECRET. MUST equal "
        "the Worker's WEBHOOK_SECRET or every delivery gets a 401.",
    )
    p.add_argument(
        "--remove",
        action="store_true",
        help="deleteWebhook (drop_pending_updates=false) — use before switching "
        "back to long polling.",
    )
    p.add_argument(
        "--print-curl",
        action="store_true",
        help="Print the equivalent curl command instead of calling Telegram.",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    if not settings.bot_token or settings.bot_token == "YOUR_BOT_TOKEN_HERE":
        print(
            "BOT_TOKEN is not set (repo-root .env / environment). "
            "«توکن ربات تنظیم نشده است.»",
            file=sys.stderr,
        )
        return 1

    if args.remove:
        bot, _ = await bot_mod.build_bot_and_dispatcher()
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        finally:
            await bot.session.close()
        print("deleteWebhook OK — the bot can return to polling mode now.")
        print("حذف وبهوک انجام شد — ربات می‌تواند به حالت polling برگردد.")
        return 0

    # ── URL ──
    try:
        url = _resolve_url(args.url if args.url is not None else settings.webhook_base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # ── secret ──
    # A random per-boot secret (what bot.py falls back to) is deliberately
    # NOT used here: the deployed Worker/container expects the fixed value it
    # was configured with, so registering anything else makes Telegram's
    # header mismatch and every update returns 401.
    secret = (args.secret if args.secret is not None else settings.webhook_secret or "").strip()
    if not secret:
        print(
            "WEBHOOK_SECRET is empty. Set it in .env (or pass --secret) and "
            "store the SAME value on Cloudflare:\n"
            "  npx wrangler secret put WEBHOOK_SECRET -c deploy/cloudflare/wrangler.jsonc\n"
            "«برای وبهوک، WEBHOOK_SECRET الزامی است و باید با مقدار Worker "
            "یکسان باشد.»",
            file=sys.stderr,
        )
        return 1
    if not bot_mod._WEBHOOK_SECRET_RE.match(secret):
        print(
            "WEBHOOK_SECRET must be 1-256 chars of A-Z a-z 0-9 _ - only. "
            "«WEBHOOK_SECRET فقط باید حروف، عدد، _ و - داشته باشد.»",
            file=sys.stderr,
        )
        return 1

    # Dispatcher is built only to resolve allowed_updates the same way the
    # runtime does (this never touches the network).
    bot, dp = await bot_mod.build_bot_and_dispatcher()
    try:
        kwargs = bot_mod.build_set_webhook_kwargs(dp, url=url, secret=secret)

        if args.print_curl:
            allowed = json.dumps(kwargs["allowed_updates"], ensure_ascii=False)
            # Single line on purpose: PowerShell aliases `curl` → Invoke-WebRequest,
            # so the example uses `curl.exe`, and continuations (`^`/backtick)
            # differ per shell. Replace <BOT_TOKEN> with the real token.
            print(
                f'curl.exe -s -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" '
                f'--data-urlencode "url={url}" '
                f'--data-urlencode "secret_token={secret}" '
                f'--data-urlencode "drop_pending_updates=false" '
                f"--data-urlencode 'allowed_updates={allowed}'"
            )
            print(
                f"# allowed_updates ({len(kwargs['allowed_updates'])} types): "
                f"{', '.join(kwargs['allowed_updates'])}"
            )
            return 0

        try:
            await bot.set_webhook(**kwargs)
        except Exception as exc:  # TelegramNetworkError is a subclass
            if type(exc).__name__ == "TelegramNetworkError":
                bot_mod.log_connection_advice(
                    f"set_webhook failed: {type(exc).__name__}: {exc}", bot
                )
                return 1
            print(f"set_webhook rejected by Telegram: {exc}", file=sys.stderr)
            print(
                "Common causes / علل رایج:\n"
                "  * URL must be https:// and reachable from the internet.\n"
                "  * Register the WORKER url, not the container — e.g.\n"
                "    https://anon-chat-bot.<account>.workers.dev/telegram/webhook\n"
                "  * WEBHOOK_SECRET characters must be A-Z a-z 0-9 _ - only.",
                file=sys.stderr,
            )
            return 1

        print(f"setWebhook OK: {url}")
        print(f"  secret_token: {len(secret)} chars, drop_pending_updates=False")
        print(f"  allowed_updates: {', '.join(kwargs['allowed_updates'])}")
        print("ثبت وبهوک انجام شد. تست سلامت / Health check:")
        print("  npx wrangler ...   →   curl https://<worker>.workers.dev/health")
        return 0
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
