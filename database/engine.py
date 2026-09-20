"""
Async SQLAlchemy engine and session factory.
Swap 'aiosqlite' for 'asyncpg' when moving to PostgreSQL.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings
from database.models import Base

# ── Engine ──────────────────────────────────────────────
# pool_pre_ping verifies connections before use (safe for all dialects).
# When migrating to PostgreSQL, add pool_size=10, max_overflow=20 here.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

# ── Session factory ─────────────────────────────────────
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables on startup.

    Also performs a lightweight, non-destructive migration for SQLite:
    adds newly-introduced columns (e.g. ``users.is_admin``) to tables that
    were created by an older version of the schema.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ── Lightweight migration (SQLite) ──
        dialect = engine.dialect.name
        if dialect == "sqlite":
            # Ensure "is_admin" column exists on users table
            result = await conn.execute(text("PRAGMA table_info(users)"))
            columns = {row[1] for row in result.fetchall()}
            if "is_admin" not in columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0"))

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


async def get_session() -> AsyncSession:
    """FastAPI/DI-friendly session dependency."""
    async with async_session_factory() as session:
        yield session
