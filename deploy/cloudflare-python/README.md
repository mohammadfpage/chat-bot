# Cloudflare Python Worker prototype (free tier)

A minimal, **from-scratch** port of the bot's webhook entry point that runs on
the Cloudflare Workers **Free** plan. It proves the free path end-to-end:
webhook secret validation, a manual update router, the Telegram Bot API over the
FFI, and D1 persistence.

> This directory is fully isolated. It does **not** import anything from the main
> project (`bot.py`, `handlers/`, `database/`, ...) and it does **not** touch the
> Render deployment. The existing `deploy/cloudflare/` (Containers) path is left
> untouched and remains the paid, run-code-as-is option.

## Why a rewrite (not a lift-and-shift)

The current code cannot run on Workers Free as-is:

| Blocker | Reason |
| --- | --- |
| `aiogram` | Importing it rebuilds 300+ pydantic models (~300 MB RSS > 128 MB limit). |
| async SQLAlchemy | Needs `greenlet`, which the runtime does not provide (Hyperdrive supports **sync** SQLAlchemy only). |
| Long-lived loop / aiohttp server | Workers are event-driven: `fetch` / `scheduled` / Durable Object alarms. |
| In-process RAM state | `handlers/chat.py` keeps matching state in module globals; one process assumption must move to a Durable Object. |

This prototype sidesteps all four for the covered surface. Remaining features
(matching, economy, inline mode, background jobs) are the next milestones.

## Layout

```
deploy/cloudflare-python/
├─ pyproject.toml          # pywrangler/uv project (no runtime deps)
├─ wrangler.jsonc          # python_workers flag + D1 binding
├─ migrations/0001_init.sql
├─ scripts/set_webhook.py  # standalone webhook register/info/delete
├─ src/
│  ├─ entry.py             # WorkerEntrypoint.fetch (+ GET /health)
│  ├─ config.py            # Settings.from_env
│  ├─ telegram.py          # Bot API client over the JS fetch FFI
│  ├─ db.py                # D1 UserRepository
│  └─ router.py            # manual update router (private chats only)
└─ tests/test_router.py    # pure-Python tests (no runtime needed)
```

Prototype surface: `/start`, `/help`, `callback_query` ack, D1 user upsert,
`GET /health`. Non-private chats are ignored (group-silence rule).

## Prerequisites

- **uv** — required by `pywrangler` to build/run Python Workers.
  Windows: `winget install --id=astral-sh.uv` (or `pip install uv`).
- A **Cloudflare account** (Free plan is enough).
- Node/npx (already available) for the `wrangler` CLI.
- A **separate test bot token** — do not point this Worker and Render at the
  same token: one token = one webhook, so the two deployments would fight over
  updates and split matching state / double-charge coins.

## Deploy (free)

```bash
cd deploy/cloudflare-python

# 1. Create the D1 database and copy the printed database_id into wrangler.jsonc
npx wrangler d1 create anon-chat-python-worker

# 2. Apply the schema
npx wrangler d1 migrations apply anon-chat-python-worker --remote

# 3. Store secrets (never commit these)
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET

# 4. Deploy
npx wrangler deploy

# 5. Point Telegram at the Worker and verify /health
$env:TELEGRAM_BOT_TOKEN="123:TEST"; python scripts/set_webhook.py set https://anon-chat-python-worker.<subdomain>.workers.dev/telegram/webhook
```

## Local development

```bash
uv run pywrangler dev        # http://localhost:8787
uv run pytest                # pure-Python unit tests
```

Local `.dev.vars` (git-ignored) supplies `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_WEBHOOK_SECRET`; see `.dev.vars.example`.

## Verify without a runtime

The router/config/D1 code is dependency-free, so it is covered by unit tests
that run on any Python 3.10+:

```bash
python -m pytest tests -q
```

## Free-plan limits to watch

100k requests/day, 10 ms CPU/request, 128 MB RAM, 50 subrequests/request,
6 concurrent outbound connections, 5 Cron Triggers. D1: 5M rows read/day and
100k rows written/day.

## Next milestones

1. Durable Object for matching state (`pair_map`, queues, rate-limiter) and
   per-user timers via alarms.
2. Port remaining commands/callbacks and the economy (decimal-safe) into the
   manual router.
3. Cron Triggers for retention / roster sync / chat expiry.
4. Only then run the Render-vs-Cloudflare latency comparison (separate token +
   separate DB, sequential webhook switch, P50/P95/P99, cold vs warm).
