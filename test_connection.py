"""
Standalone connectivity probe for api.telegram.org — run it BEFORE bot.py.
==========================================================================

A filtered network looks like a broken bot: ``ClientConnectorError: Cannot
connect to host api.telegram.org:443 [WinError 1225]``. That message says
nothing about *why*, so this script answers the only question that matters —
"can this machine reach Telegram, and through which route?" — without
starting the bot, without touching the database, and without sending
anything to Telegram beyond ``getMe``.

Usage
-----
    python test_connection.py

What it checks, in order:

1. **Direct** — straight to api.telegram.org, no proxy at all.
2. **PROXY_URL** — the proxy from ``.env`` (SOCKS or HTTP), if one is set.
3. **Bot API** — ``getMe`` through the first route that worked, which also
   validates ``BOT_TOKEN``. The token is never printed.

Exit codes
----------
    0   at least one route works (the third check passing is the real proof)
    1   nothing works — see the printed advice (Persian + English)

The script never raises: every failure is a printed line, because its whole
job is to turn a stack trace into an answer.
"""

from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlsplit

import aiohttp

# ── Config ─────────────────────────────────────────────────────
# Read the same .env the bot reads. If that import blows up (broken .env,
# missing pydantic), fall back to os.environ so the probe still works.
try:
    from config import settings

    PROXY_URL = (settings.proxy_url or "").strip()
    BOT_TOKEN = (settings.bot_token or "").strip()
except Exception:  # pragma: no cover - defensive
    import os

    PROXY_URL = (os.environ.get("PROXY_URL") or "").strip()
    BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

TARGET = "https://api.telegram.org"
TIMEOUT = aiohttp.ClientTimeout(total=15)
_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


def _mask(url: str) -> str:
    """Hide the proxy password in printed output."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparsable>"
    if not parts.password:
        return url
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.username or ''}:***@{parts.hostname or ''}{port}"


def _describe(exc: BaseException) -> str:
    """One short, human-readable reason instead of a repr() wall."""
    name = type(exc).__name__
    text = str(exc)
    if "1225" in text or "refused" in text.lower():
        return f"{name} — connection refused (filtered/blocked, or nothing is listening)"
    if "timed out" in text.lower() or isinstance(exc, asyncio.TimeoutError):
        return f"{name} — timed out (packet dropped by a firewall)"
    if "getaddrinfo" in text.lower() or "Name or service" in text:
        return f"{name} — DNS failed (the hostname does not resolve here)"
    if "certificate" in text.lower() or "SSL" in text:
        return f"{name} — TLS problem ({text[:120]})"
    return f"{name}: {text[:160]}"


def _session_for(proxy: str | None) -> tuple[aiohttp.ClientSession, aiohttp.TCPConnector | None]:
    """Build a session for *proxy*, plus the connector to close afterwards.

    SOCKS needs its own connector (``aiohttp-socks``); plain HTTP is carried
    by aiohttp's ``proxy=`` argument instead. ``None`` connector = direct.
    """
    connector: aiohttp.TCPConnector | None = None
    if proxy and urlsplit(proxy).scheme.lower().startswith("socks"):
        from aiohttp_socks import ProxyConnector

        connector = ProxyConnector.from_url(proxy)
    # trust_env=False (aiohttp's default, made explicit): a system-wide
    # HTTP_PROXY variable must never silently change which route we test.
    session = aiohttp.ClientSession(
        connector=connector, timeout=TIMEOUT, trust_env=False
    )
    return session, connector


def _proxy_kwarg(proxy: str | None) -> dict:
    """``proxy=`` only applies to HTTP proxies; SOCKS is in the connector."""
    if proxy and not urlsplit(proxy).scheme.lower().startswith("socks"):
        return {"proxy": proxy}
    return {}


async def _close_connector(connector: aiohttp.TCPConnector | None) -> None:
    if connector is not None:
        await connector.close()


async def probe(label: str, proxy: str | None) -> bool:
    """Run one route and print a verdict line. ``True`` when it answered."""
    shown = _mask(proxy) if proxy else "no proxy (direct)"
    connector: aiohttp.TCPConnector | None = None
    try:
        # trust_env stays False on purpose: a stray system HTTP_PROXY must
        # not turn the "direct" check into a proxied one.
        session, connector = _session_for(proxy)
        started = time.perf_counter()
        async with session:
            async with session.get(
                TARGET, allow_redirects=False, **_proxy_kwarg(proxy)
            ) as resp:
                status = resp.status
        elapsed = time.perf_counter() - started
        ok = status < 500  # 404/401 still prove the wire works
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {label:<22} {shown:<38} HTTP {status} in {elapsed * 1000:.0f} ms")
        return ok
    except BaseException as exc:  # noqa: BLE001 — report, never crash
        print(f"[FAIL] {label:<22} {shown:<38} {_describe(exc)}")
        return False
    finally:
        await _close_connector(connector)


async def probe_api(proxy: str | None) -> bool:
    """``getMe`` through a known-good route: proves token + full API path."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[SKIP] bot api check                no BOT_TOKEN set in .env")
        return False

    url = f"{TARGET}/bot{BOT_TOKEN}/getMe"
    connector: aiohttp.TCPConnector | None = None
    try:
        session, connector = _session_for(proxy)
        async with session:
            async with session.get(url, **_proxy_kwarg(proxy)) as resp:
                data = await resp.json(content_type=None)
        if data.get("ok"):
            name = data.get("result", {}).get("username", "?")
            print(f"[OK  ] bot api check                getMe → @{name} (token is valid)")
            return True
        print(f"[FAIL] bot api check                Telegram said: {data}")
        return False
    except BaseException as exc:  # noqa: BLE001
        print(f"[FAIL] bot api check                {_describe(exc)}")
        return False
    finally:
        await _close_connector(connector)


