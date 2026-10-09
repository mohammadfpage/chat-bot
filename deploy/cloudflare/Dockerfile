# OPTIONAL / NOT the current deployment path.
#
# Cloudflare Containers image for the anonymous-chat bot. The current
# deployment is an ordinary Python Web Service (Render) and never builds
# this image — see README.md. This file is kept here so the Cloudflare
# Containers option can be restored later without rewriting it.
#
# To actually build it, the Docker build context must contain requirements.txt
# and the bot source (i.e. the repository root). Wrangler uses the directory
# of its config file as the context, so `deploy/cloudflare/` alone cannot see
# them: copy the files back to the repo root (or point the build context there)
# before running `npx wrangler deploy`. See deploy/cloudflare/README.md.
#
# Constraints imposed by the platform:
#   * linux/amd64 only — the architecture Cloudflare runs
#   * no TLS inside: the Worker proxies plain HTTP to WEBAPP_PORT
#   * the disk is ephemeral — SQLite must never be used here (bot.py refuses
#     to boot with one); DATABASE_URL has to be managed PostgreSQL
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer survives every edit to the bot code.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The bot itself. .dockerignore keeps .env, the venv, SQLite files, backups
# and node_modules out — secrets arrive as container env vars from the
# Worker (worker/index.js copies vars + `wrangler secret put` values into
# the Durable Object's envVars).
COPY . .

# WEBAPP_PORT — the aiohttp webhook server binds WEBAPP_HOST (0.0.0.0).
EXPOSE 8080

# RUN_MODE=webhook comes from wrangler.jsonc vars; main() installs the
# SIGTERM handler first so a platform stop closes pools/WAL cleanly.
CMD ["python", "bot.py"]
