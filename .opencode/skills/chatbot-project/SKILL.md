---
name: chatbot-project
description: Full architecture map of the Persian anonymous-chat Telegram bot at "D:\Project\chat bot" (aiogram 3 + SQLAlchemy async) — file-by-file structure, startup order, hard rules (HTML parse mode, group silence, escaping), economy/decimal system, security layers, run & verify commands. Use when starting, navigating, debugging, or reviewing this project's code, so you do not have to re-read the codebase.
---

# راهنمای پروژه چت‌بات ناشناس / Project Knowledge Base

> خلاصۀ همه‌چیزی که دربارۀ این پروژه می‌دانیم، تا لازم نباشد کد را دوباره بخوانی.
> Everything we know about this repo, so a fresh agent does not burn tokens rediscovering it.
> اگر چیزی را تغییر دادی، همین فایل را هم به‌روز کن. / Update this file when you change the project.

---

## ۱. پروژه چیست؟ / What is it?

Persian (Farsi) anonymous-chat bot for Telegram:

- **چت ۱-به-۱ ناشناس (Matching)** — کاربران جفت می‌شوند و پیام رد و بدل می‌کنند، پیام‌ها ذخیره نمی‌شوند. 1-on-1 matching, relay only, nothing persisted.
- **نجوا (Whisper)** — پیام مخفی داخل گروه؛ متن وارد گروه نمی‌شود، فقط گیرنده در خصوصی می‌بیند. Secret message via inline mode.
- **پیام ناشناس (Inline / Anonymous)** — کارت `@bot` در هر چت برای درخواست/ارسال پیام ناشناس، گروهی و خصوصی. Inline picker → anonymous DMs + real-time session.
- **پنل ادمین** — آمار، بن/آنبن، تبلیغ، مدیریت ادمین، هدیه سکه، تنظیمات سیاست (قیمت/پاداش/لیمیت).
- **سکه (Coins)** — تنها ارز؛ شارژ، کیف پول، روزانه، زیرمجموعه‌گیری. Single currency, decimal (2 places).

Stack: **aiogram 3.31 (pinned) · SQLAlchemy 2 async · aiosqlite (SQLite `database.db`) / asyncpg (PostgreSQL, production) · pydantic-settings · Python 3.12 · two run modes: long polling (default) or aiohttp webhook server (`RUN_MODE`)** — see §2b and §2c. Current deployment = **Render Python Web Service** (`render.yaml`); Cloudflare Containers is optional (`deploy/cloudflare/`).

`requirements.txt`: `aiogram, sqlalchemy[asyncio], aiosqlite, asyncpg, redis, pydantic-settings, pydantic` — asyncpg + redis are USED by PRODUCTION (the current Render Python web service, plus the optional Cloudflare Containers path): `DATABASE_URL=postgresql+asyncpg://…`, `REDIS_URL` for FSM; locally neither is imported at startup.

---

## ۲. اجرا و بررسی / Run & verify

```powershell
# run (system python works; venv also exists)
$env:PYTHONIOENCODING='utf-8'; python bot.py
# or: D:\Project\chat bot\venv\Scripts\python.exe bot.py
```

- Config comes from `.env` (`config.py` → `settings`). Token in `.env`; `.env.example` documents every key.
- No linter/test framework installed (no ruff/flake8/pyflakes/pytest). Verification = compile + import + smoke scripts.
- **One command runs everything** (compileall → `import bot` → all 11 smokes, UTF-8, non-zero exit on any failure):

```powershell
$env:PYTHONIOENCODING='utf-8'; & "D:\Project\chat bot\venv\Scripts\python.exe" "D:\Project\chat bot\smoke\verify.py"
# or: test Telegram reachability first: & $py test_connection.py
```

- Smokes live in **`smoke/`** (in-repo, path-relative — they work from anywhere):
  - `smoke_handlers5.py` (35) — menu/panel/HTML-escape/FSM-coverage/dead-button audit across every handler + `is_bot_control_text` relay guard
  - `smoke_support.py` (41) — support tickets: templates/status helpers, user+admin keyboards, `/support` command wiring, router registration, and a sqlite round-trip (create → transcript → admin-read → answered/closed winner rules)
  - `smoke_profile_edit.py` (41) — «✏️ ویرایش پروفایل»: `ProfileEdit` states, card/edit keyboards, handler registration, the `~StateFilter(ProfileSetup, ProfileEdit)` gender-collision guard, a source scan for no-Telegram-call-under-a-session, and a sqlite round-trip through `_finish_field_edit` (one field changed, others untouched)
  - `smoke_forcejoin.py` — root panel = 13 buttons / non-root = 11, force-join fail-open, `_answer_join_prompt`
  - `smoke_phase2.py` (16) — BASE_DIR, sqlite URL, WAL, log redaction, `_hook` kwargs filter, backgrounded cleanup ("RuntimeError: hook exploded" is INTENTIONAL = 16/16)
  - `smoke_economy.py` (25) — atomic deduct, exact refunds, peek/record, free-whisper exploit
  - `smoke_gift.py` — gift scope/confirm/bulk + `cb_gift_confirm`-before-prefix registration order
  - `smoke_force_join4.py` (39) — membership cache, re-check fail-open, prompt dedupe ("accepting: api down" line is expected output)
  - `smoke_phase6.py` (76) — backup rotation, retention purge (5 tables incl. `user_reports`), wallet coin history, chat-ended keyboard + rematch lockout, report inbox, flags editor, whisper maxlen, privacy source scans
  - `smoke_webhook.py` (35) — RUN_MODE normalisation, `build_webhook_url` https rules, secret charset, `build_set_webhook_kwargs` (drop_pending=False, `chat_member` in allowed_updates), aiohttp app routes + secret-token 401/200 gate (offline TestClient)
  - `smoke_cloudflare.py` (90+) — postgres URL normalisation + `database_dialect`/`in_cloudflare_container`, `engine_options` per backend, `ChatPair` schema, **functional rebuild** (persist → wipe RAM → `rebuild_chat_state` → queue/pairs/FSM/last_mode restored, one-sided/banned demoted), `_persist_pair`/`_persist_ended`/`leave_search_queue`, `build_fsm_storage` backends, `_install_stop_signal`, startup-hook order, static checks of the optional `deploy/cloudflare/` files, the no-Wrangler root invariant, `render.yaml`, and `PORT` handling
- Smokes must keep the whole-repo rglob filters excluding `venv`/`.opencode`/`smoke` (fixture reason-strings would false-fail).
- PowerShell gotchas: no `rg` in this shell (use the grep tool or `Select-String`); `python -c` with quotes gets mangled → write a temp `.py` file instead; `&` chaining works, `&&` does not in PS 5.1; the console is cp1252 → set `PYTHONIOENCODING=utf-8` or reconfigure stdout; do NOT grep outputs case-insensitively for `fail`/`traceback` (phase2/force_join4 print those words on success) — rely on exit codes.

### پراکسی / Proxy — `PROXY_URL`

`api.telegram.org` is filtered in this region, so the bot ships with a proxy path:

- **`.env`** → `PROXY_URL=socks5://127.0.0.1:1080` (also `socks4`, `socks5h`, `http://host:port`, `user:pass@`). Empty = direct.
- **`config.py:proxy_url`** → read once at startup.
- **`bot.py:build_session()`** → `AiohttpSession(proxy=url)`; aiogram swaps in `aiohttp_socks.ProxyConnector`. Every construction failure (bad scheme, missing package) degrades to a *direct* session with a logged error — never an exception.
- **`bot.py:_fallback_to_direct(bot)`** → if the proxy never connects, the inner session is swapped for a direct one **inside `GuardedSession`** (keyboard guard preserved), then `delete_webhook` is retried.
- **`bot.py:log_connection_advice()`** → the bilingual (EN/FA) troubleshooting block; called when startup or polling loses the network. Exits with code **1**, no traceback.
- **`test_connection.py`** → standalone probe: direct route → `PROXY_URL` route → `getMe` (validates the token, never prints it). Exit 0 = at least one route works. Run it before `bot.py`.
- Requires `aiohttp-socks>=0.12.0` (in `requirements.txt`, installed in **both** the venv and the system Python — the user runs `python bot.py`, not the venv).

**Network**: a connectivity failure is not a code bug. Startup retries `delete_webhook` 5×3s (`bot.py:_clear_webhook`), falls back, then logs the advice block and exits 1. `logger` is **module-level** in `bot.py` — it used to be a local inside `main()`, which made this very path crash with `NameError`; do not move it back.

