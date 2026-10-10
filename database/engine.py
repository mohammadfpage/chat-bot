"""
Async SQLAlchemy engine and session factory.

The backend is chosen entirely by ``settings.database_url``: SQLite for
local dev (aiosqlite), PostgreSQL for the Cloudflare container (asyncpg).
``engine_options()`` is the single place that knows which connect arguments
each backend needs — database.engine, the smokes and the migration guard
all derive their behaviour from it instead of re-checking the URL.
"""

import logging
import re

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings
from database.models import Base

logger = logging.getLogger(__name__)

# ── Identifier guard for the raw migration SQL ─────────────
# The ORM covers every query that touches user input; the only raw statements
# here are the startup migrations, which interpolate table/column names from
# the dicts below. Validating them anyway means a future edit that accidentally
# routes ANY value through f-string SQL fails loudly instead of executing.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _ident(name: str) -> str:
    """Return ``name`` if it is a plain SQL identifier, else raise."""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name!r}")
    return name

# ── Engine options per backend ────────────────────────────
def engine_options(url: str) -> dict:
    """Return ``create_async_engine`` kwargs for the backend in ``url``.

    Kept a pure function of the URL so tests (and ``smoke_cloudflare``) can
    assert on the exact kwargs without instantiating an engine.

    SQLite
      ``timeout=30`` — aiosqlite waits for a locked file instead of failing
      instantly: the long-polling loop plus background tasks briefly overlap
      writers, and "database is locked" must never reach a user.

    PostgreSQL
      ``server_settings={"timezone": "UTC"}`` — ``func.now()`` inside a
      transaction takes the session timezone; pinning it to UTC keeps that
      value aligned with the naive-UTC timestamps Python writes with
      ``datetime.now(timezone.utc).replace(tzinfo=None)`` (every
      ``created_at`` in models.py), so retention cutoffs and Telegram-time
      displays agree across the two writers.
      The pool is sized for a container that may serve handlers, background
      sweeps and the rebuild-on-wake at once; ``pool_recycle`` rotates
      connections before typical idle-kill policies do.
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 30}}
    if url.startswith("postgres"):
        return {
            "connect_args": {"server_settings": {"timezone": "UTC"}},
            "pool_size": 10,
            "max_overflow": 20,
            "pool_recycle": 1800,
        }
    return {}

# ── Engine ──────────────────────────────────────────────
# pool_pre_ping verifies connections before use (safe for all dialects).
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    **engine_options(settings.database_url),
)


if settings.database_dialect == "sqlite":
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        """Per-connection SQLite hardening.

        WAL lets readers proceed while a writer holds the lock (the bot
        queries from handlers while a background task writes); NORMAL
        synchronous is the WAL-recommended balance of safety vs speed;
        busy_timeout makes a contended writer retry instead of raising
        ``database is locked`` at the user.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

