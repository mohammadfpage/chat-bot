"""
FSM storage backed by the project's own database (SQLAlchemy async engine).

Why this exists
---------------
The FSM storage (profile-wizard steps, anonymous-chat queue position, …) used
to have only two backends:

* ``RedisStorage``  — when ``REDIS_URL`` is set (Opt in, extra paid service);
* ``MemoryStorage`` — the silent fallback, which LOSES all state on every
  restart or platform sleep. On Render that means a user standing mid-wizard
  was thrown back to the start for no reason an operator could see.

This module removes the losing fallback. ``REDIS_URL`` set → Redis is used
exactly as before; ``REDIS_URL`` empty → the same records are kept in the
database the bot already talks to (``DATABASE_URL``):

* production (Render): PostgreSQL — the *fast, already-open* connection,
  instead of a third network hop to a Redis that may be in the wrong region;
* local dev: SQLite — the state survives ``Ctrl+C`` too, and the table is
  just a file the next run reads back.

Nothing about the Redis path changes. This storage also doubles as the
fallback when Redis IS configured but fails the startup PING, so a broken
Upstash URL degrades to a durable backend instead of a lossy one.

Semantics match aiogram's own storages:

* ``get_state`` of an unknown key → ``None``; ``get_data`` → ``{}``;
* ``set_state``/``set_data`` are REPLACE, not merge (as documented);
* data values are coerced through JSON, exactly like RedisStorage does for
  ``msgpack``/pickle — a numpy value or a message object hits the same wall,
  just with a clearer message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import select

from database.engine import async_session_factory
from database.models import FsmState

logger = logging.getLogger(__name__)

#: Sentinel for "this field was not passed". ``None`` is a REAL value here
#: (``state.clear()`` writes exactly ``None``), so the "don't touch it" case
#: needs its own marker.
_UNSET: Any = object()


def _storage_key(key: StorageKey) -> str:
    """Turn an aiogram ``StorageKey`` into one collision-free text column value.

    All four aiogram key parts are integers (or ``None``), so a ``:``-joined
    encoding is injective and short enough for the ``String(120)`` column.
    Telegram ids never exceed 15 digits; each part is 16 or fewer characters.
    """
    return (
        f"{key.bot_id}:{key.chat_id}:{getattr(key, 'user_id', None)}:"
        f"{getattr(key, 'thread_id', None)}"
    )


def _json_safe(data: Mapping[str, Any]) -> dict[str, Any]:
    """Round-trip values through JSON so the column can always store them.

    RedisStorage serialises values with an encoder before the network; this is
    the database backend's equivalent. Clean data (anything the wizard stores:
    strings, ints, bools, list/dict) passes through **by reference** — the
    round-trip only happens once, on the rare message that carries an exotic
    value, so there is no extra cost in the hot path.
    """
    try:
        json.dumps(data)
        return dict(data)
    except (TypeError, ValueError):
        # One control-engineering fallback for exotic types (datetime, a
        # Telegram object, …): ``default=str`` never raises.
        return json.loads(json.dumps(dict(data), default=str))
    # NOTE: memory will occasionally hold a *string* instead of the original
    # object for one exotic value — same trade-off Redis makes and the reason
    # the wizard stores plain strings only.


class DatabaseStorage(BaseStorage):
    """Keep FSM state in the same database the bot already uses."""

    def __init__(self, session_factory: Any | None = None) -> None:
        # Injectable for smokes/tests; default is the process-wide factory,
        # which shares the engine and connection pool with every handler.
        self._session_factory = session_factory or async_session_factory

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        value = state.state if hasattr(state, "state") else state
        await self._upsert(key, state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._read(key)
        return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError(
                f"Data must be a dict or dict-like object, got {type(data).__name__}"
            )
        await self._upsert(key, data=_json_safe(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._read(key)
        return dict(row.data) if row and row.data else {}

    async def _read(self, key: StorageKey) -> FsmState | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FsmState).where(
                    FsmState.storage_key == _storage_key(key)
                )
            )
            return result.scalar_one_or_none()

    async def _upsert(
        self,
        key: StorageKey,
        *,
        state: Any = _UNSET,
        data: dict[str, Any] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Write state and/or data to one row, creating it when missing.

        ``_UNSET`` means "leave this column alone"; a real ``None`` overwrites
        it — that distinction is what makes ``await state.clear()`` (which
        writes state=None + data={}) actually clear the record.
        """
        sk = _storage_key(key)
        async with self._session_factory() as session:
            row = await session.get(FsmState, sk)
            if row is None:
                row = FsmState(storage_key=sk)
                session.add(row)
            if state is not _UNSET:
                row.state = state
            if data is not _UNSET:
                row.data = data
            await session.commit()

    async def close(self) -> None:
        # Nothing to close: the engine is process-global and shut down by the
        # boot/shutdown code that owns it (bot.main finally → engine.dispose).
        return None


__all__ = ["DatabaseStorage"]