---

### وبهوک / Webhook — `RUN_MODE` (`bot.py` + `config.py`)

Two modes selected by `RUN_MODE` in `.env` (unknown value → polling + warning): `polling` (default, unchanged) and `webhook`. Full user guide = **`WEBHOOK.md`** (local no-SSL test, server deploy, Cloudflare research).

- `config.py`: `run_mode`, `webhook_base_url` (public https origin, trailing `/` stripped), `webhook_path` (leading `/` forced), `webhook_secret`, `webapp_host` (`127.0.0.1`), `webapp_port` (`8080`), `webhook_mode` property; all normalised by `_normalize_run_mode`.
- `bot.py` webhook section: `build_webhook_url` (rejects empty/non-https base with EN/FA ValueError), `webhook_secret_token` (`WEBHOOK_SECRET` validated against `^[A-Za-z0-9_-]{1,256}$`, empty → `secrets.token_urlsafe(24)` per boot), `build_set_webhook_kwargs` (`url`, `secret_token`, **`drop_pending_updates=False`**, `allowed_updates=dp.resolve_used_update_types()` — mirrors polling; without it Telegram's default excludes `chat_member` and the roster/onboarding dies), `build_webhook_app` (POST `webhook_path` via `SimpleRequestHandler(handle_in_background=True, secret_token=…)` + `GET /health`), `run_webhook`.
- `run_webhook` order: validate URL/secret → `build_webhook_app` → `setup_application(app, dp, bot=bot)` (**must come after `build_bot_and_dispatcher()` — workflow_data is captured by value**) → `AppRunner(handle_signals=False)` → `TCPSite.start()` → THEN `bot.set_webhook` (socket must listen first). Fails: network → `log_connection_advice`; Telegram 400 → port/https/secret hints; both → `SystemExit(1)` after `runner.cleanup()`. Runs until cancelled (`await asyncio.Future()`), **webhook is NOT deleted on shutdown** (Telegram keeps retrying; next start re-asserts or polling clears it).
- `main()` branches after the shared `_clear_webhook` reachability probe (the probe also clears stale registration in webhook mode); `finally` closes session + disposes engine in both modes.
- Local test without SSL: `cloudflared tunnel --url http://127.0.0.1:8080` → paste https URL into `WEBHOOK_BASE_URL`. Telegram accepts **only https://** and only ports **443/80/88/8443** when connecting directly (a tunnel hides the local port) — the local server is plain HTTP, TLS belongs to the tunnel/proxy.
- `smoke/smoke_webhook.py` (35 checks, offline): config normalisation, URL/secret rules, setWebhook kwargs, TestClient POSTs (no secret → 401, wrong → 401, valid → 200). **It deliberately does NOT call `setup_application`** — aiohttp's `runner.setup()`/`TestServer.start_server()` fires `app.on_startup`, and the startup hooks message real groups + sweep the real DB.

### استقرار — `§2c` (فعلی: Render؛ Cloudflare اختیاری)

**Current deployment = an ordinary Python Web Service (Render) driven by the
root `render.yaml`: `pip install -r requirements.txt` + `python bot.py`, webhook
mode, binds platform `PORT` on `0.0.0.0`, `/health` health check. No Docker, no
Wrangler, no Node. `config.port` (env `PORT`) overrides `WEBAPP_PORT` and, when
`WEBAPP_HOST` was not set explicitly, widens the bind to `0.0.0.0`. Render Free
sleeps after ~15 min and has no persistent disk → production MUST use managed
PostgreSQL (`DATABASE_URL`) + Redis (`REDIS_URL`).**

The old Cloudflare Containers setup is preserved **outside the repo root** under
`deploy/cloudflare/` (so no platform auto-detects a Node/Worker project and runs
`npx wrangler deploy`). Whole bot runs as a Docker image behind a tiny Worker
(no VPS) if that option is restored. **It is still runnable FROM the repo root —
point Wrangler at the config: `npx wrangler deploy -c deploy/cloudflare/wrangler.jsonc`
(every relative path inside the config resolves against the config file's
directory, not cwd). `containers[0].image_build_context: "../.."` makes the
Docker build context = the repo root, so the Dockerfile's `COPY requirements.txt`
/ `COPY . .` work in place (no more "copy files back to the root"); the
repo-root `.dockerignore` is what keeps secrets/venv/db/node_modules out.**
**Containers are Workers-Paid-only** (Cloudflare pricing, Aug 2026: `Free: N/A`,
$5/mo) — "Cloudflare free tier" is NOT possible for this architecture; free
hosting = Render path or VPS + cloudflared tunnel. Build files:

| File (all under `deploy/cloudflare/`) | Role |
| --- | --- |
| `Dockerfile` | `python:3.12-alpine`, pip install requirements, `CMD ["python","bot.py"]`, `EXPOSE 8080` (build context = repo root via `image_build_context`) |
| `.dockerignore` | legacy-context ignores; the **effective** one is the repo-root `.dockerignore` (keeps `.env`/`.dev.vars`/`venv/`/`*.db*`/`backups/`/`logs/`/`**/node_modules/` out) |
| `wrangler.jsonc` | `containers[]` (`class_name: BotContainer`, `image: ./Dockerfile`, **`image_build_context: "../.."`**, **`max_instances: 1` — pair_map is per-process, 2 instances = split worlds**), `durable_objects.bindings` `BOT_CONTAINER`, **`exports`** (`{"type":"durable-object","storage":"sqlite"}` — cannot coexist with legacy `migrations`), `vars` (RUN_MODE=webhook, WEBHOOK_PATH, WEBHOOK_BASE_URL="", WEBAPP_HOST=0.0.0.0/8080, ADMIN_IDS, CHANNEL_*, LOG_FILE="", GROUP_KEYBOARD_CLEANUP=false) |
| `worker/index.js` | `BotContainer extends Container` (`defaultPort=8080`, `sleepAfter="10m"`, constructor copies every **string** Worker env entry → `this.envVars` = how vars + `wrangler secret put` values reach the container — platform injects only `CLOUDFLARE_*`); default export **path-gates**: only `POST env.WEBHOOK_PATH` and `GET /health` are proxied via `env.BOT_CONTAINER.getByName("bot").fetch(request)`; everything else 404s **without touching the DO** (no accidental cold-boot) |
| `package.json` | `@cloudflare/containers ^0.3.7` (bundled by wrangler), `wrangler ^4.148.0`, `type: module`; scripts `dev`/`deploy`/`tail` + `cf:dev`/`cf:deploy`/`cf:tail`/`cf:secret`/`cf:check` (all pass `-c wrangler.jsonc`; run via `npm --prefix deploy/cloudflare run cf:dev`) |
| `.dev.vars.example` | template for local `wrangler dev` secrets (BOT_TOKEN, DATABASE_URL, WEBHOOK_BASE_URL, WEBHOOK_SECRET, RUN_MODE=webhook, ADMIN_IDS, optional REDIS_URL/PROXY_URL); real `.dev.vars` is git-ignored and is read from the **config directory** |
| `set_webhook.py` | standalone helper: imports `build_webhook_url`/`build_set_webhook_kwargs` (so `allowed_updates` can't drift — `chat_member` included), `--url`/`--secret`/`--print-curl`/`--remove`; sys.path-inserts repo root (parents[2]) |

`npx wrangler dev` needs a running **Docker daemon** and reads `.dev.vars` from
the config dir (verified in wrangler source: `path.resolve(configDir, ".dev.vars")`).
`npm install --prefix deploy/cloudflare` installs the tooling without a root
`package.json` (which the smoke forbids).

Secrets (never in git): `npx wrangler secret put` → **BOT_TOKEN, DATABASE_URL, WEBHOOK_SECRET, REDIS_URL, PROXY_URL**. Re-put + `npx wrangler deploy` to rotate (envVars are read at container start). Deploy needs **Node 20+ AND a running Docker daemon** (wrangler builds the image locally — even `--dry-run` demands the Docker CLI; `--containers-rollout=none` skips the container and only deploys the Worker). Verified offline **from the repo root**: `wrangler deploy --dry-run --containers-rollout=none -c deploy/cloudflare/wrangler.jsonc` (and `npm --prefix deploy/cloudflare run cf:check`) bundles the Worker (54 KiB) and lists the `BOT_CONTAINER` binding + all vars + container `anon-chat-bot-botcontainer` from the Dockerfile — proving config-relative resolution of `main`/`image`/`image_build_context`.

Design decisions (all covered by `smoke_cloudflare.py`):

- **`database/models.py::ChatPair`** (`chat_pairs`, ONE row per user, PK `user_id` BigInteger `autoincrement=False`) = durable mirror of the RAM state; `status queued|paired|ended`, `mode`/`gender` kept even after `ended` («چت بعدی» repeats the mode across restarts), `opened_at` = **Python epoch float** (SQLite and PostgreSQL disagree on `now()`).
- **`handlers/chat.py` persistence**: `_KEEP` sentinel (`_write_chat_row` — unchanged columns untouched), `_persist_queue`, `_persist_pair` (BOTH directions in ONE txn — half a pair = split-brain), `_persist_ended` (**conditional UPDATE** on `status IN ('queued','paired')` — never inserts a row for a stranger), `leave_search_queue` (RAM pop + mirror write; called from cancel_search/nav_start/_to_main_menu).
- **`rebuild_chat_state(bot, storage)`** — startup hook registered **FIRST** (before roster/persistence/expiry/retention): restores `search_queue`, mutual-only `pair_map`, `chat_opened_at` (epoch→monotonic), `last_mode`; FSM → `in_queue`/`in_chat`; demotes one-sided/banned rows (RAM + persisted `ended`); deliberately does NOT restore `last_partner`/`rematch_offers`/`_rematch_declined`; then runs `_sweep_expired` once.
- **`config.py`**: `redis_url`, `cloudflare_deployment_id` (platform-injected, read-only), `_normalize_database_url` (`postgres://`/`postgresql://` → `postgresql+asyncpg://`), properties `database_dialect` (`postgresql`/`sqlite`/scheme) and `in_cloudflare_container`.
- **`database/engine.py::engine_options(url)`** (pure): sqlite → `connect_args timeout=30`; postgres → `server_settings timezone=UTC` + `pool_size 10/max_overflow 20/pool_recycle 1800`; sqlite PRAGMA hook gated on `database_dialect == "sqlite"`.
- **`bot.py`**: `build_fsm_storage(redis_url)` (RedisStorage lazily / MemoryStorage fallback with error log; URL never printed raw), `_install_stop_signal()` (SIGTERM → `task.cancel()`, `add_signal_handler` + `signal.signal`/`call_soon_threadsafe` fallback for Windows), `main()` = stop-signal **then** `in_cloudflare_container AND database_dialect == "sqlite"` → `SystemExit(1)` guard (ephemeral disk) **before** `init_db()`.
- Guards added for PG/any-DB: `handlers/profile.py` city ≤128 / height ≤32 (validated + escaped in confirm card), `handlers/admin.py::cb_backup` shows a Persian pg_dump hint when dialect ≠ sqlite (never sends a stale `database.db`).
- Ops: container idles 10 min → SIGTERM (no SIGKILL); next webhook/`/health` boots fresh and rebuilds; `/health` = `{"status":"ok"}`; `max_instances` must stay 1; Workers Paid + egress. `LOG_FILE=""` (stdout only), `GROUP_KEYBOARD_CLEANUP=false` (middleware still covers first sight).

---

## ۳. ساختار پوشه‌ها / Directory map

```
bot.py              entry point, wiring, register_commands, main()
config.py           pydantic-settings Settings (all .env keys) + admin_ids parsing
filters.py          IsAdmin / IsRootAdmin / IsProfileComplete / IsBanned
test_connection.py  standalone reachability probe (direct / PROXY_URL / getMe)
database/           models (incl. ChatPair mirror), engine (init_db migration + engine_options), session factory
handlers/           all aiogram routers (13 files)
keyboards/          reply.py, inline.py, admin.py, user.py, __init__.py
middleware/         admin_guard, force_join, keyboard_guard, group_*_watch
states/fsm.py       all FSM StatesGroups
utils/              economy, emoji, group_*, roster, whisper helpers
smoke/              verify.py (one-command runner) + smoke_*.py self-checks (no Telegram needed)
scripts/            backup_db.py (online sqlite backup + rotation; sqlite3-only — see cb_backup guard)
render.yaml         CURRENT deployment: Render Python Web Service (pip install + python bot.py; no Docker/wrangler)
deploy/cloudflare/  OPTIONAL Cloudflare Containers files, kept OUT of the repo root so no host auto-runs wrangler:
  Dockerfile        (python:3.12-alpine → python bot.py, EXPOSE 8080; needs repo-root build context)
  .dockerignore     (keeps .env / venv / *.db* / backups / node_modules out of the image)
  wrangler.jsonc    (Worker + container config: vars, DO binding, max_instances 1)
  worker/index.js   (path gate + env/secret forwarding to the container)
  package.json      (@cloudflare/containers + wrangler — Node tooling)
.env / .env.example / requirements.txt / pixel-bot.service (systemd, for a server) /
  README.md (run + Render deploy + optional Cloudflare) / WEBHOOK.md (webhook guide + research)
```

### handlers/ (routers — order matters, see §4)

| File | Purpose | Key handlers |
| --- | --- | --- |
| `group_lifecycle.py` | `my_chat_member`/`chat_member` → onboarding card, rights request, roster feed | `on_bot_membership_changed`, `on_member_changed`, `on_bot_left_group` |
| `inline_anon.py` (158 KB, biggest) | نجوا via inline mode: inline query, chosen result, read/stats/options callbacks | `on_inline_query`, `on_chosen_inline_result`, `cb_read_whisper` |
| `anon_chat.py` | Real-time anonymous 1-on-1 session (request→active→ended), DB-backed | `cb_start_anon_chat`, `cb_anon_chat_accept`, `relay_to_partner` |
| `navigation.py` | `/start`, `/menu`, «بازگشت» — must be first among menu routers | `nav_menu`, `nav_start`, `nav_back_to_menu` |
| `keyboard_fix.py` | `/fix_keyboard` — clears a stuck reply keyboard, **open to any group member** | `cmd_fix_keyboard` |
| `admin.py` | Whole admin panel; router-wide `IsAdmin()` + `IsRootAdmin()` on root-only; **support-ticket inbox** (§3b: `cb_support_*`, `admin_support_send_reply`, `_render_admin_ticket`) | `cb_stats`, `cb_broadcast_*`, `cb_cost_*`, `cb_limits*`, `cb_gift_*`, `cb_manage_admins` |
| `support.py` | User side of «🎧 پشتیبانی»: open card, write/follow-up, history + transcript; `deliver_team_reply` DM shared with the panel | `open_support`, `cb_support_*`, `submit_support_message` |
| `whisper.py` (59 KB) | `/نجوا` command flow, private-chat whisper, body collection | `cmd_whisper`, `whisper_collect_body`, `cb_whisper_view` |
| `anonymous.py` | Guest inbox / hybrid async+real-time anon chat | `show_inbox`, `cb_inbox_open`, `cb_anon_block` |
| `chat.py` | Matching + relay: `pair_map`/`search_queue` in memory, 24h lifetime, reports | `start_search`, `forward_text`, `end_chat`, `report_user` |
| `profile.py` | Profile FSM, profile view (+ «✏️ ویرایش پروفایل» field picker = `ProfileEdit`), blocked list, wallet (coins, daily bonus, referral) | `process_age/city/gender/height`, `show_profile`, `_send_profile_card`, `cb_profile_edit`, `edit_age/city/gender/height/photo`, `wallet_daily_bonus` |
| `start.py` | `/start`, deep links, `/help`, `/inline`, admin dual-panel | `cmd_start`, `cmd_help`, `cmd_inline_help` |
| `inline_menu.py` | The 3-option bare `@bot` picker (tutorial / نجوا / ناشناس) | `on_inline_query` articles |

### utils/

| File | Purpose |
| --- | --- |
| `economy.py` (38 KB) | Coins, policy cache (`get_policy`, 5s TTL), rates, refunds, ledger, matching modes, rate limits, `round_coins/fmt_coins/parse_amount/parse_int` |
| `emojis.py` | Central emoji manager; dual fallback plain Unicode + custom-emoji-id (Premium not required) |
| `group_commands.py` | ONE source for all command help text (`GROUP_COMMANDS`, `group_commands_text` — **currently unused**, group is silent) |
| `group_admin.py` | Reads bot's own rights, `promote_text`/`anonymous_admin_text`/`chat_link`, `notify_admins` |
| `group_cleanup.py` | Startup sweep: re-send `ReplyKeyboardRemove` to every known group (BOM in file → AST parse can fail, irrelevant) |
| `group_members.py` | In-memory roster of group members (`MemberProfile`) so inline picker resolves targets without DB |
| `roster_store.py` / `roster_sync.py` | Durable half of roster (batched writes) + backfill via `getChatAdministrators` |
| `target_lookup.py` | Single place that resolves a whisper recipient (used by `/نجوا` and inline picker) |
| `membership.py` | Shared "is user in channel/group?" checks (force-join + whisper send/view) |
| `whisper_config.py` | Single-row `whisper_config` accessor with cache |
| `support.py` | Support-ticket status values (`STATUS_OPEN/ANSWERED/CLOSED`, `status_label`, `status_from_filter`) + the user-facing card/reply templates (`support_home_text`, `support_reply_text`, …) — ONE source so greeting/thanks can't drift |
| `chat_types.py` | Group/private predicates shared by keyboard guard + cleanup |
| `helpers.py` | `format_user_profile` (HTML-escaped), `RateLimiter`, text formatting |
| `db_config.py` | **DEPRECATED**, delegates to `config.py` |

### middleware/ (registration order = priority order, outermost first)

1. `ReplyKeyboardGuardMiddleware` (message + callback)
2. `GroupKeyboardWatchMiddleware` (message + callback)
3. `GroupMemberWatchMiddleware` (message + callback)
4. `BlockBannedMiddleware` (message + callback)
5. `ForceJoinMiddleware` (message + callback + **inline_query**)
6. `AdminPanelGuardMiddleware` (callback only, **last**)

Plus **session-level** `install_keyboard_guard(bot)` — wraps the Bot session so no outgoing `ReplyKeyboardMarkup` can ever reach a group, even from a handler that forgot its filter.

### database/

- `models.py` — `User`, `BlockList` (+ `UserReport` for the report inbox; `BlockList` has no `created_at`), `SupportTicket`/`SupportMessage` (answerable support conversations; ticket `status` = open/answered/closed, `updated_at` float-to-top, message `is_read` drives the panel's unread badge), `AnonymousContact`, `AnonymousMessage`, `AnonChatSession`, `Whisper`, `InlineWhisper`, `GroupChat`, `GroupRoster`, `WhisperConfig`, `RequiredChannel`, `BotPolicy`, `CoinTransaction`.
- `engine.py` — `create_async_engine`, `async_session_factory`, `init_db()` (create tables + **startup migrations** for legacy columns), `get_session()`.
- Raw SQL exists **only** in `init_db()`, guarded by `_ident()`/`_IDENT = [A-Za-z_][A-Za-z0-9_]*` — never route user input through f-string SQL.

---

## ۴. ترتیب راه‌اندازی / Startup order (`bot.py`)

`build_bot_and_dispatcher()`:

1. `Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))` ← **global HTML**
2. `install_keyboard_guard(bot)` (session-level)
3. middlewares in the order of §3
4. routers **specific → general**: `group_lifecycle → inline_anon → anon_chat → navigation → keyboard_fix → admin → support → whisper → anonymous → chat → profile → start`
   - `inline_anon` first so state-gated catch-alls don't swallow the reply to an inline card.
   - `anon_chat` before menu routers (bare message handler with its own SkipHandler bail-out).
   - `navigation` first among *menu* routers; `keyboard_fix` above every state-gated catch-all.
   - `support` above the state-gated relay catch-alls (like `navigation`): the «🎧 پشتیبانی» label must be claimed before `chat`/`anonymous` relay it. Its entry refuses to open inside a live session (warns instead) so it never swaps a live session's FSM state and strands a partner.
5. startup hooks in this order: **`rebuild_chat_state` (FIRST — everything else assumes the maps are real)** → group keyboard cleanup → `start_roster_sync` → `start_persistence` → `start_chat_expiry` → `start_retention`; matching shutdowns.
6. `dp.workflow_data["dp"]`, `["storage"]` for handlers/startup callbacks.

`main()`: `_install_stop_signal()` (SIGTERM → cancel the running task) → container+SQLite guard (`in_cloudflare_container` + `database_dialect == "sqlite"` → `SystemExit(1)`) → `logging.basicConfig` → `init_db()` → token check → build → `register_commands()` → `_clear_webhook()` (retry) → `dp.startup`/`start_polling` (or `run_webhook`).

`register_commands()` — **private scope only**: sets commands scoped to `AllPrivateChats` and **deletes** group/default scopes (deleting is what actually empties a scope; overwriting doesn't).

---

## ۵. قوانین سخت / Hard rules (违反 = bug)

1. **`ParseMode.HTML` is global.** Every `send_*` parses HTML. User-controlled text (`first_name`, `username`, `city`, `height`, `chat.title`, amounts) **must be escaped** with `html.escape` — an unescaped `<` in a name kills the send. Already fixed in `helpers.format_user_profile`, `chat._format_profile`, `start.py` (4 sites), `admin.py`, `force_join.py`.
2. **گروه سکوت می‌کند / Groups are silent.** `/start`, `/help`, `/inline`, `/menu` return with **zero** output in groups. The only text the bot ever posts in a group is: (a) one onboarding card when added, (b) an answered نجوا card, (c) `/fix_keyboard` reply, (d) rights/demotion request. No slash-hint replies (the `on_slash` handler was deleted — do not re-add it).
3. **One card on add.** `group_lifecycle._post_onboarding` posts a *single* merged card (welcome + rights ask) with `keyboards.inline.group_onboarding_kb`; promotion buttons use `https://t.me/<bot>?startgroup&admin=delete_messages+restrict_members+invite_users+manage_chat` (URL-joined with `+`). Never reintroduce separate welcome/promotion cards.
4. **No `ReplyKeyboardMarkup` in groups.** Enforced at session level by `keyboard_guard`; new reply keyboards must go through `keyboards/reply.py` conventions.
5. **Admin callbacks**: prefix `admin:`; router-level `IsAdmin()` + `AdminPanelGuardMiddleware` backstop. Adding an `admin:` callback means it is automatically guarded — don't bypass.
6. **Decimal costs**: only the 4 `BotPolicy` costs are fractional (`Float` columns); rewards/limits stay integers; all money math goes through `round_coins`/`fmt_coins`/`parse_amount`. UI shows `fmt_coins` (`7.5 سکه`), never raw `str(float)`.
7. **Never `drop_pending_updates=True`** — losing a `chosen_inline_result` leaves a dead نجوا card. Keep `False` in `delete_webhook`.
8. **Session/DB style**: always `async with async_session_factory() as session:`; no raw SQL outside `init_db()`.
9. **Cloudflare invariants**: `max_instances` stays **1** (RAM state is per-process); no SQLite inside the container (boot guard refuses it — ephemeral disk); secrets only via `wrangler secret put` (never `vars`, never `.env` in the image); the Worker path gate must keep waking the container ONLY for `POST WEBHOOK_PATH`/`GET /health`; `chat_pairs` writes stay on the `_persist_*`/`leave_search_queue` helpers (raw writes bypass the split-brain rules).

---

## ۶. اقتصاد / Economy (`utils/economy.py`)

- `COIN_DECIMALS=2`, `MAX_POLICY_VALUE=1_000_000`.
- Values live in the single `bot_policy` row (`get_policy()`, 5s cache → `peek_policy()` for hot paths).
- Costs: whisper/match/anon/… four fractional fields; charging always via `_coins_after_charge` + `_ledger` (writes `CoinTransaction`, rounds to 2 dp).
- Persian numerals accepted on input (`parse_amount` has `_NUMERAL_MAP` + thousands separators).
- Rate limits: in-memory `_SlidingWindow` (per-minute/per-hour), `should_warn` cooldown.

---

## ۷. سبک کد / Code style & conventions

- **Comments/docstrings in English**, extremely detailed ("why" narratives); **user-facing text in Persian** (`سلام`, `⛔ دسترسی غیرمجاز`, «نجوا», «سکه»).
- Heavy module-level docstrings explaining design decisions — read them first; they usually answer "why is this like this".
- Emoji constants in `utils/emojis.py` (never inline raw emoji in handlers; use `get_plain_emoji`/icon ids).
- Buttons: `style="primary" | "success" | "danger"` (Bot API 8.2+), callbacks via `keyboards/*.py` constants (`ONBOARDING_*`, `HELP_START_PARAM`, …).
- Callback data is `namespace:action` (e.g. `admin:cost_edit`), guarded + routed by prefix.
- FSM states in `states/fsm.py` only; routers use `StateFilter(...)` via aiogram's `F.state`.
- Every router module exposes `router = Router()`; `handlers/__init__.py` re-exports them as `*_router`.

---

## ۸. تاریخچهٔ کارهای انجام‌شده / Session history (already done — do not redo)

1. **اعشاری‌سازی هزینه‌ها** — 4 cost columns → `Float`, `round_coins`/`fmt_coins`/`parse_amount` everywhere; legacy INTEGER columns verified to hold fractions (no DB rebuild).
2. **امنیت** — HTML-escaping audit of every user string; `middleware/admin_guard.py` added as last callback middleware; raw-SQL identifier guard in `database/engine.py`.
3. **سکوت گروه** — group branches added first in `cmd_start`/`cmd_help`/`cmd_inline_help`/`nav_menu`; `on_slash` + regex deleted; docs refreshed (`whisper.py`, `group_lifecycle.py`, `group_commands.py`…).
4. **کارت واحد هنگام اضافه‌شدن** — `_post_onboarding` + `group_onboarding_kb`, `group_welcome_kb` deleted, `group_admin_link()` now uses `startgroup&admin=`; demotion still posts its own ask (`post_in_group` flag).
5. **رفع `NameError: logger`** — module-level `logger` in `bot.py` (was a local inside `main()`), so network-failure paths log instead of crashing.
6. **پراکسی و مدیریت خطای اتصال** — `PROXY_URL` in `.env` → `config.py:proxy_url` → `bot.py:build_session()` (`AiohttpSession(proxy=...)` + `aiohttp-socks`, installed in venv *and* system Python); `bot.py:_fallback_to_direct()` swaps a dead proxy for a direct session inside `GuardedSession`; `bot.py:log_connection_advice()` prints the EN/FA troubleshooting block and exits 1 (no traceback) on startup *or* polling failure; new standalone `test_connection.py` (direct → proxy → `getMe`, exit 0/1).
7. **بخش مستقل «عضویت اجباری»** — force-join moved OUT of «تنظیمات نجوا» into its own panel section (`admin:forcejoin*` callbacks, `admin_forcejoin_kb`/`admin_forcejoin_channels_kb`); the legacy single `whisper_config.required_chat_*` chat is migrated into `required_channels` on first section open (`_migrate_legacy_join_chat`); fixed the `TypeError: object NoneType can't be used in 'await' expression` crash — `clear_membership_cache()` is **sync**, 3 `await`s removed in `handlers/admin.py`; add-channel now verifies the bot's own rights (`_required_chat_rights_error` → Persian "add & promote the bot" message, FSM state kept for retry) and every screen shows ⚠️ rows where `bot_is_admin` fails (`_unchecked_channel_ids`); stale `admin:whisper:join*`/`channels*` buttons redirect to the new section; `AdminWhisper.waiting_for_chat` deleted; private `/نجوا` help now lists channels via `configured_channels()`.
8. **هدیهٔ گروهی (همه / n تصادفی)** — the panel's «هدیه دادن» gained a WHO screen (`admin:gift:<type>:one|all|random` → `admin_gift_scope_kb`): one-user keeps the old id→amount FSM; «همه» jumps straight to amount; «تصادفی» asks `AdminGift.waiting_for_count` first (capped at the non-banned count). Bulk grants go through new `utils/economy.bulk_add_coins`/`bulk_add_premium_days` (ONE transaction, returns granted telegram_ids; `user_ids=None` = every non-banned user, same population as broadcast) and require a confirmation card with per-user amount + total (`AdminGift.confirm_send` + `admin_gift_confirm_kb`). Recipient notices run as a background task (`_GIFT_NOTIFY_TASKS` keeps refs; 0.05 s pacing, failures skipped) so the admin gets the panel back immediately. Registration order matters: `cb_gift_confirm` must precede the `admin:gift:` prefix handler (verified by `smoke_gift.py`).

9. **پاکسازی git (staged، کامیت با کاربر)** — added root `.gitignore` (`.env*` except `.env.example`, `*.db*`/`backups/`, `venv/`, `__pycache__/`/`*.pyc`, `logs/`, `.opencode/node_modules/`) and removed 3673 junk paths from the index **without touching the worktree** (`git ls-files -z … | git update-index --force-remove -z --stdin` — note: `--pathspec-from-file` is NOT supported by `update-index`, and `--stdin` must be the last option). Everything is now `git add -A` staged; the working tree is untouched — **the user commits, never commit yourself**. `.env` was never actually tracked (audit claim was wrong) but `database.db` WAS — token lives only in the untracked `.env`.

10. **فاز ۲ — زیرساخت** — `BASE_DIR` anchors (imports work from any CWD), sqlite URL absolutizer (relative `sqlite+aiosqlite:///…` → absolute), WAL PRAGMAs, `_setup_logging()` + `_TokenRedactingFilter` (pattern `\d{8,10}:[A-Za-z0-9_-]{30,}` — **no `\b` boundaries** or the match breaks), `_hook(name, **kwargs)` filters kwargs so handlers can register startup/shutdown safely, keyboard cleanup backgrounded, `engine.dispose()` on shutdown, pinned `requirements.txt`. → `smoke_phase2.py` 16/16.

11. **فاز ۳ — اقتصاد** — `_atomic_deduct` (one `UPDATE … WHERE balance >= ?`, no race), `check_and_deduct_balance → float | None` returning the exact deducted amount, exact-amount refunds (`refund_whisper`/`refund_match`), `peek()`/`record()` ledger accessors, free-whisper exploit closed. → `smoke_economy.py` 25/25.

12. **فاز ۴ — عضویت اجباری** — `is_member` **fails open** (Telegram API error ⇒ allow; never lock everyone out), `_answer_join_prompt` dedupes the join prompt, `missing_required_chats(..., force_refresh)`. → `smoke_forcejoin.py` + `smoke_force_join4.py` 39/39.

13. **فاز ۵ — ریداکتورهای handlers** — ban-notify-once, HTML `escape()` on every user string (admin/chat/profile/start), dead `reply_markup` kwargs removed (`callback.answer` swallows it), `parse_int` hardening (Persian numerals accepted), FSM coverage audit (every state has a message handler), dead `AnonymousStates` group removed. → `smoke_handlers5.py` 28/28.

14. **فاز ۶a — پشتیبان‌گیری** — `config.backup_keep=14`; `scripts/backup_db.py::run_backup()` (sqlite3 online backup, `-N` collision suffix, CLI `--db/--out/--keep`); root-only «💾 پشتیبان‌گیری» button (`admin:backup`) → `cb_backup` (`IsRootAdmin`, `asyncio.to_thread`, `FSInputFile` to PM). **Root panel = 12 buttons** (smoke_forcejoin expectation was fixed 11→12).

15. **فاز ۶b — نگهداری داده** — `retention_whisper_days=30 / _anon_message=60 / _anon_session=7 / _ledger=365 / _report_days=180` (0 = forever); `utils/retention.py::purge_expired()` sweeps **5 tables** (`whispers`, `anonymous_messages`, `anon_chat_sessions`, `coin_transactions`, `user_reports`), first sweep at 600 s then daily; `start_retention`/`stop_retention` registered via `_hook` in `dp.startup/shutdown`.

16. **فاز ۶c — تاریخچهٔ سکه** — `REASON_LABELS`/`reason_label()` moved `handlers/admin.py` → `utils/economy.py`; `ICON_HISTORY=📜`; wallet card row 2 «📜 تاریخچهٔ سکه» → `wallet:history`; `handlers/profile.py::wallet_history` (last 10 `CoinTransaction` DESC, reasons translated+escaped, Tehran time) + `wallet_open` re-render. **Timestamp convention: user/panel times = naive-UTC `created_at` + Tehran `timedelta(hours=3, minutes=30)`.**

17. **فاز ۶d — rematch + صندوق گزارش‌ها** — `chat.py`: `rematch_offers` dict (`_REMATCH_TTL=600`), `rematch:ask|accept|decline` callbacks (both parties revalidated free/unblocked before re-pairing), `_announce_match(bot, ..., charge=False)` gives free rematches (⚠️ signature now `(bot, fsm_storage, user_id, partner_id, mode, partner_mode, *, charge=True)` — no `state`/`message`); `report_user` also inserts `UserReport` (new table; `BlockList` has no `created_at`), admin inbox `admin:reports` (last 10, Tehran time, back → `admin:users`). End-card wiring was redesigned in entry 19 — `rematch_end_kb` and `_force_end(..., reply_kb=)` no longer exist.

18. **فاز ۶e — ویرایشگرهای جامانده** — user flags: `admin:user:flags` → `AdminPIS.waiting_for_flags_id` (miss keeps state for retry) → `admin_user_flags_kb` toggling `is_vip|has_subscription|is_exempt` via `admin:flags:toggle:<field>:<id>` + `session.merge`; whisper max-length: `admin:whisper:maxlen` → `AdminWhisper.waiting_for_max_length` (bounds **100–4096**, `_mutate_whisper_config(max_length=)` then re-renders config card).

19. **批次 UX — پایان چت + حریم خصوصی** — **end flow**: `keyboards/reply.py::chat_end_kb(has_rematch)` is a REPLACEMENT reply keyboard sent on every end card — rows `NEXT_CHAT_LABEL="🔍 چت بعدی"` → `REMATCH_LABEL="🔁 اتصال مجدد"` (conditional) → `BACK_TO_MENU`; the old design attached only an inline rematch button while «لغو چت»/«بلاک» stayed visible with no idle-state handler (dead taps, no route to the next chat) and `rematch_end_kb` is **deleted** (function + exports + smoke). `chat.py` new module state: `last_partner`/`last_mode` (set by `_remember_pair_end` on `_force_end`/`_sweep_expired`/accept; `last_mode[user_id]=mode` in `_begin_search`) and `_rematch_declined: set[frozenset[int]]` (`_decline_key`). `start_search` is now a thin label wrapper over extracted `_begin_search(message, state, fsm_storage, mode)`. New reply handlers `next_chat`/`rematch_request` branch in_chat/in_queue/foreign-FSM/generic — **every branch answers**, foreign states reply WITHOUT swapping the keyboard (that flow's own escape row stays). Shared `_ask_rematch(requester, partner_id, bot, requester_state, requester_name) -> str | None` serves both the reply button and legacy `rematch:ask:` cards: refusals returned as Persian text, `last_partner` popped only for permanent refusals (declined/blocked), offer card deduped + "mutual offer" answered. `cb_rematch_decline`: answer → lock BOTH directions → edit card → notify requester with **no reply_markup** (never clobber another flow's keyboard); re-request after decline is impossible. `_announce_match` success discards an old lockout (fresh consent supersedes an old no). Offer text name fallback = «یک کاربر» (was `str(requester)` = id leak). Whisper `_MENU_ESCAPE_TEXTS` += both new labels. **Privacy**: blocked list = `_blocked_label` (first name, else «کاربر بلاک‌شده» — never id/@username) + shared `_blocked_entries_for_kb`; unblock alert shows the label; `inline.py::blocked_list_kb` fallback id-free; `anonymous.py` inbox header drops `از کاربر {sender_id}` (positional «کاربر ۱» only); `whisper.py::mention_html` = `https://t.me/<user>` link for usernames, plain name otherwise (no `tg://user?id=`); `inline_anon.py::_target_mention` (name/«یکی از اعضای این گروه», no link), `_target_label(target_username, target_name)` (username→name→«عضو گروه»), `_target_display` = name only. Self-ids stay BY DESIGN: `inline_menu` «آیدی‌عددی من», inline self-prefill, all admin surfaces. → smoke_phase6 §7 rewritten + §8 privacy source scans + §9 functional lockout/offer/dedupe tests; full `smoke/verify.py` green.

20. **آیدی کانال داخل متن پرامپت عضویت اجباری** — new `utils/membership.py::join_bullet(chat)` renders each join line as `• عنوان — <a href="https://t.me/x">@x</a>` (copyable ID for users whose URL-button tap dies on a bad VPN; title alone linked when it already IS `@handle`; private invite links get a `لینک عضویت` label; derived `t.me/c/…` omitted). Used by ALL 4 prompt sites: `middleware/force_join._answer_join_prompt`, `whisper._join_prompt_text` + both single-channel `_JOIN_PROMPT.format` sites, `inline_anon.join_prompt_text` (also feeds `start._handle_inline_help`). Dedupe fires only on a leading `@` + casefold-equal username — a plain casefold eats display titles like `MyChan` (caught by `smoke_force_join4`).

21. **حالت وبهوک (`RUN_MODE`)** — dual run mode: `polling` (default, untouched) / `webhook` = local aiohttp server (`SimpleRequestHandler` + `GET /health`) registered via `setWebhook`. New settings `WEBHOOK_BASE_URL/PATH/SECRET`, `WEBAPP_HOST/PORT` (`config.py:_normalize_run_mode`); `bot.py` gains `build_webhook_url` (https-only validation), `webhook_secret_token` (charset-validated or per-boot random), `build_set_webhook_kwargs` (`drop_pending_updates=False`, `allowed_updates=dp.resolve_used_update_types()` — required or `chat_member` rosters die), `build_webhook_app`, `run_webhook` (listen → set_webhook order, `AppRunner(handle_signals=False)`, webhook left registered on shutdown, network/400 failure paths EN/FA). `main()` branches after the shared `_clear_webhook` probe. Local test without any SSL via `cloudflared tunnel` (Telegram only accepts https:// + ports 443/80/88/8443). Docs: `WEBHOOK.md` incl. Cloudflare research (Workers Python = not viable for this project: no persistent disk/long-running tasks/shared memory; Containers = viable later but ephemeral disk forces a DB move; recommended = ordinary server + Cloudflare Tunnel). `.env`/`.env.example` documented; → `smoke_webhook.py` 35/35, full `verify.py` 10/10.

22. **فاز ۲ — استقرار Cloudflare Containers (بدون VPS)** — the whole bot runs as a Docker image behind a path-gating Worker (see §2c for the file-by-file map). Code changes: `config.py` += `redis_url`/`cloudflare_deployment_id`/`_normalize_database_url`/`database_dialect`/`in_cloudflare_container`; `database/engine.py` += `engine_options(url)` (pure, sqlite timeout=30 vs postgres UTC+pool) and the PRAGMA hook now gated on dialect; `database/models.py` += `ChatPair` mirror (one row/user, `opened_at` Python epoch, `autoincrement=False` PK); `handlers/chat.py` += persistence section (`_KEEP`, `_write_chat_row`, `_persist_queue/_persist_pair/_persist_ended` conditional-update, `leave_search_queue`) wired into `_sweep_expired`/`_force_end`/`_abort_pair`/`report_user`/`cancel_search`/`cb_rematch_accept`/`_begin_search` + **`rebuild_chat_state`** startup hook (registered FIRST; mutual-only pairs, epoch→monotonic clock, banned/one-sided demoted+persisted, FSM restored, then `_sweep_expired`) and `_free_stale_chat_state`; `handlers/navigation.py` both `search_queue.pop` sites → `leave_search_queue`; `handlers/profile.py` city ≤128/height ≤32 guards + escaped confirm card; `handlers/admin.py::cb_backup` Persian pg-dump hint when dialect ≠ sqlite; `bot.py` += `build_fsm_storage(redis_url)` (lazy Redis / Memory fallback), `_install_stop_signal()` (SIGTERM→cancel, Windows fallback), rebuild registered first, container+sqlite boot guard; `.env.example` += `REDIS_URL`. New build files `Dockerfile`, `.dockerignore`, `wrangler.jsonc`, `worker/index.js` (string-env forwarding + path gate), `package.json`; `.gitignore` += `node_modules/`/`.wrangler/`; docs: **`README.md`** created (run + exact deploy steps: npm install → wrangler login → 5× `secret put` → deploy → set `WEBHOOK_BASE_URL` → redeploy → `curl /health`) + `WEBHOOK.md` option-2 updated to «انجام شد». → `smoke_cloudflare.py` (~90 checks incl. a full functional rebuild), full `verify.py` **11/11**.

23. **فاز ۳ — استقرار روی Render + جداسازی Cloudflare** — the host was running `npx wrangler deploy` (log: «Could not detect a directory containing static files») because a root `package.json` + `wrangler.jsonc` made it look like a Node/Worker project. Moved `package.json`/`package-lock.json`/`wrangler.jsonc`/`Dockerfile`/`.dockerignore`/`worker/` into `deploy/cloudflare/` (optional, documented in its README); added root `render.yaml` (Python web service: build `pip install -r requirements.txt`, start `python bot.py`, `healthCheckPath: /health`, secrets `sync: false`); `config.py` += `port` field (env `PORT` overrides `WEBAPP_PORT` and widens bind to `0.0.0.0` when `WEBAPP_HOST` unset, via `model_fields_set`); `bot.py` warns when `PORT` is set with SQLite; `.gitignore` += `.venv/`; `.env.example` += `PORT`; `smoke_cloudflare.py` updated (paths + root-no-wrangler + render checks); README §3 = Render, §4 = optional Cloudflare; WEBHOOK.md updated. → full `verify.py` green.

24. **فاز ۴ — اجرای Cloudflare از ریشهٔ ریپو + secretهای لوکال + هلپر وبهوک** — `npx wrangler dev` از ریشه با خطای `Missing entry-point to Worker script` می‌افتاد چون کانفیگ داخل `deploy/cloudflare/` است. اصلاحات: (الف) `wrangler.jsonc` حالا `containers[0].image_build_context = "../.."` دارد → context داکر = ریشهٔ ریپو، پس Dockerfile **in-place** بیلد می‌شود (نیاز به کپی فایل‌ها به ریشه نیست)؛ `wrangler` مقدار `image`/`image_build_context` را نسبت به **پوشهٔ فایل کانفیگ** حل می‌کند (از سورس wrangler تأیید شد: `path.resolve(dirname(configPath), …)`)، بنابراین `npx wrangler deploy/dev/tail -c deploy/cloudflare/wrangler.jsonc` از ریشه کار می‌کند. (ب) `.dockerignore` **ریشه** ساخته شد (چون با context=ریشه، داکر همان را می‌خواند): `.env`, `.dev.vars`, `venv/`, `*.db`, `backups/`, `**/node_modules/` و… بیرون از image. (ج) `deploy/cloudflare/package.json` اسکریپت‌های `cf:dev/cf:deploy/cf:tail/cf:secret/cf:check` (همه با `-c wrangler.jsonc`) گرفت؛ اجرا از ریشه: `npm --prefix deploy/cloudflare run cf:dev`. (د) `.dev.vars.example` (الگو، commit می‌شود) + `.dev.vars` تولیدشده از `.env` ریشه (git-ignored؛ `BOT_TOKEN` واقعی، `WEBHOOK_SECRET` تصادفیِ valid، `RUN_MODE=webhook`, `ADMIN_IDS`, `DATABASE_URL` لوکال SQLite، `WEBHOOK_BASE_URL` خالی)؛ wrangler این فایل را از **پوشهٔ کانفیگ** می‌خواند (`path.resolve(configDir, ".dev.vars")` از سورس)، پس هم با `-c` از ریشه و هم از داخل پوشه load می‌شود. `.gitignore` += `.dev.vars`/`.dev.vars.*` + `!.dev.vars.example`. (ه) `deploy/cloudflare/set_webhook.py` — هلپری که `build_webhook_url`/`build_set_webhook_kwargs` خودِ پروژه را ایمپورت می‌کند تا `allowed_updates` (شامل `chat_member`) از dispatcher ساخته شود؛ فلگ‌های `--url`/`--secret`/`--print-curl`/`--remove`؛ اگر `WEBHOOK_SECRET` خالی باشد عمداً خطا می‌دهد (secret تصادفیِ هر بوت با Worker نمی‌خواند → ۴۰۱). (و) Docs: `deploy/cloudflare/README.md` بازنویسی (دستورات `-c` + npm scripts + چک‌لیست + invariantها)، README §۴ و WEBHOOK.md §۳ آپدیت، Dockerfile/`.dockerignore` کامنت‌ها اصلاح. **⚠️ واقعیت مهم:** Containers فقط روی **Workers Paid (‏$5/ماه)** است (`Free: N/A` در pricing اوت ۲۰۲۶) — «free tier» برای این معماری ممکن نیست. → `smoke_cloudflare.py` +~25 بررسی (image_build_context، root .dockerignore، dev.vars.example، cf:* scripts، `set_webhook._resolve_url`)، full `verify.py` 11/11 سبز؛ `cf:check` از ریشه خروجی 54 KiB می‌دهد.

25. **رفع نشتی دکمه‌ها در چت ناشناس** — باگ: در حالت گفتگوی ناشناس (هم inbox `handlers/anonymous.py::AnonChatStates.in_session` و هم real-time `handlers/anon_chat.py`)، اگر کاربر فراموش می‌کرد چت را ببندد و یکی از دکمه‌های منوی ربات را می‌زد، متن دکمه به عنوان پیام عادی برای طرف مقابل relay/ذخیره می‌شد. اصلاح: در `keyboards/reply.py` یک ست `BOT_CONTROL_TEXTS` (همهٔ لیبل‌های کنترلی reply-keyboard، به‌جز مقادیری مثل سن/شهر که پیام واقعی‌اند) + هلپر `is_bot_control_text(text)` (شروع با `/` یا عضویت در ست) اضافه شد و از `keyboards` export شد. `anonymous.py::anon_message_router` حالا قبل از relay با `is_bot_control_text` چک می‌کند و در صورت true پیام «ابتدا با «❌ پایان چت / بازگشت» گفتگو را ببندید» می‌دهد و relay نمی‌کند (دکمهٔ خروج در باگ قبلی handle می‌شود). `anon_chat.py::relay_to_partner` هم شرط قبلی (فقط `/` و `BACK_TO_MENU_TEXTS`) با `is_bot_control_text` جایگزین شد تا با `SkipHandler` به هندلر خودش بدهد؛ import قدیمی `BACK_TO_MENU_TEXTS` حذف شد. کامنت قدیمی `start.py::cmd_inline_help` که ادعا می‌کرد لیبل‌های منو در چت فعال relay می‌شوند به‌روز شد. → `smoke_handlers5.py` 28→35 (بررسی پوشش لیبل‌ها + فیلتر هر دو relay).

26. **سیستم پشتیبانی (تیکت قابل‌پاسخ)** — قابلیت جدید «🎧 پشتیبانی»: کاربر از منوی اصلی (یا `/support`) وارد می‌شود، پیام می‌نویسد و تیم در همان گفتگو جواب می‌دهد؛ ادمین‌ها تیکت‌های «در انتظار پاسخ» را از «پاسخ‌داده‌شده» جدا می‌بینند و همه‌چیز قابل پیگیری است. **مدل‌ها** (`database/models.py`): `SupportTicket` (جدول `support_tickets`: `user_id`, `status`, `created_at`, `updated_at`) + `SupportMessage` (جدول `support_messages`: `ticket_id`, `sender_id`, `is_admin`, `content`, `is_read`). وضعیت‌ها: `open` (کاربر آخر نوشته)، `answered` (تیم جواب داده)، `closed` (بایگانی؛ پیام بعدی **تیکت جدید** می‌سازد — `_winner_ticket` فقط `open` را برمی‌گرداند). `updated_at` روی هر پیام جلو می‌رود تا لیست بر اساس «آخرین تماس» مرتب شود. **قالب‌ها** در `utils/support.py` (تک‌منبع): `status_label`/`status_from_filter` + متن‌ها؛ پاسخ تیم = `support_reply_text` = «سلام» + متن ادمین (`escape`شده) + «سپاس‌گزاریم» — دقیقاً همان ساختار درخواست‌شده، پس greeting/thanks هرگز از هم جدا نمی‌شوند. **سمت کاربر** `handlers/support.py` (`support_router`): `open_support` (دکمهٔ منو + `/support`) کارت خانه می‌دهد؛ `support:write` حالت `SupportStates.waiting_for_message` را با پرامپت + `main_menu_kb()` مسلح می‌کند (لغو = دکمهٔ «🏠 منوی اصلی» که `navigation` هندل می‌کند)؛ `submit_support_message` پیام را به تیکت `open` می‌چسباند یا تیکت جدید می‌سازد؛ `support:history`/`support:view:{id}` فهرست و متن گفتگو؛ `support:back` منوی اصلی را برمی‌گرداند. پیام جدید به **همهٔ** ادمین‌ها (ریشه + ارتقایی) با دکمهٔ `admin:support:open:{id}` پینگ می‌شود. `deliver_team_reply` (DM با قالب + منوی اصلی) بین این فایل و پنل مشترک است. **سمت ادمین** در `handlers/admin.py` §۳ب: `admin:support` (شمارش هر سبد)، `admin:support:list:{open|answered|all}:{page}` (فهرست صفحه‌بندی‌شده ۸تایی با برچسب وضعیت + نشانگر 🔴 پیام خوانده‌نشده)، `admin:support:open:{id}` (متن + علامت‌گذاری خوانده‌شده)، `admin:support:reply:{id}` (حالت `AdminSupport.waiting_for_reply`)، `admin_support_send_reply` (ذخیره + ارسال DM + وضعیت `answered`)، `admin:support:close|reopen:{id}`. **کلیدواژه‌ها**: `SUPPORT_LABEL="🎧 پشتیبانی"` در `keyboards/reply.py` (منوی اصلی ردیف ۵ کنار «📖 راهنما») و به `BOT_CONTROL_TEXTS` **و** `whisper._MENU_ESCAPE_TEXTS` اضافه شد؛ `support_router` در `bot.py` **بالای** catch-allهای relay و بعد از `admin` ثبت شد و ورودش داخل چت/سشن ناشناس فعال را رد می‌کند (فقط هشدار می‌دهد) تا state جلسهٔ زنده را عوض نکند و طرف مقابل بی‌پاسخ نماند. `/support` به `PRIVATE_COMMANDS` + کارت `/help` اضافه شد. → `smoke_support.py` (41) + `smoke_handlers5.py` 35 + `smoke_forcejoin.py` (پنل ۱۰→۱۱، ریشه ۱۲→۱۳)؛ کل `verify.py` **12/12**.

27. **ویرایش پروفایل (بخش «✏️ ویرایش پروفایل»)** — زیر کارت «👤 پروفایل من» یک دکمهٔ اینلاین «⚙️ ویرایش پروفایل» اضافه شد که یک منوی انتخاب فیلد باز می‌کند. هر فیلد مستقل ویرایش می‌شود (سن/شهر/جنسیت/قد/عکس) و بلافاصله ذخیره می‌شود — بدون تکرار کل ویزارد. **حالت‌ها** (`states/fsm.py`): `ProfileEdit` با `waiting_for_age/city/gender/height/photo` (صادرشده از `states`). **صفحه‌کلیدها** (`keyboards/user.py`): `profile_card_kb()` (`profile:edit`) و `profile_edit_kb()` (`profile:edit:{age,city,gender,height,photo}` + `profile:edit:back`)؛ هر دو صادرشده از `keyboards`. **هندلرها** (`handlers/profile.py`): کارت پروفایل حالا از تابع مشترک `_send_profile_card` ساخته می‌شود (متن/عکس با دکمهٔ ویرایش اینلاین به‌جای ریپلای‌کیبورد)، `cb_profile_edit` منو را ویرایش می‌کند (روی کارت عکسی fallback به ارسال پیام جدید)، `cb_edit_<field>` حالت را ست و صفحه‌کلید همان فیلد (`age_kb`/`city_kb`/`gender_kb`/`height_kb`/`profile_photo_choice_kb`) را می‌فرستد، پیام بعدی در `edit_<field>` اعتبارسنجی و با `_finish_field_edit` ذخیره می‌شود (سشن DB بسته می‌شود، سپس «✅ تغییرات ذخیره شد» + `main_menu_kb()` + رندر مجدد کارت). لغو = `_cancel_field_edit`. **نکتهٔ حیاتی:** فیلتر `set_gender_standalone` از `~StateFilter(ProfileSetup)` به `~StateFilter(ProfileSetup, ProfileEdit)` تغییر کرد، وگرنه پاسخ «👩 زن/👨 مرد» در حالت ویرایش را می‌بلعید. از `_profile_photo_file_id`/`_send_profile_card` مشترک استفاده می‌شود تا کارت قبل و بعد از ویرایش یکی باشد. → `smoke/smoke_profile_edit.py` (41) به `verify.py` اضافه شد؛ همهٔ ۱۳ مرحله سبز.

Git: سه کامیت — `821db80 create project`, `8b08e45 change and fix bugs and add fichure`, `1e4df1f change to congig the ..` (شامل فازهای webhook + Cloudflare Containers + Render). **همه‌چیز تا `1e4df1f` کامیت و push شده.** تغییرات این فاز (۲۴) هنوز کامیت نشده: modified `.gitignore`/`README.md`/`WEBHOOK.md`/`deploy/cloudflare/{.dockerignore,Dockerfile,README.md,package.json,wrangler.jsonc}`/`smoke/smoke_cloudflare.py` + untracked root `.dockerignore`, `deploy/cloudflare/.dev.vars.example`, `deploy/cloudflare/set_webhook.py`. **The user commits, never commit yourself.** `.env`/`.env.bak`/`database.db`/`venv/`/`backups/`/`logs/`/`node_modules/`/`.wrangler/`/**`deploy/cloudflare/.dev.vars`** are ignored — `git status` must never show them.

---

## ۹. نکات ظریف / Pitfalls that will bite you

- Line numbers in this file drift as the code grows — match on the symbol (`build_session`, `log_connection_advice`), not the number.
- `bot.py` builds the `Bot` with `DefaultBotProperties(parse_mode=ParseMode.HTML)` — forget escaping ⇒ `TelegramBadRequest: can't parse entities`.
- `PROXY_URL` problems fail *silently* at construction (deliberate: `build_session` degrades to direct) — always run `test_connection.py` before assuming the proxy works.
- Editing `database/models.py` column types does **not** rebuild an existing SQLite file; `init_db()` has explicit `ALTER`/recreate logic for that.
- `config.py` comments are mojibake in some readers (encoding artifact), the code is fine.
- `utils/group_cleanup.py` starts with a BOM → some AST tools choke; it runs fine.
- Persian UI + RTL: keep message text simple HTML (`<b>`, `<a>`), never nest weird tags; verify by asserting tag balance in a smoke script.
- Handlers registered in an order that looks arbitrary — the comments in `bot.py:119-162` explain each position; moving a router changes behavior.
- To test without Telegram: import modules directly and assert on pure functions (`fmt_coins`, `group_admin_link`, `format_user_profile`, keyboard builders) — that is what the smoke scripts do.

---

## ۱۰. فهرست فایل‌های کلیدی / Where to look first

| I want to… | Open |
| --- | --- |
| run every check at once | `venv\Scripts\python smoke\verify.py` (compile + import + all 11 smokes → 13 steps) |
| deploy (current: Render) | root `render.yaml`; `pip install -r requirements.txt` + `python bot.py`; env `RUN_MODE=webhook`, `WEBAPP_HOST=0.0.0.0`, `PORT` auto |
| deploy to Cloudflare Containers (optional) | files under `deploy/cloudflare/` (design in SKILL §2c); from the repo root: `npm install --prefix deploy/cloudflare` → `npm --prefix deploy/cloudflare run cf:check` / `cf:dev` / `cf:deploy` (or `npx wrangler … -c deploy/cloudflare/wrangler.jsonc`); needs Workers Paid + Docker daemon |
| local Cloudflare secrets | `deploy/cloudflare/.dev.vars.example` → copy to `.dev.vars` (git-ignored, read from the config dir by wrangler) |
| register/clear the Telegram webhook for a deployed Worker | `python deploy/cloudflare/set_webhook.py` (`--url`, `--print-curl`, `--remove`) |
| switch polling↔webhook / local no-SSL test | `RUN_MODE` + `WEBHOOK_*` in `.env`, `bot.py:run_webhook`, guide `WEBHOOK.md` |
| change a price/reward | `utils/economy.py` + `handlers/admin.py` (`cb_cost_*`, `cb_rewards*`) + `database/models.py` `BotPolicy` |
| gift coins / airdrop to users | `handlers/admin.py` §6 (`admin:gift*`, `_spawn_gift_notify`) + `keyboards/admin.py` (`admin_gift_*_kb`) + `utils/economy.py` (`add_coins` / `bulk_add_*`) |
| add/fix proxy support | `config.py:proxy_url`, `bot.py:build_session` / `_fallback_to_direct` / `log_connection_advice`, `test_connection.py` |
| change what a group sees | `handlers/group_lifecycle.py`, `keyboards/inline.py` (`group_onboarding_kb`) |
| change command help text | `utils/group_commands.py` (also feeds `set_my_commands`) |
| add an admin button | `keyboards/admin.py` + `handlers/admin.py` (`cb_*`, prefix `admin:`) |
| change matching/relay | `handlers/chat.py` |
| change نجوا flow | `handlers/whisper.py` (command) + `handlers/inline_anon.py` (inline) + `utils/target_lookup.py` |
| change force-join / required channels | `handlers/admin.py` section 7b (`admin:forcejoin*`, `_migrate_legacy_join_chat`) + `keyboards/admin.py` (`admin_forcejoin_kb`) + `utils/membership.py` |
| change middleware gating | `middleware/*` + registration order in `bot.py:87-117` |
| escape/format a profile | `utils/helpers.py::format_user_profile` |
