"""
Configuration module using pydantic-settings.
All settings live in .env — no database tables needed.
"""

import logging
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The project directory — settings must not depend on the CWD the process
# happens to be started from (systemd, a scheduler, or a shell in another
# folder would otherwise look for .env / database.db in the wrong place).
BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Bot ──
    bot_token: str = ""

    # ── Admins (Root / Super Admins) ──
    # Stored as raw comma-separated string from .env (e.g. "7174138646,123456789").
    # The @model_validator below parses it into ``admin_ids`` (list[int]).
    admin_ids: str = ""

    # ── Force-Join Channel ──
    channel_id: int = 0
    channel_username: str = ""

    # ── Inline Mode (Anonymous Messaging) ──
    # Public HTTPS URL of the icon shown as the thumbnail of the
    # InlineQueryResultArticle. The Bot API only accepts a *URL* here (a
    # Telegram file_id is NOT valid), so it must be a real hosted image.
    # Leave empty to send the result without a thumbnail.
    inline_thumbnail_url: str = ""

    # ── Inline menu thumbnails (the 3 options shown for a bare "@bot") ──
    # One URL per option, so each row of the picker is told apart by its icon
    # before the text is even read. Same hard requirements as
    # ``inline_thumbnail_url``: a PUBLIC https image URL (a Telegram file_id is
    # NOT valid), no more than 1 KB, at most 200x200 pixels, and width/height
    # must be declared by Telegram reading the file itself.
    #
    # Each falls back to ``inline_thumbnail_url`` when left empty, and to no
    # thumbnail at all when that is empty too — Telegram simply renders the row
    # without one, which is valid.
    inline_tutorial_thumb: str = ""   # آموزش ارسال نجوا  → green question icon
    inline_whisper_thumb: str = ""    # درخواست نجوا     → purple id icon
    inline_anon_chat_thumb: str = ""  # پیام ناشناس      → pink mail icon

    # ── Tutorial media ──
    # Optional image shown by the «🖼️ عکس آموزشی» button of the tutorial card.
    # When empty the button becomes a callback that expands the card into a
    # written step-by-step guide instead — see ``handlers/inline_menu.py``.
    tutorial_photo_url: str = ""

    # ── Group keyboard cleanup (on startup) ──
    # Telegram caches the last reply keyboard PER CHAT, and re-adding the bot
    # does not flush it — only a fresh ReplyKeyboardRemove does. These two
    # switches control the startup sweep that pushes that removal into every
    # group the bot has ever posted a whisper card in.
    #   group_keyboard_cleanup:  set to "false" to skip the sweep entirely
    #   group_keyboard_cleanup_delay: seconds to wait before deleting the
    #                           invisible cleanup message (give clients time
    #                           to apply the removal before it vanishes)
    group_keyboard_cleanup: str = "true"
    group_keyboard_cleanup_delay: float = 2.0

    # ── Database ──
    # -- Proxy (optional) --
    # The single escape hatch for regions where api.telegram.org is filtered.
    # Left empty the bot connects directly, exactly as before.
    #
    #   PROXY_URL=socks5://127.0.0.1:1080        SOCKS5 (also socks4/socks5h)
    #   PROXY_URL=socks5://user:pass@host:1080   with authentication
    #   PROXY_URL=http://127.0.0.1:8080          plain HTTP proxy
    #
    # Both forms are handled by aiogram's AiohttpSession(proxy=...) through
    # the aiohttp-socks package. The value is read once at startup; if the
    # proxy cannot be built, or never connects, the bot falls back to a
    # direct connection and says so -- see bot.py:build_session.
    proxy_url: str = ""

    # ── Run mode ──
    # RUN_MODE=polling (default) — long polling, works with zero network
    # setup; this is what you want on a laptop and behind a VPN.
    # RUN_MODE=webhook         — runs a local aiohttp server and registers it
    # with Telegram via setWebhook. Requires WEBHOOK_BASE_URL (a public
    # https:// address — see WEBHOOK.md for the no-SSL local test with
    # `cloudflared tunnel`).
    run_mode: str = "polling"

    # ── Webhook (used only when RUN_MODE=webhook) ──
    # WEBHOOK_BASE_URL — the PUBLIC https:// origin Telegram will POST to.
    #   Local test : https://random-word-words.trycloudflare.com (cloudflared)
    #   Server     : https://bot.example.com  (a real domain + TLS)
    #   Telegram REJECTS http:// — the local server itself speaks plain
    #   HTTP; the tunnel/reverse proxy is what terminates TLS.
    # WEBHOOK_PATH   — the route the server listens on; joined to the base
    #                  URL. A leading "/" is added automatically.
    # WEBHOOK_SECRET — sent as X-Telegram-Bot-Api-Secret-Token on every
    #                  Telegram request and verified before any handler
    #                  runs. Characters A-Z a-z 0-9 _ - only (1-256).
    #                  Leave empty to auto-generate a fresh one each boot
    #                  (it is re-registered with Telegram on every start,
    #                  so a rotating secret stays valid).
    # WEBAPP_HOST/PORT — the local bind address. 127.0.0.1 is right for a
    #                  tunnel or an nginx/caddy reverse proxy on the same
    #                  machine; use 0.0.0.0 only when something else on the
    #                  network must reach the port directly. Note Telegram
    #                  itself only connects to ports 443/80/88/8443 — a
    #                  tunnel or proxy hides any other local port.
    # PORT          — injected by managed hosts (Render, Railway, Fly, …).
    #                  When present it OVERRIDES WEBAPP_PORT so the process
    #                  binds the port the platform routes to; local dev never
    #                  sets it, so the WEBAPP_* defaults still apply there.
    webhook_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""
    webapp_host: str = "127.0.0.1"
    webapp_port: int = 8080
    port: str = ""

    # ── Logging ──
    # LOG_LEVEL=DEBUG|INFO|WARNING|ERROR — console verbosity.
    # LOG_FILE=logs/bot.log — rotating file sink (1 MB x 5); empty disables it.
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"

    # ── Retention (days; 0 = keep forever) ──
    # Swept once a day by utils.retention, started at boot. Private message
    # bodies should not outlive the conversations they belong to; the ledger
    # stays far longer because it is the economy's audit trail.
    retention_whisper_days: int = 30
    retention_anon_message_days: int = 60
    retention_anon_session_days: int = 7
    retention_ledger_days: int = 365
    # Block reports are moderation history, not conversation content — kept
    # long enough to investigate a dispute, then swept like everything else.
    retention_report_days: int = 180

    # ── Backups ──
    # How many database-*.db snapshots to keep under backups/ — both
    # scripts/backup_db.py (cron/systemd) and the admin panel's «پشتیبان‌گیری»
    # rotate against this number.
    backup_keep: int = 14

    # ── Database ──
    # Local/dev default is SQLite. For Cloudflare Containers set
    # DATABASE_URL to a managed PostgreSQL instance, e.g.
    #   postgresql+asyncpg://user:pass@host:5432/dbname
    # ``postgres://…`` and ``postgresql://…`` are auto-upgraded to the
    # ``postgresql+asyncpg://`` form by the validator below — the asyncpg
    # driver is the only one this codebase can actually use (no psycopg).
    database_url: str = "sqlite+aiosqlite:///database.db"

    # ── FSM storage (used only by bot.py) ──
    # Empty = aiogram MemoryStorage (state dies with the process — fine for
    # local polling, but it also means a Render/Cloudflare sleep WIPES any
    # half-finished profile wizard, which is why production MUST set this).
    # Set to a Redis URL to keep FSM states across restarts/sleeps:
    #   Upstash (TLS REQUIRED):  REDIS_URL=rediss://default:<password>@<region>.upstash.io:<port>
    #   plain Redis:             REDIS_URL=redis://default:<password>@<host>:6379/0
    # NB: Upstash only speaks TLS — a plain redis:// URL fails the handshake
    # and every state read dies, so use the rediss:// endpoint it gives you.
    # Read by build_fsm_storage() and pinged once by verify_fsm_storage();
    # an unreachable/broken URL falls back to MemoryStorage with a logged
    # ERROR instead of failing startup.
    redis_url: str = ""

    # ── Cloudflare ──
    # Injected automatically by the platform (Cloudflare deploys set
    # CLOUDFLARE_DEPLOYMENT_ID in the container environment); never set it
    # by hand. Used to enable container-only safety rails, e.g. refusing to
    # boot with a SQLite database that would be wiped when the container
    # sleeps. Read through a field (not os.environ) so the value is
    # testable and shows up in Settings reprs.
    cloudflare_deployment_id: str = ""

    # ── Parsed admin IDs (populated by model_validator) ──
    _parsed_admin_ids: list[int] = []

    @model_validator(mode="after")
    def _normalize_database_url(self) -> "Settings":
        """Upgrade bare PostgreSQL schemes to the asyncpg driver.

        SQLAlchemy resolves ``postgresql://`` to psycopg2, which this project
        does not ship, so a pasted Heroku/Neon/Supabase URL would explode at
        the first connection with "can't load plugin: psycopg2". Rewriting to
        ``postgresql+asyncpg://`` makes the common paste-and-go work; an
        explicitly typed driver (``postgresql+anything://``) is left alone.
        Runs before the SQLite absolutizer so the two never interact.
        """
        url = (self.database_url or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        self.database_url = url
        return self

    @model_validator(mode="after")
    def _absolutize_sqlite_url(self) -> "Settings":
        """Rewrite a relative sqlite URL to an absolute path under BASE_DIR.

        ``sqlite+aiosqlite:///database.db`` is resolved against the CWD by
        SQLAlchemy, so starting the bot from another directory would silently
        create (and then never find) a second empty database. Anchoring it
        here means the engine and the migration code always see one file.
        """
        prefix = "sqlite+aiosqlite:///"
        url = self.database_url
        if url.startswith(prefix):
            path = url[len(prefix):]
            if path and path != ":memory:" and not Path(path).is_absolute():
                self.database_url = prefix + str((BASE_DIR / path).resolve())
        return self

    @model_validator(mode="after")
    def _parse_admin_ids(self) -> "Settings":
        """Parse the comma-separated ``admin_ids`` string into ``list[int]``.

        This prevents Pydantic v2 validation errors that occur when
        ``admin_ids`` is typed as ``list[int]`` directly — pydantic-settings
        would try to JSON-parse the raw env string before any field_validator
        runs, and a comma-separated value like ``"7174138646,123456789"`` is
        not valid JSON.
        """
        raw = self.admin_ids
        if isinstance(raw, list):
            self._parsed_admin_ids = [int(x) for x in raw]
        elif isinstance(raw, str) and raw.strip():
            cleaned = raw.strip().strip("[]")
            parsed: list[int] = []
            for part in cleaned.split(","):
                part = part.strip()
                if not part:
                    continue
                if part.isdigit():
                    parsed.append(int(part))
                else:
                    # A silently dropped entry means an admin logs in and has
                    # no panel — say so at boot instead of debugging it later.
                    logger.warning(
                        "ADMIN_IDS: ignoring non-numeric entry %r", part
                    )
            self._parsed_admin_ids = parsed
        else:
            self._parsed_admin_ids = []
        if not self._parsed_admin_ids:
            logger.warning("ADMIN_IDS is empty — no root admin configured.")
        return self

    @model_validator(mode="after")
    def _normalize_run_mode(self) -> "Settings":
        """Normalise the run-mode switch and the webhook URL pieces.

        Done here so every consumer (bot.py, smokes) sees one canonical
        shape: ``run_mode`` is exactly ``"polling"`` or ``"webhook"``,
        ``webhook_path`` always starts with ``/`` and ``webhook_base_url``
        never ends with one. An unknown ``RUN_MODE`` falls back to polling
        with a warning — a typo in .env must never stop a bot that would
        otherwise run fine.
        """
        mode = (self.run_mode or "").strip().lower()
        if mode in {"webhook", "hook"}:
            self.run_mode = "webhook"
        elif mode in {"", "polling", "poll"}:
            self.run_mode = "polling"
        else:
            logger.warning(
                "RUN_MODE=%r is not polling/webhook — falling back to polling.",
                self.run_mode,
            )
            self.run_mode = "polling"

        path = (self.webhook_path or "").strip() or "/telegram/webhook"
        self.webhook_path = path if path.startswith("/") else f"/{path}"
        self.webhook_base_url = (self.webhook_base_url or "").strip().rstrip("/")
        self.webhook_secret = (self.webhook_secret or "").strip()
        return self

    @model_validator(mode="after")
    def _apply_platform_port(self) -> "Settings":
        """Let a managed host's ``PORT`` drive the webhook bind port.

        Render (and most PaaS hosts) assign a port at runtime via ``PORT`` and
        route public traffic to it — the process must bind exactly that port or
        the health check fails. ``WEBAPP_PORT`` stays the local-development
        default; an explicit ``PORT`` simply wins.

        When ``PORT`` is present and the operator did NOT set ``WEBAPP_HOST``
        explicitly, the bind address is widened to ``0.0.0.0``: a platform
        port that only listens on ``127.0.0.1`` is unreachable from the host's
        router. An explicit ``WEBAPP_HOST`` (e.g. the tunnel default
        ``127.0.0.1``) is always respected.

        A non-numeric ``PORT`` is ignored with a warning instead of crashing —
        a stray export must never stop the bot from booting.
        """
        raw = (self.port or "").strip()
        if not raw:
            return self
        if not raw.isdigit():
            logger.warning(
                "PORT=%r is not a number — ignoring it and using WEBAPP_PORT.",
                self.port,
            )
            return self
        self.webapp_port = int(raw)
        if "webapp_host" not in self.model_fields_set:
            self.webapp_host = "0.0.0.0"
        return self

    @property
    def admin_ids_list(self) -> list[int]:
        """Return the parsed list of root admin Telegram IDs."""
        return self._parsed_admin_ids

    @property
    def database_dialect(self) -> str:
        """``"postgresql"`` / ``"sqlite"`` / the raw scheme for anything else.

        Computed from the NORMALIZED ``database_url`` (after the validator
        above), so ``postgres://…`` — the form a managed-hosting console hands
        out — already reports ``postgresql``. Branches that must behave
        differently per backend (engine connect options, backup guards,
        container safety rails) ask this instead of re-implementing string
        matching, which is how the config and the engine once drifted apart.
        """
        url = self.database_url
        if url.startswith("postgres"):
            return "postgresql"
        if url.startswith("sqlite"):
            return "sqlite"
        scheme = url.split("://", 1)[0]
        return scheme.split("+", 1)[0] or "unknown"

    @property
    def in_cloudflare_container(self) -> bool:
        """Whether the process believes it runs inside a Cloudflare Container.

        True only when the platform injected CLOUDFLARE_DEPLOYMENT_ID — a
        laptop can never set it accidentally by exporting a stray variable,
        because the smoke tests and local runs document it as read-only.
        """
        return bool((self.cloudflare_deployment_id or "").strip())

    @property
    def group_cleanup_enabled(self) -> bool:
        """Whether the on-startup group keyboard sweep should run.

        Kept as a plain string in .env (like ``admin_ids``) so a stray value
        can never crash startup: anything other than an explicit false-ish
        value means "on".
        """
        return self.group_keyboard_cleanup.strip().lower() not in {
            "false", "0", "no", "off", "",
        }

    @property
    def webhook_mode(self) -> bool:
        """Whether the bot should serve updates over webhook (vs polling)."""
        return self.run_mode == "webhook"

    def thumbnail_url(self, which: str) -> str:
        """Resolve one inline-menu icon, falling back to the generic one.

        ``which`` is the name of the setting to read. Returning an empty string
        is a valid answer — the Bot API simply renders the picker row without a
        thumbnail — so callers never have to special-case "unconfigured".
        """
        specific = {
            "tutorial": self.inline_tutorial_thumb,
            "whisper": self.inline_whisper_thumb,
            "anon_chat": self.inline_anon_chat_thumb,
        }.get(which, "")
        return (specific or self.inline_thumbnail_url).strip()


settings = Settings()