def _advice(direct_ok: bool, proxy_ok: bool) -> None:
    print()
    print("=" * 72)
    if direct_ok or proxy_ok:
        print("At least one route works — the bot should start. / یک مسیر کار می‌کند.")
        if proxy_ok and not direct_ok:
            print("→ Keep PROXY_URL set in .env (direct access is blocked here).")
        if direct_ok and not proxy_ok and PROXY_URL:
            print("→ PROXY_URL does not work; leave it empty or fix the proxy.")
        return

    print("No route to api.telegram.org works — this is network filtering,")
    print("not a bug in the bot code.")
    print("هیچ مسیری به api.telegram.org باز نیست — این فیلترینگ شبکه است، نه باگ کد.")
    print()
    print("Fix / راه حل:")
    print("  1. Start your VPN/proxy client, then run this script again.")
    print("  2. Set a working proxy in .env:")
    print("       PROXY_URL=socks5://127.0.0.1:1080")
    print("       PROXY_URL=http://127.0.0.1:8080")
    print("  3. Re-run:  python test_connection.py")
    print("  4. Only when this script prints OK should you run:  python bot.py")
    print("=" * 72)


async def run() -> int:
    print("Probing https://api.telegram.org")
    print(f"PROXY_URL = {_mask(PROXY_URL) or '(not set)'}")
    print("-" * 72)

    direct_ok = await probe("direct", None)
    proxy_ok = False
    if PROXY_URL:
        scheme = urlsplit(PROXY_URL).scheme.lower()
        if scheme not in _PROXY_SCHEMES:
            print(f"[FAIL] proxy                     unsupported scheme {scheme!r}")
        else:
            proxy_ok = await probe("via PROXY_URL", PROXY_URL)
    else:
        print("[SKIP] via PROXY_URL              not set in .env (skipping)")

    print("-" * 72)
    route = PROXY_URL if proxy_ok else (None if direct_ok else None)
    api_ok = await probe_api(route)

    _advice(direct_ok, proxy_ok)
    return 0 if (direct_ok or proxy_ok) and (api_ok or not BOT_TOKEN) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
