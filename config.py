"""
Configuration module using pydantic-settings.
All settings live in .env — no database tables needed.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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

    # ── Database ──
    database_url: str = "sqlite+aiosqlite:///database.db"

    # ── Parsed admin IDs (populated by model_validator) ──
    _parsed_admin_ids: list[int] = []

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
            self._parsed_admin_ids = [
                int(x.strip())
                for x in cleaned.split(",")
                if x.strip().isdigit()
            ]
        else:
            self._parsed_admin_ids = []
        return self

    @property
    def admin_ids_list(self) -> list[int]:
        """Return the parsed list of root admin Telegram IDs."""
        return self._parsed_admin_ids


settings = Settings()
