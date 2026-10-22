"""
Connectivity + latency probe for the production database and FSM cache.

Run this ON THE SERVER that runs the bot (Render Shell, a container console, a
VPS) so the numbers are the real round-trip times the bot sees — a laptop in
another region measures the wrong network:

    python scripts/check_latency.py

It prints, without ever revealing a credential:

  * Redis (Upstash): TCP connect, TCP+TLS handshake, warm PING, and the cost of
    ONE aiogram FSM step (state + data read, state + data write, expire — the
    several *sequential* round trips a single profile tap actually pays for).
  * PostgreSQL (Neon): engine connect, a warm ``SELECT 1`` and a real
    ``SELECT ... FROM users`` — the same shape of query the middlewares run.

Interpretation: a warm PING near the TCP RTT means Redis is fine and the delay
is the number of round trips; a warm ``SELECT 1`` of hundreds of ms means the
network/database is far away (or Neon is cold); Python-side cost is whatever is
left in a slow ``update`` line from the ``perf`` logger.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings  # noqa: E402

N = 10


def _stat(samples: list[float]) -> str:
    if not samples:
        return "n/a"
    return (
        f"min={min(samples) * 1000:.1f}ms  "
        f"avg={statistics.mean(samples) * 1000:.1f}ms  "
        f"p50={statistics.median(samples) * 1000:.1f}ms  "
        f"max={max(samples) * 1000:.1f}ms"
    )


async def _probe_redis() -> None:
    print("── Redis / FSM storage ─────────────────────────────")
    url = (settings.redis_url or "").strip()
    if not url:
        print("  REDIS_URL is empty → FSM stored in the DATABASE (durable across")
        print("  restarts/sleeps — no extra Redis service needed).")
        return

    if url.startswith("redis://") and (urlsplit(url).hostname or "").endswith(
        ".upstash.io"
    ):
        url = "rediss://" + url[len("redis://"):]
        print("  (plain redis:// upgraded to rediss:// for Upstash)")
    host = urlsplit(url).hostname or "?"
    print(f"  host: {host}")

    import ssl

    wait = 5.0
    tcp: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, urlsplit(url).port or 6379), wait
            )
        except Exception as exc:
            print(f"  TCP connect FAILED: {type(exc).__name__}: {exc}")
            return
        tcp.append(time.perf_counter() - t0)
        writer.close()
    print(f"  TCP connect     : {_stat(tcp)}")

    ctx = ssl.create_default_context()
    tls: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, urlsplit(url).port or 6379, ssl=ctx),
                wait,
            )
        except Exception as exc:
            print(f"  TLS handshake FAILED: {type(exc).__name__}: {exc}")
            break
        tls.append(time.perf_counter() - t0)
        writer.close()
    if tls:
        print(f"  TCP+TLS         : {_stat(tls)}")

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            url, socket_connect_timeout=wait, socket_timeout=wait
        )
        t0 = time.perf_counter()
        await client.ping()
        print(f"  first PING      : {(time.perf_counter() - t0) * 1000:.1f}ms")
        pings: list[float] = []
        for _ in range(N):
            t0 = time.perf_counter()
            await client.ping()
            pings.append(time.perf_counter() - t0)
        print(f"  warm PING       : {_stat(pings)}")

        step: list[float] = []
        for _ in range(N):
            t0 = time.perf_counter()
            await client.hgetall("probe:fsm")
            await client.hgetall("probe:fsm:data")
            await client.hset("probe:fsm", mapping={"state": "x"})
            await client.hset("probe:fsm:data", mapping={"d": "1"})
            await client.expire("probe:fsm", 3600)
            step.append(time.perf_counter() - t0)
        print(f"  FSM 5-op step   : {_stat(step)}")
        print("  ↳ each aiogram FSM read/write is SEPARATE round trips; at a high")
        print("    RTT this is the dominant cost — put Redis in Render's region.")
        await client.aclose()
    except Exception as exc:
        print(f"  redis-py FAILED: {type(exc).__name__}: {exc}")
    print()


async def _probe_postgres() -> None:
    print("── PostgreSQL ──────────────────────────────────────")
    if not settings.database_dialect.startswith("postgres"):
        print(f"  DATABASE_URL is {settings.database_dialect!r} → no remote DB to probe.")
        return
    host = urlsplit(settings.database_url).hostname or "?"
    print(f"  host: {host}")
    print(f"  ssl  : {'require' if 'ssl=' in settings.database_url else 'driver default'}")

    try:
        from sqlalchemy import text

        from database.engine import engine

        t0 = time.perf_counter()
        async with engine.connect() as conn:
            print(f"  engine connect  : {(time.perf_counter() - t0) * 1000:.1f}ms")
            warm: list[float] = []
            for _ in range(N):
                t0 = time.perf_counter()
                await conn.execute(text("SELECT 1"))
                warm.append(time.perf_counter() - t0)
            print(f"  SELECT 1 (warm) : {_stat(warm)}")
            real: list[float] = []
            for _ in range(N):
                t0 = time.perf_counter()
                await conn.execute(text("SELECT id FROM users LIMIT 1"))
                real.append(time.perf_counter() - t0)
            print(f"  SELECT users    : {_stat(real)}")
        await engine.dispose()
        print("  ↳ a warm SELECT 1 costing hundreds of ms means the DB is in a")
        print("    different region (or Neon was cold and just resumed).")
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
    print()


async def main() -> None:
    print()
    print("Bot latency probe — run this on the machine that runs the bot.")
    print(f"database dialect: {settings.database_dialect}")
    print()
    await _probe_redis()
    await _probe_postgres()


if __name__ == "__main__":
    asyncio.run(main())