# ── Session factory ─────────────────────────────────────
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables on startup.

    Also performs a lightweight migration for SQLite: adds newly-introduced
    columns to tables that were created by an older version of the schema, and
    drops the ones the current schema no longer has.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ── Lightweight migration (SQLite) ──
        dialect = engine.dialect.name
        if dialect == "sqlite":
            # Column migrations per table: name -> SQL type/statement
            migrations = {
                "users": {
                    "is_admin": "BOOLEAN DEFAULT 0",
                    "is_banned": "BOOLEAN DEFAULT 0",
                    # REAL so a fractional balance (a 0.5-priced service) is
                    # representable. On an existing table this dict entry is
                    # skipped — SQLite's INTEGER affinity still stores any
                    # non-losslessly-convertible value such as 2.5 as REAL, so
                    # old databases round-trip fractional coins without a
                    # rebuild.
                    "coins": "REAL DEFAULT 0",
                    "last_daily_bonus": "DATETIME",
                    "referred_by": "BIGINT",
                    "referral_count": "INTEGER DEFAULT 0",
                    "is_vip": "BOOLEAN DEFAULT 0",
                    "premium_until": "DATETIME",
                    "show_profile_photo": "BOOLEAN DEFAULT 0",
                    "profile_photo": "VARCHAR(512)",
                    # Profile fields. They predate the gender column in most
                    # databases, but a database created before the wizard
                    # existed lacks them too — and without the columns the
                    # wizard's final commit raises and nothing saves.
                    "age": "INTEGER",
                    "city": "VARCHAR(128)",
                    "height": "VARCHAR(32)",
                    "is_profile_complete": "BOOLEAN DEFAULT 0",
                    # Matching is charged, not whitelisted by a counter. Both
                    # flags default to 0, which is the safe direction: nobody
                    # becomes free of charge because a column appeared.
                    "has_subscription": "BOOLEAN DEFAULT 0",
                    "is_exempt": "BOOLEAN DEFAULT 0",
                    # Needed by «چت با دختر» / «چت با پسر»: a user who has not
                    # declared a gender cannot be matched by either of them, so
                    # NULL here means "profile not finished" from the matcher's
                    # point of view.
                    "gender": "VARCHAR(8)",
                },
                "bot_policy": {
                    "daily_bonus_coins": "INTEGER DEFAULT 25",
                    "referral_invitee_coins": "INTEGER DEFAULT 15",
                    "chat_lifetime_hours": "INTEGER DEFAULT 24",
                    # Prices are REAL: only the four *costs* may be fractional.
                    # The reward columns above stay INTEGER on purpose — they
                    # are counters the bot hands out whole.
                    "chat_girl_cost": "REAL DEFAULT 1",
                    "chat_boy_cost": "REAL DEFAULT 1",
                    # Per-service pricing, admin-tunable from the panel. The
                    # defaults preserve today's behaviour exactly: random
                    # connect has always been free, a whisper has always cost 1.
                    "random_chat_cost": "REAL DEFAULT 0",
                    "whisper_cost": "REAL DEFAULT 1",
                    # The widened anti-spam caps (old defaults: 6/min, 40/h).
                    # These MUST be in the migration dict: the default-seeding
                    # UPDATEs below run unconditionally, and on a database
                    # created before the columns existed that was an instant
                    # OperationalError → crash loop on every startup.
                    "messages_per_minute": "INTEGER DEFAULT 20",
                    "messages_per_hour": "INTEGER DEFAULT 300",
                },
                # The snooper ledger. SQLite has no JSON type of its own and
                # SQLAlchemy's JSON is stored as TEXT, so the column is added as
                # TEXT seeded with an empty JSON array: every row that exists
                # right now would otherwise come back as NULL, which is not a
                # list, and the first reader to be recorded would crash on it.
                "whispers": {
                    "snoopers": "TEXT NOT NULL DEFAULT '[]'",
                    # The private «یک پیام مخفی دارید» notice — a FALLBACK,
                    # sent only when the card could not be posted to the group and
                    # this row would otherwise have no route to its reader. The
                    # happy path sends nothing privately. NULL on every existing
                    # row, which is correct: those whispers went out as group
                    # cards, so there is no message to edit to «خوانده شد» and the
                    # read-receipt on the card still happens.
                    "notice_message_id": "INTEGER",
                },
                # ``target_viewed`` gates the card's action buttons: an
                # inline whisper carries ONLY the read button until the person
                # it was written for opens it.
                #
                # No ``notice_message_id`` here, deliberately: Telegram publishes
                # inline cards itself, so there is no failed send to fall back
                # from and nothing to keep a notice id for.
                "inline_whispers": {
                    "snoopers": "TEXT NOT NULL DEFAULT '[]'",
                    "target_viewed": "BOOLEAN NOT NULL DEFAULT 0",
                },
            }
            migrated: set[tuple[str, str]] = set()
            for table, columns in migrations.items():
                table = _ident(table)
                result = await conn.execute(text(f"PRAGMA table_info({table})"))
                existing = {row[1] for row in result.fetchall()}
                for col, ddl in columns.items():
                    col = _ident(col)
                    if col not in existing:
                        if table == "bot_policy":
                            # The singleton row is created lazily by get_policy();
                            # make sure a row exists before ALTER can add columns.
                            row = await conn.execute(
                                text("SELECT COUNT(*) FROM bot_policy")
                            )
                            if (row.scalar() or 0) == 0:
                                await conn.execute(
                                    text(
                                        "INSERT INTO bot_policy (id) VALUES (1)"
                                    )
                                )
                        await conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        )
                        migrated.add((table, col))

            # ── Seed ``inline_whispers.target_viewed`` on existing rows ──
            # The new column defaults to 0, which is correct going forward but
            # wrong for everything already in the table: a whisper the recipient
            # HAS opened shows its action buttons in the group right now, and
            # back to 0 they would lose them the next time either party re-taps
            # the read button. ``is_viewed`` is the closest signal an old row
            # carries (either party may set it), so it is used as the seed: the
            # worst a wrong guess does is leave a card showing the buttons it
            # is already showing today.
            if ("inline_whispers", "target_viewed") in migrated:
                await conn.execute(
                    text("UPDATE inline_whispers SET target_viewed = 1 "
                         "WHERE is_viewed = 1")
                )

            # ── Drop the "tokens" economy ──
            #
            # Coins are the only currency now: tokens are charged per message,
            # which is exactly the per-message cost the new model removes. The
            # ORM no longer maps these columns, so leaving them would create a
            # table whose real shape disagrees with the code — the same drift
            # that made ``user.tokens`` raise AttributeError.
            #
            # The coins already in a user's balance are the whole of what they
            # ever actually spent (they could also spend tokens), so nothing is
            # converted or refunded here: the token columns held a quota that is
            # simply no longer consumed. Dropping them is irreversible, which is
            # why it is logged rather than done silently.
            dropped_columns = {
                "users": ["tokens", "last_token_reset"],
                "bot_policy": [
                    "tokens_per_day",
                    "token_cost_per_message",
                    "referral_invitee_tokens",
                    "coin_to_token_rate",
                ],
            }
            for table, columns in dropped_columns.items():
                table = _ident(table)
                result = await conn.execute(text(f"PRAGMA table_info({table})"))
                existing = {row[1] for row in result.fetchall()}
                for col in columns:
                    col = _ident(col)
                    if col in existing:
                        await conn.execute(
                            text(f"ALTER TABLE {table} DROP COLUMN {col}")
                        )
                        logger.info(
                            "Dropped obsolete column %s.%s (tokens era ended)",
                            table,
                            col,
                        )

            # ── Widen the anti-spam defaults on the singleton policy row ──
            # Only overwrites values still holding the OLD defaults, so an admin
            # who already tuned a number is never clobbered. The old caps (6 per
            # minute) were sized for a per-message economy; with matching as the
            # only charge, 6/min reads as the bot being broken.
            await conn.execute(
                text("UPDATE bot_policy SET messages_per_minute=20 "
                     "WHERE messages_per_minute=6")
            )
            await conn.execute(
                text("UPDATE bot_policy SET messages_per_hour=300 "
                     "WHERE messages_per_hour=40")
            )
            await conn.execute(
                text("UPDATE bot_policy SET daily_bonus_coins=25 "
                     "WHERE daily_bonus_coins IS NULL OR daily_bonus_coins=0")
            )
            await conn.execute(
                text("UPDATE bot_policy SET referral_invitee_coins=15 "
                     "WHERE referral_invitee_coins IS NULL "
                     "OR referral_invitee_coins=0")
            )

            # Migrate anonymous_messages table if needed
            result = await conn.execute(text("PRAGMA table_info(anonymous_messages)"))
            anon_columns = {row[1] for row in result.fetchall()}
            
            # Check if old schema exists (owner_id/guest_id) vs new (sender_id/receiver_id)
            if "owner_id" in anon_columns and "sender_id" not in anon_columns:
                # Drop and recreate with new schema
                await conn.execute(text("DROP TABLE anonymous_messages"))
                await conn.run_sync(Base.metadata.create_all)
            elif "sender_id" not in anon_columns:
                # Table doesn't exist or is malformed, create_all handles it
                pass
            
            # Add is_owner_replying if missing
            result = await conn.execute(text("PRAGMA table_info(anonymous_messages)"))
            anon_columns = {row[1] for row in result.fetchall()}
            if "is_owner_replying" not in anon_columns:
                await conn.execute(text("ALTER TABLE anonymous_messages ADD COLUMN is_owner_replying BOOLEAN DEFAULT 0"))

        # ── Ensure the critical columns exist on EVERY backend ──
        #
        # ``Base.metadata.create_all`` only ever issues CREATE TABLE; it NEVER
        # ALTERs a table that already exists. On a managed PostgreSQL (Neon,
        # Supabase, …) whose ``users`` table was created by an older build, a
        # column added to the model afterwards — ``gender``,
        # ``is_profile_complete``, ``age``, ``city`` … — is therefore simply
        # ABSENT, and because the ORM reads and writes every mapped column, the
        # breakage is not confined to that one field: loading or saving a
        # profile raises ``UndefinedColumn`` and NOTHING is persisted. The
        # migration block above runs only ``if dialect == "sqlite"``, so
        # PostgreSQL never healed itself — which is exactly the
        # "some things just do not save on Neon" symptom.
        #
        # This step runs on both backends, reflects the live columns and adds
        # whatever is missing with portable DDL (every type below is valid on
        # SQLite AND PostgreSQL). It is idempotent: existing columns are
        # skipped, so it is safe on every restart.
        critical_columns = {
            "users": {
                "age": "INTEGER",
                "city": "VARCHAR(128)",
                "height": "VARCHAR(32)",
                "gender": "VARCHAR(8)",
                "is_profile_complete": "BOOLEAN DEFAULT FALSE",
                "show_profile_photo": "BOOLEAN DEFAULT FALSE",
                "profile_photo": "VARCHAR(512)",
                "is_admin": "BOOLEAN DEFAULT FALSE",
                "is_banned": "BOOLEAN DEFAULT FALSE",
                "coins": "DOUBLE PRECISION DEFAULT 0",
                "has_subscription": "BOOLEAN DEFAULT FALSE",
                "is_exempt": "BOOLEAN DEFAULT FALSE",
                "is_vip": "BOOLEAN DEFAULT FALSE",
                "last_daily_bonus": "TIMESTAMP",
                "premium_until": "TIMESTAMP",
                "referred_by": "BIGINT",
                "referral_count": "INTEGER DEFAULT 0",
                # Any mapped column that is absent breaks the ORM's SELECT as
                # a whole, so the metadata timestamp is healed too. The default
                # is a CONSTANT literal on purpose: SQLite refuses
                # ``ADD COLUMN … DEFAULT CURRENT_TIMESTAMP`` ("non-constant
                # default"), while a quoted timestamp is accepted by both
                # SQLite and PostgreSQL.
                "registered_at": "TIMESTAMP DEFAULT '1970-01-01 00:00:00'",
            },
        }

        def _reflected_columns(sync_conn, table: str) -> set[str]:
            """Columns that physically exist in ``table`` right now."""
            return {
                column["name"]
                for column in sa_inspect(sync_conn).get_columns(table)
            }

        for table, columns in critical_columns.items():
            table = _ident(table)
            existing = await conn.run_sync(_reflected_columns, table)
            for col, ddl in columns.items():
                col = _ident(col)
                if col not in existing:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    )
                    logger.info(
                        "Added missing column %s.%s (schema self-heal).",
                        table,
                        col,
                    )


async def get_session() -> AsyncSession:
    """FastAPI/DI-friendly session dependency."""
    async with async_session_factory() as session:
        yield session
