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

    database_url: str = "sqlite+aiosqlite:///database.db"

    # ── Parsed admin IDs (populated by model_validator) ──
    _parsed_admin_ids: list[int] = []

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

    @property
    def admin_ids_list(self) -> list[int]:
        """Return the parsed list of root admin Telegram IDs."""
        return self._parsed_admin_ids

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
