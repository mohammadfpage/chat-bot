"""
FSM storage backed by the project's own database (SQLAlchemy async engine).

Why this exists
---------------
The FSM storage (profile-wizard steps, anonymous-chat queue position, …) used
to have only two backends:

* ``RedisStorage``  — when ``REDIS_URL`` is set (opt-in, extra paid service);
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
* ``state.clear()`` (state=None + data={}) really clears;
* data values are coerced through JSON, like RedisStorage does for
  ``msgpack``/pickle — an exotic value (a datetime, a Telegram object) is
  stringified rather than crashing the update.

Two round-trip savings, both measured against Neon in production
(~200-340 ms per DB query — see the ``slow DB query`` startup lines):

1. **One-statement writes.** The first DB-backed version did
   ``SELECT`` row → ``UPDATE``/``INSERT`` → ``COMMIT`` per FSM write. The
   write is now a single atomic ``INSERT … ON CONFLICT DO UPDATE`` — three
   sequential round trips become one, with no read-modify-write race.
2. **Per-process read-through cache.** aiogram reads the FSM state ONCE per
   update (``FSMContextMiddleware``), plus whatever the handler asks for.
   With no cache each of those reads was a fresh session + connection
   pre-ping + ``SELECT`` — several hundred ms of sequential latency that
   every single update paid, even the ones that touch nothing FSM.
   The cache holds ``KEY → (state, data)`` for THIS process and is updated
   by every write that goes through THIS storage, so a read is served from
   RAM and a write updates the DB and the cache atomically (cache first, DB
   committed next — a failed commit drops the entry so the next read
   re-fetches the truth from the database). ``state`` and ``data`` are cached
   with independent "known" flags because aiogram writes them in separate
   calls: writing one must never assert a value for the other, or a
   ``set_data`` would hide a wizard step the database still holds.
   Correctness scope (read before trusting it):
   * the bot runs as ONE process (Render free instance / ``max_instances:1``
     on the optional Cloudflare path), and every FSM write — handlers, the
     startup ``rebuild_chat_state`` hook, the expiry sweep — funnels through
     this single storage instance ("bot.py" stores it in
     ``dp.workflow_data["storage"]`` exactly once);
   * ``fsm_states`` has no other writer in the codebase and no external
     writer is expected;
   * the TTL below is therefore MEMORY hygiene, not staleness control: an
     expired entry simply re-reads the database. The only stale-read window
     that can ever exist needs a second process writing the same key, i.e.
     someone deliberately running two bot instances against one database —
     at most 60 s of divergence for wizard state, nothing else (pair state
     has its own persistent mirror in ``chat_pairs``).
3. **No-op writes are skipped.** ``state.clear()`` followed by
   ``set_state(idle)`` is the normal "back to the menu" path, and aiogram
   implements ``clear()`` as TWO calls (state=None, data={}). With the read
   cache telling us the row already holds those exact values, those writes
   become zero database round trips instead of three. A repeated menu tap
   therefore costs no FSM statement at all, and a ``set_data`` that rewrites
   the same dict is free.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from typing import Any, Mapping

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database.engine import async_session_factory, engine as _engine
from database.models import FsmState

#: The upsert dialect is fixed once per process — the storage shares the
#: engine with every handler, so its dialect IS the live dialect.
_upsert_insert = pg_insert if _engine.dialect.name == "postgresql" else sqlite_insert

logger = logging.getLogger(__name__)

#: Sentinel for "this field was not passed". ``None`` is a REAL value here
#: (``state.clear()`` writes exactly ``None``), so the "don't touch it" case
#: needs its own marker.
_UNSET: Any = object()

#: Memory hygiene for the read cache: entries expire after this many seconds
#: (a later read simply re-SELECTs) and the oldest entry is evicted past the
#: size cap. One record is a state string + a small dict — thousands fit in
#: a few MB.
_CACHE_TTL = 60.0
_CACHE_MAX_ENTRIES = 4_096

#: Marker for "this column has not been loaded into the cache yet". It is
#: deliberately NOT ``None``: ``None`` is a real state value (``state.clear()``
#: writes exactly it), and conflating the two is how a write to ``data`` would
#: erase a state the database still holds.
_UNKNOWN: Any = object()

#: sk → (cached_at, state_known, state, data_known, data)
_CacheRow = tuple[float, bool, Any, bool, dict[str, Any] | None]
_cache: OrderedDict[str, _CacheRow] = OrderedDict()


def _cache_get(sk: str) -> tuple[bool, Any, bool, dict[str, Any] | None] | None:
    """``(state_known, state, data_known, data)``; ``None`` on miss/expiry."""
    entry = _cache.get(sk)
    if entry is None:
        return None
    cached_at, state_known, state, data_known, data = entry
    if time.monotonic() - cached_at >= _CACHE_TTL:
        _cache.pop(sk, None)
        return None
    _cache.move_to_end(sk)
    return state_known, state, data_known, data


def _cache_put(
    sk: str,
    *,
    state: Any = _UNKNOWN,
    data: Any = _UNKNOWN,
) -> None:
    """Record only the columns this write actually touched.

    A ``set_data`` says NOTHING about ``state``: the row may already hold a
    wizard step that this process has not read yet. The two columns therefore
    carry independent "known" flags, and a read of an unknown column falls
    through to the database instead of assuming ``None`` — the difference
    between "no state" and "not loaded yet". Getting this wrong silently
    strands a user mid-wizard after a restart.
    """
    current = _cache_get(sk)
    state_known, old_state, data_known, old_data = current or (False, None, False, None)
    if state is not _UNKNOWN:
        state_known, old_state = True, state
    if data is not _UNKNOWN:
        data_known, old_data = True, data
    _cache[sk] = (time.monotonic(), state_known, old_state, data_known, old_data)
    _cache.move_to_end(sk)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _cache_drop(sk: str) -> None:
    _cache.pop(sk, None)


def _storage_key(key: StorageKey) -> str:
    """Turn an aiogram ``StorageKey`` into one collision-free text column value.

    The key parts this bot actually distinguishes are integers (or ``None``), so
    a ``:``-joined encoding is injective and short enough for the
    ``String(120)`` column (Telegram ids ≤ 15 digits, one part per slot).
    ``business_connection_id`` is appended ONLY when present.
    """
    parts: list[Any] = [
        key.bot_id,
        key.chat_id,
        getattr(key, "user_id", None),
        getattr(key, "thread_id", None),
    ]
    bconn = getattr(key, "business_connection_id", None)
    if bconn:
        parts.append(bconn)
    # ``str(p)`` (so a missing part is the literal ``"None"``) is NOT cosmetic:
    # it is the encoding every row written by the previous version used.
    # Switching the ``None`` placeholder would orphan all persisted wizard
    # state on deploy, so this must stay byte-identical for regular chat keys.
    return ":".join(str(p) for p in parts)


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
        # One engineered fallback for exotic types: ``default=str`` never raises.
        return json.loads(json.dumps(dict(data), default=str))


class DatabaseStorage(BaseStorage):
    """Keep FSM state in the same database the bot already uses.

    Reads are served from a small per-process cache that every write through
    THIS storage instance also updates (write-through, see the module docstring
    for the single-process correctness argument). Writes are one atomic upsert
    round trip instead of SELECT+UPDATE+COMMIT.

    The cache tracks ``state`` and ``data`` independently: aiogram writes them
    with separate calls, and a write must never claim a value for the column it
    did not touch (see :func:`_cache_put`).
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        # Injectable for smokes/tests; default is the process-wide factory,
        # which shares the engine and connection pool with every handler.
        self._session_factory = session_factory or async_session_factory

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        value = state.state if hasattr(state, "state") else state
        # The column is Text: a ``State`` (or ``str``) is stored bare — what
        # every handler/filter expects — but a structured payload (aiogram
        # scenes hand aiogram a LIST/JSON) is JSON-encoded, and decoded
        # symmetrically in ``get_state`` below.
        if value is not None and not isinstance(value, str):
            value = json.dumps(value)
        await self._upsert(key, state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        value = await self._read_state(key)
        # Bare strings (State names, every row written before scenes) pass
        # through untouched; only JSON-looking text is decoded.
        if isinstance(value, str) and value[:1] in ("[", "{"):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError(
                f"Data must be a dict or dict-like object, got {type(data).__name__}"
            )
        await self._upsert(key, data=_json_safe(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        data = await self._read_data(key)
        return dict(data) if data else {}

    # -- internals ---------------------------------------------------------

    async def _read_state(self, key: StorageKey) -> Any:
        """The state column, from cache when loaded, else one SELECT."""
        sk = _storage_key(key)
        cached = _cache_get(sk)
        if cached is not None and cached[0]:
            return cached[1]
        await self._load(sk)
        cached = _cache_get(sk)
        return cached[1] if cached is not None else None

    async def _read_data(self, key: StorageKey) -> dict[str, Any] | None:
        """The data column, from cache when loaded, else one SELECT."""
        sk = _storage_key(key)
        cached = _cache_get(sk)
        if cached is not None and cached[2]:
            return cached[3]
        await self._load(sk)
        cached = _cache_get(sk)
        return cached[3] if cached is not None else None

    async def _load(self, sk: str) -> None:
        """SELECT both columns once and cache them as KNOWN (row or no row)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(FsmState.state, FsmState.data).where(
                    FsmState.storage_key == sk
                )
            )
            row = result.one_or_none()

        if row is None:
            # "There is no row" is a fact worth caching too: the next read of
            # either column must not hit the database again for 60 s.
            _cache_put(sk, state=None, data=None)
        else:
            _cache_put(sk, state=row[0], data=dict(row[1]) if row[1] else None)

    async def _upsert(
        self,
        key: StorageKey,
        *,
        state: Any = _UNSET,
        data: dict[str, Any] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Write state and/or data to one row in ONE statement.

        ``_UNSET`` means "leave this column alone"; a real ``None`` overwrites
        it — that distinction is what makes ``await state.clear()`` (which
        writes state=None + data={}) actually clear the record.

        Only the columns in ``values`` are written; the cache is told about
        exactly those columns, so a ``set_data`` can never hide a persisted
        state that has not been read yet in this process.
        """
        sk = _storage_key(key)

        cached = _cache_get(sk)
        values: dict[str, Any] = {}
        # A write that would store the value already known for this process is
        # a guaranteed no-op (single-process, see the module docstring), and on
        # Neon every avoided statement is ~200-340 ms off the user's wait. This
        # is exactly the ``clear() → set_state(idle)`` dance in the menu
        # handlers: two of its three writes changed nothing.
        if state is not _UNSET:
            if cached is not None and cached[0] and cached[1] == state:
                pass
            else:
                values["state"] = state
        if data is not _UNSET:
            # ``None`` and ``{}`` are the same "no wizard data" row state: the
            # loader caches a missing/JSON-null column as ``None`` while
            # ``get_data`` hands callers ``{}``, so comparing them strictly
            # would rewrite an empty dict forever.
            same_data = (
                cached is not None
                and cached[2]
                and (cached[3] or {}) == (data or {})
            )
            if not same_data:
                values["data"] = data
        if not values:  # nothing to change: no round trip at all
            return

        _cache_put(sk, state=values.get("state", _UNKNOWN), data=values.get("data", _UNKNOWN))

        try:
            async with self._session_factory() as session:
                # One INSERT (fresh key) or UPDATE (known key) — never a
                # SELECT-then-rewrite, so a write is exactly one round trip
                # and can never race a concurrent write for the same key.
                stmt = (
                    _upsert_insert(FsmState)
                    .values(storage_key=sk, **values)
                    .on_conflict_do_update(
                        index_elements=[FsmState.storage_key],
                        set_=values,
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except SQLAlchemyError:
            # The DB is the source of truth: a failed write must not leave a
            # cache entry claiming a value it never persisted — drop it so the
            # next read re-SELECTs. The exception propagates to aiogram, which
            # reports it for this update exactly like RedisStorage would.
            _cache_drop(sk)
            raise

    async def close(self) -> None:
        # Nothing to close: the engine is process-global and shut down by the
        # boot/shutdown code that owns it (bot.main finally → engine.dispose).
        return None


__all__ = ["DatabaseStorage"]
