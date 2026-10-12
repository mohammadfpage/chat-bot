"""Standalone helper to register/remove the Telegram webhook for this Worker.

Does not import anything from the main project. Standard library only, so it
runs on any Python 3.10+ interpreter.

Examples:
    python scripts/set_webhook.py set  https://<worker>.workers.dev/telegram/webhook
    python scripts/set_webhook.py info
    python scripts/set_webhook.py delete
"""

import argparse
import json
import os
import secrets
import sys
import urllib.request


def call(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["set", "info", "delete"])
    parser.add_argument("url", nargs="?", help="webhook URL (for 'set')")
    parser.add_argument(
        "--secret",
        default=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        help="webhook secret token (defaults to $TELEGRAM_WEBHOOK_SECRET)",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1

    if args.action == "set":
        if not args.url:
            print("'set' requires a webhook URL", file=sys.stderr)
            return 1
        secret = args.secret or secrets.token_urlsafe(32)
        result = call(
            token,
            "setWebhook",
            {
                "url": args.url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        print(json.dumps(result, indent=2))
        if "--secret" not in sys.argv and not os.environ.get(
            "TELEGRAM_WEBHOOK_SECRET"
        ):
            print(f"\nGenerated secret (store as Worker secret): {secret}")
    elif args.action == "info":
        print(json.dumps(call(token, "getWebhookInfo"), indent=2))
    else:
        print(json.dumps(call(token, "deleteWebhook"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
