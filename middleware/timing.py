"""
Timing instrumentation for the three latency-sensitive hops.

The symptom these exist for is "the buttons feel slow", which is impossible to
attribute from the outside: the time could be spent in the network to Redis, in
a database round trip, in a Telegram API call, or in plain Python. This module
measures all three of the ones the code controls and logs each at a single
logger (``perf``):

* :class:`TimingMiddleware` — the whole update, from the first middleware to the
  last handler line. Registered OUTERMOST so its number is the total the user
  actually waited through, including every other middleware.
* :class:`TimingStorage` — every FSM ``get_state``/``get_data``/``set_state``/
  ``set_data``/``update_data``/``get_value`` call. aiogram keeps state and data
  under SEPARATE keys, so one profile step is several *sequential* Redis round
  trips; on a remote Redis that is the difference between 5 ms and 1.5 s.
* ``database/engine.py`` — every SQL statement (``slow DB query`` lines).

Threshold is ``PERF_SLOW_MS`` (default 150 ms): at or above it the line is a
WARNING and always visible; below it the line is DEBUG so ``LOG_LEVEL=DEBUG``
reveals the full breakdown without drowning a normal run.

Privacy: message *content* is never logged. A message is summarised by its chat
type, sender id and length (or its command name); only callback data — which is
our own ``namespace:action`` vocabulary, never user text — is printed verbatim.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineQuery, Message

from config import settings

logger = logging.getLogger("perf")

#: Anything at/above this many milliseconds is logged at WARNING; below it at
#: DEBUG. 0 means "log everything".
_SLOW_MS = float(settings.perf_slow_ms or 0)

#: Label of the update being handled right now, so a slow FSM storage call
#: emitted deep inside the dispatcher can name the tap it belonged to.
_current_update: ContextVar[str] = ContextVar("perf_current_update", default="-")


def _emit(label: str, started: float, detail: str = "") -> float:
    """Log one measured duration and return it in milliseconds."""
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    line = detail.strip()
    if elapsed_ms >= _SLOW_MS:
        logger.warning("%s | %.0fms | %s", label, elapsed_ms, line)
    else:
        logger.debug("%s | %.1fms | %s", label, elapsed_ms, line)
    return elapsed_ms


def _describe(event: Any) -> str:
    """A content-free label for the update (see the module privacy note)."""
    user = getattr(event, "from_user", None)
    uid = user.id if user else "?"
    if isinstance(event, CallbackQuery):
        data = (event.data or "")[:60]
        return f"cb user={uid} data={data}"
    if isinstance(event, InlineQuery):
        return f"inline user={uid} q_len={len(event.query or '')}"
    if isinstance(event, Message):
        chat = event.chat
        ctype = chat.type if chat else "?"
        if event.text and event.text.startswith("/"):
            cmd = event.text.split()[0].split("@")[0]
            return f"msg chat={ctype} user={uid} cmd={cmd}"
        text_len = len(event.text) if event.text else 0
        return f"msg chat={ctype} user={uid} text_len={text_len}"
    return f"event={type(event).__name__} user={uid}"


class TimingMiddleware(BaseMiddleware):
    """Outermost middleware: log how long one update took end to end."""

    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        label = _describe(event)
        token = _current_update.set(label)
        started = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            _current_update.reset(token)
            _emit("update", started, label)


class TimingStorage(BaseStorage):
    """Transparent proxy that times every FSM storage call.

    Delegates every attribute it does not override, so swapping it in for the
    real ``RedisStorage``/``MemoryStorage`` changes nothing except the log. The
    ``redis`` attribute the startup PING uses is reached through
    ``__getattr__``; wrap only AFTER that check has passed.
    """

    def __init__(self, inner: BaseStorage) -> None:
        self._inner = inner

    # -- measured operations ------------------------------------------------
    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        started = time.perf_counter()
        try:
            return await self._inner.set_state(key, state)
        finally:
            _emit("fsm set_state", started, _key_detail(key, _current_update.get()))

    async def get_state(self, key: StorageKey) -> str | None:
        started = time.perf_counter()
        try:
            return await self._inner.get_state(key)
        finally:
            _emit("fsm get_state", started, _key_detail(key, _current_update.get()))

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        started = time.perf_counter()
        try:
            return await self._inner.set_data(key, data)
        finally:
            _emit("fsm set_data", started, _key_detail(key, _current_update.get()))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return await self._inner.get_data(key)
        finally:
            _emit("fsm get_data", started, _key_detail(key, _current_update.get()))

    async def update_data(
        self, key: StorageKey, data: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return await self._inner.update_data(key, data, **kwargs)
        finally:
            _emit("fsm update_data", started, _key_detail(key, _current_update.get()))

    async def get_value(self, key: StorageKey, dict_key: str) -> Any:
        started = time.perf_counter()
        try:
            return await self._inner.get_value(key, dict_key)
        finally:
            _emit("fsm get_value", started, _key_detail(key, _current_update.get()))

    async def close(self) -> None:
        return await self._inner.close()

    # -- everything else (key_builder, redis, …) passes straight through ----
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _key_detail(key: StorageKey, update_label: str) -> str:
    """``user=<id> update=<what triggered it>`` for a storage log line."""
    return f"user={getattr(key, 'user_id', '?')} update={update_label}"


__all__ = ["TimingMiddleware", "TimingStorage"]
