# Optional: Cloudflare Containers (not the current deployment)

The project's **current** deployment is an ordinary Python Web Service
(see the repository-root `render.yaml` and `README.md`). It runs
`python bot.py` directly and **never** executes Wrangler or Docker.

Everything in this directory is an **optional** Cloudflare Containers setup,
kept aside so it can be restored without rewriting it — and, since the
`image_build_context` fix below, deployable **in place from the repo root**.

| File | Purpose |
| --- | --- |
| `Dockerfile` | image that runs the bot (`python bot.py`) inside a container |
| `.dockerignore` | ignores for the *legacy* in-place context (effective ignore is now the repo-root `.dockerignore`) |
| `wrangler.jsonc` | Worker + container config (Durable Object, `max_instances: 1`, `image_build_context: "../.."`) |
| `worker/index.js` | path gate that forwards only `POST WEBHOOK_PATH` + `GET /health` |
| `package.json` | `@cloudflare/containers` + `wrangler` + `cf:*` npm scripts |
| `.dev.vars.example` | template for local-dev secrets (`wrangler dev`) |
| `set_webhook.py` | registers the deployed Worker URL with Telegram |

These files live outside the repository root so that a Python host (Render,
Railway, Fly, a VPS, …) cannot auto-detect a Node/Worker project and run
`npx wrangler deploy` by mistake.

---

## ⚠️ Free plan is not enough

Cloudflare's own pricing page (checked August 2026) lists Containers as
**Workers Paid only** (`Free: N/A`, `$5/month` minimum, with included CPU/
memory/egress). A **Workers Free** account can host Workers and SQLite-backed
Durable Objects, but **not** the container this bot runs in. For a free
deployment use the Render path (root `README.md` §3) or a server + Cloudflare
Tunnel (`WEBHOOK.md` §3). See `WEBHOOK.md` §3 for the full research.

The rest of this file is about the (paid) Containers path.

---

## How to run it from the repo root

Every command works from the repository root; `-c` points Wrangler at this
config file. **Relative paths inside `wrangler.jsonc` — `main`, `image`,
`image_build_context` — resolve against the config file's directory, never
the shell's cwd**, so these behave exactly like running Wrangler from inside
`deploy/cloudflare/`.

```powershell
# one-time: tooling installed HERE, never in the repo root
npm install --prefix deploy/cloudflare

# a) local development / emulation (needs a running Docker daemon)
npx wrangler dev -c deploy/cloudflare/wrangler.jsonc

# b) secrets (production). Run once per key; re-put + redeploy to rotate.
npx wrangler secret put BOT_TOKEN      -c deploy/cloudflare/wrangler.jsonc
npx wrangler secret put DATABASE_URL   -c deploy/cloudflare/wrangler.jsonc
npx wrangler secret put WEBHOOK_SECRET -c deploy/cloudflare/wrangler.jsonc
npx wrangler secret put REDIS_URL      -c deploy/cloudflare/wrangler.jsonc
npx wrangler secret put PROXY_URL      -c deploy/cloudflare/wrangler.jsonc

# c) production deploy (builds the image with repo-root context + pushes)
npx wrangler deploy -c deploy/cloudflare/wrangler.jsonc

# d) live logs
npx wrangler tail -c deploy/cloudflare/wrangler.jsonc
```

### npm-script shortcuts (no `-c` needed)

`npm run` executes scripts with the package directory as cwd, so the config
is found automatically. From the repo root:

```powershell
npm --prefix deploy/cloudflare run cf:dev       # = wrangler dev
npm --prefix deploy/cloudflare run cf:deploy    # = wrangler deploy
npm --prefix deploy/cloudflare run cf:tail      # = wrangler tail
npm --prefix deploy/cloudflare run cf:secret -- BOT_TOKEN
npm --prefix deploy/cloudflare run cf:check     # offline dry-run validation
```

### Why it can build in place now

`Dockerfile` needs `requirements.txt` and the bot source, which live at the
**repository root**, not here. Wrangler's default build context is the
Dockerfile's directory, which is why the old instructions said "copy the
files back to the root". `wrangler.jsonc` now sets:

```jsonc
"containers": [
  { "class_name": "BotContainer",
    "image": "./Dockerfile",
    "image_build_context": "../..",   // ← repo root, relative to THIS file
    "max_instances": 1 }
]
```

Because the context becomes the repository root, Docker reads the **root
`.dockerignore`** (not this folder's), which keeps `.env`, `.dev.vars`,
`venv/`, `*.db*`, `backups/`, `logs/` and `node_modules/` out of the image.

---

## Local dev (`wrangler dev`)

1. Copy the template and fill in real values:
   ```powershell
   Copy-Item deploy/cloudflare/.dev.vars.example deploy/cloudflare/.dev.vars
   ```
   `.dev.vars` is git-ignored. Wrangler loads it from the **config
   directory**, so it is picked up both from the root (`-c …`) and from
   inside this folder.
2. `wrangler dev` needs a running **Docker daemon** (it builds and runs the
   container locally).
3. Webhook mode needs a public https URL. Either run a tunnel and put its
   address in `WEBHOOK_BASE_URL`:
   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8080
   ```
   …or, to test without any public URL, set `RUN_MODE=polling` in `.dev.vars`
   (needs `PROXY_URL` where `api.telegram.org` is filtered).
4. Locally, `DATABASE_URL` may stay SQLite (`sqlite+aiosqlite:///dev_local.db`).
   In a real container the disk is ephemeral, so the bot **refuses to boot**
   with SQLite — production must use managed PostgreSQL.

---

## Deploy checklist

1. Ensure the account is on **Workers Paid**.
2. `npm install --prefix deploy/cloudflare`
3. `npx wrangler login`
4. Create a managed PostgreSQL and set it:
   `npx wrangler secret put DATABASE_URL -c deploy/cloudflare/wrangler.jsonc`
5. Put the other secrets (`BOT_TOKEN`, `WEBHOOK_SECRET`, optionally
   `REDIS_URL`, `PROXY_URL`).
6. Edit `ADMIN_IDS` in `wrangler.jsonc` (`vars`), then:
   `npx wrangler deploy -c deploy/cloudflare/wrangler.jsonc`
7. Copy the printed `https://<name>.<account>.workers.dev` URL into
   `WEBHOOK_BASE_URL` in `wrangler.jsonc`, then deploy again.
8. Health: `curl https://<name>.<account>.workers.dev/health` → `{"status":"ok"}`
9. Register with Telegram:
   `python deploy/cloudflare/set_webhook.py --url https://<name>.<account>.workers.dev`

## End-to-end checks (offline)

```powershell
# config is valid, Worker bundles, container is discovered — no deploy
npm --prefix deploy/cloudflare run cf:check

# show the exact setWebhook call without contacting Telegram
python deploy/cloudflare/set_webhook.py --print-curl --url https://example.workers.dev

# the whole project's smoke suite (includes the Cloudflare wiring checks)
venv\Scripts\python smoke\verify.py
```

## Invariants (do not change)

* `max_instances` stays **1** — matching state (`pair_map`/`search_queue`) is
  per-process RAM; two containers would be two disjoint worlds.
* No SQLite in a real container — the boot guard refuses it (ephemeral disk).
* Secrets only via `wrangler secret put` (never `vars`, never committed).
* `worker/index.js` must wake the container **only** for
  `POST WEBHOOK_PATH` / `GET /health`.
* `chat_pairs` writes stay on the `_persist_*` / `leave_search_queue` helpers.
