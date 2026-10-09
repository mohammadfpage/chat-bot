# Optional: Cloudflare Containers (not the current deployment)

The project's **current** deployment is an ordinary Python Web Service
(see the repository-root `render.yaml` and `README.md`). It runs
`python bot.py` directly and **never** executes Wrangler or Docker.

Everything in this directory is the *old* Cloudflare Containers setup, kept
aside so it can be restored later without rewriting it:

| File | Purpose |
| --- | --- |
| `Dockerfile` | image that runs the bot (`python bot.py`) inside a container |
| `.dockerignore` | keeps `.env`, `venv/`, `*.db*`, `backups/`, `node_modules/` out |
| `wrangler.jsonc` | Worker + container config (Durable Object, `max_instances: 1`) |
| `worker/index.js` | path gate that forwards only `POST WEBHOOK_PATH` + `GET /health` |
| `package.json` | `@cloudflare/containers` + `wrangler` (Node tooling) |

These files were moved out of the repository root so that a Python host
(Render, Railway, Fly, a VPS, …) cannot auto-detect a Node/Worker project and
run `npx wrangler deploy` by mistake.

## Restoring the Cloudflare option

Wrangler builds the container image with the **directory of `wrangler.jsonc`
as the build context**, but the image needs `requirements.txt` and the bot
source from the repository root. So this directory cannot build in place.
To deploy to Cloudflare again:

1. Copy these files back to the repository root (or configure your build to
   use the repository root as the Docker context).
2. Run the commands in the old README section (still preserved at the bottom
   of the root `README.md`): `npm install` → `npx wrangler login` →
   `npx wrangler secret put ...` → `npx wrangler deploy`.
3. Remember `max_instances` must stay `1` (matching state lives in process RAM)
   and the database must be managed PostgreSQL (the container disk is
   ephemeral).

Nothing in the Python runtime imports or depends on these files.
