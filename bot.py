"""
Anonymous Chat Bot – Main Entry Point (polling or webhook)
==========================================================
Initializes the Bot and Dispatcher, applies middleware, registers routers,
then serves updates in whichever mode ``RUN_MODE`` selects:

* ``RUN_MODE=polling`` (default) — long polling, zero network setup, the
  right choice on a laptop and behind a VPN.
* ``RUN_MODE=webhook`` — starts a local aiohttp server and registers it
  with Telegram via ``setWebhook``. The server speaks plain HTTP; the
  public https:// hop (cloudflared tunnel locally, a domain + TLS or a
  tunnel in production) is provided by whatever sits in front of it, so no
  certificate ever has to be generated for a test run. See ``WEBHOOK.md``.

Two pieces of the reply-keyboard policy live here:

* ``install_keyboard_guard(bot)`` — wraps the API session so a
  ``ReplyKeyboardMarkup`` aimed at a group is rewritten to
  ``ReplyKeyboardRemove`` before it is serialised. Enforced at the last hop
  before the wire, so it cannot be bypassed by a handler that forgets its
  ``ChatType`` filter.
* ``dp.startup.register(cleanup_group_keyboards)`` — re-sends the removal into
  every known group on start, because Telegram's per-chat keyboard cache
  survives the bot being removed and re-added.
* ``GroupKeyboardWatchMiddleware`` + ``group_lifecycle_router`` — the
  "first sight" half: any group the bot hears from (or is added to) is
  remembered in ``group_chats`` and cleared immediately, so a group that the
  startup sweep has never seen before is covered without a restart.
"""

import asyncio
import inspect
import logging
import re
import secrets
import signal
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import BASE_DIR, settings
from database import engine, init_db
from handlers.chat import (
    rebuild_chat_state,
    start_chat_expiry,
    stop_chat_expiry,
)
from middleware import (
    AdminPanelGuardMiddleware,
    BlockBannedMiddleware,
    ForceJoinMiddleware,
    GroupKeyboardWatchMiddleware,
    GroupMemberWatchMiddleware,
    ReplyKeyboardGuardMiddleware,
    install_keyboard_guard,
)
from middleware.keyboard_guard import GuardedSession
from utils.group_cleanup import cleanup_group_keyboards
from utils.retention import start_retention, stop_retention
from utils.group_commands import (
    admin_command_rows,
    private_command_rows,
)
from utils.roster_store import start_persistence, stop_persistence
from utils.roster_sync import start_roster_sync, stop_roster_sync
from utils.whisper_config import get_whisper_config

#: Module-level so ``register_commands`` and friends can log without being
#: handed a logger — a ``logger`` that only exists as a local inside
#: ``main()`` made the "Telegram unreachable" path crash with ``NameError``
#: instead of reporting the network problem it was meant to report.
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Logging: console + rotating file, with the bot token redacted
# ──────────────────────────────────────────────────────────────

#: ``123456789:AAH...`` — a bot token as it appears in an error string.
#: Deliberately NO ``\b`` at either end: real leaks come as
#: ``.../bot<token>/sendMessage`` (letter→digit, no boundary) and tokens may
#: end in ``-`` (also no boundary). The mandatory ``:`` keeps false positives
#: to plain "number:number" strings, which are harmless to mask.
_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")


class _TokenRedactingFilter(logging.Filter):
    """Replace any bot token that leaks into a log record with ``***:***``.

    ``TelegramForbiddenError`` and network exceptions embed the full request
    URL — including the token — in their message. One uncaught exception used
    to be enough to paste the whole credential into a log file that then gets
    shipped around for debugging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken args formatting
            return True
        if _TOKEN_RE.search(message):
            record.msg = _TOKEN_RE.sub("***:***", message)
            record.args = ()
        return True


def _setup_logging() -> None:
    """Console + ``logs/bot.log`` (rotating), honouring ``LOG_LEVEL``.

    Replaces ``logging.basicConfig``: a file sink survives a terminal that
    closed, the rotation keeps a chatty weekend from filling the disk, and the
    redaction filter runs on every handler so no sink can leak the token.
    """
    level_name = (settings.log_level or "INFO").strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        logging.getLogger(__name__).warning(
            "LOG_LEVEL=%r is not a known level — falling back to INFO.",
            settings.log_level,
        )
        level = logging.INFO

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(level)
    # basicConfig may have run before (e.g. imported main twice in tests) —
    # clear pre-existing handlers so the filter/rotation are guaranteed.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_TokenRedactingFilter())
    root.addHandler(console)

    log_file = (settings.log_file or "").strip()
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(_TokenRedactingFilter())
            root.addHandler(file_handler)
        except OSError as exc:
            # A read-only volume must not stop the bot — console still works.
            root.warning("Could not open LOG_FILE %s: %s", path, exc)

# ──────────────────────────────────────────────────────────────
# Proxy support (PROXY_URL in .env)
# ──────────────────────────────────────────────────────────────

#: Schemes ``AiohttpSession(proxy=...)`` understands. Anything else in
#: ``PROXY_URL`` is a typo, and a typo must degrade to a direct connection
#: with a loud log — never to an exception during startup.
_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


def _mask_proxy(url: str) -> str:
    """``socks5://user:secret@host:1080`` → ``socks5://user:***@host:1080``.

    The proxy is logged on every start; the password never is.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparsable PROXY_URL>"
    if not parts.password:
        return url
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.username or ''}:***@{host}{port}"


def build_session() -> AiohttpSession:
    """Create the API session, routed through ``PROXY_URL`` when it is set.

    ``aiohttp`` is told about the proxy by handing the URL to
    ``AiohttpSession(proxy=...)``; aiogram then swaps its plain
    ``TCPConnector`` for the ``ProxyConnector`` from ``aiohttp-socks``, which
    is what carries SOCKS4/5 *and* plain HTTP proxies, with or without
    credentials. Nothing else in the codebase has to know a proxy exists.

    Every failure mode here is non-fatal and turns into a direct connection
    instead, because a bot that starts without Telegram is strictly better
    than a bot that refuses to start over a bad ``.env`` line:

    * unsupported scheme (``PROXY_URL=1080``) → warn + direct;
    * ``aiohttp-socks`` missing → warn + direct;
    * connector refused to build → warn + direct.

    An unreachable proxy is caught later, by the retry/fallback path in
    ``main()``; construction only fails on configuration problems.
    """
    proxy = (settings.proxy_url or "").strip()
    if not proxy:
        logger.info("PROXY_URL is empty — connecting to api.telegram.org directly.")
        return AiohttpSession()

    scheme = proxy.split(":", 1)[0].lower()
    if scheme not in _PROXY_SCHEMES:
        logger.error(
            "PROXY_URL has an unsupported scheme %r (want one of: %s). "
            "Ignoring it and connecting directly.",
            scheme,
            ", ".join(sorted(_PROXY_SCHEMES)),
        )
        return AiohttpSession()

    try:
        session = AiohttpSession(proxy=proxy)
    except RuntimeError as exc:
        # aiogram raises RuntimeError when aiohttp-socks is not installed.
        logger.error(
            "%s — install it with 'pip install aiohttp-socks'. "
            "Connecting directly for now.",
            exc,
        )
        return AiohttpSession()
    except Exception as exc:
        logger.error(
            "Could not build a proxied session for %s (%s) — connecting directly.",
            _mask_proxy(proxy),
            exc,
        )
        return AiohttpSession()

    logger.info("Proxy enabled: %s", _mask_proxy(proxy))
    return session


def log_connection_advice(reason: str = "", bot: Bot | None = None) -> None:
    """Log one actionable troubleshooting block instead of a bare traceback.

    A filtered network looks exactly like a broken bot: ``ClientConnectorError
    Cannot connect to host api.telegram.org:443``. The traceback says nothing
    about *why*, and pasting it into a search engine sends people chasing
    SSL bugs that do not exist. This block says what was tried and what to do
    next, in English and Persian.

    *bot* is optional: when given, the "route" line reports the session that
    is actually in play *now* — after a proxy→direct fallback the config no
    longer describes reality.
    """
    proxy = (settings.proxy_url or "").strip()
    route = _mask_proxy(proxy) or "(unset → direct connection)"
    if bot is not None:
        outer = bot.session
        inner = outer._inner if isinstance(outer, GuardedSession) else outer
        direct_now = not isinstance(inner, AiohttpSession) or inner.proxy is None
        if proxy and direct_now:
            route = f"{_mask_proxy(proxy)} → fell back to DIRECT (proxy failed)"
        elif proxy:
            route = f"{_mask_proxy(proxy)} (proxied)"
        else:
            route = "direct (no PROXY_URL)"
    lines = [
        "=" * 68,
        "Could not reach api.telegram.org:443 — this is a network/filtering",
        "problem, not a bug in the bot code.",
        "اتصال به api.telegram.org برقرار نشد — مشکل شبکه یا فیلترینگ است،",
        "نه باگ در کد ربات.",
        "",
        "What was tried / موارد بررسی‌شده:",
        f"  PROXY_URL = {_mask_proxy(proxy) or '(unset)'}",
        f"  Route      = {route}",
    ]
    if reason:
        lines.append(f"  Last error = {reason}")
    lines += [
        "",
        "Fix it / راه حل:",
        "  1. python test_connection.py     (probe direct + proxy before starting)",
        "  2. Start your VPN/proxy client, then run it again.",
        "  3. Put a working proxy in .env:",
        "       PROXY_URL=socks5://127.0.0.1:1080",
        "       PROXY_URL=http://127.0.0.1:8080",
        "  4. Leave PROXY_URL empty only if a system-wide VPN already covers",
        "     this machine.",
        "",
        "The bot will now exit cleanly. Run 'python test_connection.py' first;",
        "it prints which route works.",
        "=" * 68,
    ]
    for line in lines:
        logger.error(line)


async def _fallback_to_direct(bot: Bot) -> bool:
    """Swap a failing proxied session for a direct one. ``True`` if swapped.

    A proxy that is down, misconfigured, or itself blocked must not stop the
    bot when the machine can still reach Telegram some other way (system VPN,
    TUN mode, a second adapter). The guard wrapper around the session is
    preserved — only the inner transport is replaced — so reply-keyboard
    scrubbing keeps working after the swap.

    No ``PROXY_URL`` means there is nothing to fall back *from*, hence
    ``False``: the caller then reports the outage instead of retrying.
    """
    if not (settings.proxy_url or "").strip():
        return False

    outer = bot.session
    inner = outer._inner if isinstance(outer, GuardedSession) else outer
    if not isinstance(inner, AiohttpSession) or inner.proxy is None:
        return False  # already direct — nothing to fall back to

    direct = AiohttpSession()
    # Close the old transport BEFORE the swap: after the swap, GuardedSession
    # would delegate .close() to the new inner session and leak the old one.
    try:
        await inner.close()  # type: ignore[misc]
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Closing the proxied session failed: %s", exc)

    if isinstance(outer, GuardedSession):
        outer._inner = direct
    else:
        bot.session = direct
    return True


# ──────────────────────────────────────────────────────────────
# Startup/shutdown hook plumbing
# ──────────────────────────────────────────────────────────────

#: Strong refs to tasks spawned from hooks — a bare ``create_task`` whose
#: task object is never stored can be garbage-collected mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def _log_task_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task %r failed: %s: %s",
            task.get_name(),
            type(exc).__name__,
            exc,
            exc_info=exc,
        )


def _keep(task: asyncio.Task) -> None:
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    task.add_done_callback(_log_task_failure)


def _hook(name: str, fn):
    """Wrap a startup/shutdown callback so one failure can't kill the rest.

    aiogram emits the hooks sequentially; an exception in an early hook would
    abort the remaining ones — and with them polling itself. ``fn`` may be
    sync or async and receives only the parameters it declares, mirroring
    aiogram's own kwarg forwarding.
    """
    parameters = inspect.signature(fn).parameters
    accepts_kwargs = any(
        p.kind is p.VAR_KEYWORD for p in parameters.values()
    )

    async def _wrapped(**kwargs):
        call_kwargs = (
            kwargs
            if accepts_kwargs
            else {k: v for k, v in kwargs.items() if k in parameters}
        )
        try:
            result = fn(**call_kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Startup hook %r failed — continuing.", name)

    _wrapped.__name__ = f"hook_{name}"
    return _wrapped


async def _spawn_keyboard_cleanup(bot: Bot) -> None:
    """Run the reply-keyboard sweep in the BACKGROUND.

    The sweep waits per chat (clients need the time to apply the removal),
    so with a real fleet it takes minutes; holding ``start_polling`` that
    long made the bot look dead — /start unanswered, updates queued. The
    first-sight watcher in ``GroupKeyboardWatchMiddleware`` still covers
    any group the sweep has not reached yet.
    """
    _keep(
        asyncio.create_task(
            cleanup_group_keyboards(bot), name="group-keyboard-cleanup"
        )
    )


# ──────────────────────────────────────────────────────────────
# FSM storage: MemoryStorage locally, Redis in the container
# ──────────────────────────────────────────────────────────────

#: How long the startup PING waits for Redis before deciding the backend is
#: unusable. Short on purpose: a wrong host, a dropped port or a TLS mismatch
#: must fail fast at boot instead of hanging the first FSM operation — and,
#: through the ping, startup itself — forever.
_FSM_PING_TIMEOUT = 5.0


def build_fsm_storage(redis_url: str | None = None) -> BaseStorage:
    """Pick the aiogram FSM backend from ``REDIS_URL``.

    Local/default: ``MemoryStorage`` — state dies with the process, which is
    exactly right for a laptop that is restarted on purpose.

    Production (Render/Cloudflare): a Redis URL keeps FSM states across the
    platform's stop/start cycles, so a user mid-profile-setup or mid-queue
    does not lose their place when the container sleeps — **but only if the
    URL actually connects**. ``RedisStorage.from_url`` merely *parses* the URL
    and opens no connection, so this function alone cannot tell a working
    Upstash endpoint from a typo; ``verify_fsm_storage`` pings it at startup
    for that. A URL that cannot even be parsed degrades to MemoryStorage with
    a loud log instead of an import/startup crash.

    Upstash (TLS) needs the **rediss://** scheme — with a plain ``redis://``
    the TLS handshake never happens and every state read fails. The endpoint
    is ``rediss://default:<password>@<region>.upstash.io:<port>``. Connect and
    read sockets are bounded so a firewalled host fails fast rather than hangs.

    Passwords in the URL are masked in the log, same as ``PROXY_URL``.
    """
    url = (settings.redis_url if redis_url is None else redis_url or "").strip()
    if not url:
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        storage = RedisStorage.from_url(
            url,
            connection_kwargs={
                "socket_connect_timeout": _FSM_PING_TIMEOUT,
                "socket_timeout": _FSM_PING_TIMEOUT,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a bad REDIS_URL must not stop boot
        logger.error(
            "REDIS_URL is set but unusable (%s) — falling back to MemoryStorage. "
            "FSM state will NOT survive a restart.", exc,
        )
        return MemoryStorage()
    logger.info("FSM storage: Redis (%s)", _mask_proxy(url))
    return storage


async def verify_fsm_storage(storage: BaseStorage) -> BaseStorage:
    """Prove a Redis FSM backend is reachable, or fall back to memory.

    ``build_fsm_storage`` cannot detect an *unreachable* Redis — parsing a URL
    succeeds even for a dead host. That gap is the whole profile-loss bug in
    production: the wizard carries age/city/gender ONLY in FSM state until
    «✅ تایید», so a Redis that silently fails on the first write loses the
    answers and the user can never finish. A single bounded ``PING`` here,
    before any update is served, turns that into a loud startup error.

    A failed ping falls back to MemoryStorage (the bot still runs) but logs at
    ERROR, and the discarded Redis pool is closed so it cannot leak. A
    MemoryStorage is returned untouched — there is nothing to reach.
    """
    redis = getattr(storage, "redis", None)
    if redis is None:
        return storage  # MemoryStorage: no connection to verify

    try:
        pong = await asyncio.wait_for(redis.ping(), timeout=_FSM_PING_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — never let a bad Redis stop boot
        logger.error(
            "REDIS_URL is set but the server did not answer PING (%s: %s) — "
            "falling back to MemoryStorage. FSM state (a half-finished "
            "profile, a chat queue) will NOT survive a restart. Check the "
            "Upstash URL: it must be rediss://… (TLS) with the right password.",
            type(exc).__name__,
            exc,
        )
        with suppress(Exception):
            await storage.close()
        return MemoryStorage()

    if not pong:
        logger.error(
            "REDIS accepted the connection but PING returned a false value — "
            "falling back to MemoryStorage; FSM state will NOT survive a "
            "restart."
        )
        with suppress(Exception):
            await storage.close()
        return MemoryStorage()

    logger.info("FSM storage ping OK — Redis reachable; state will persist.")
    return storage


# ──────────────────────────────────────────────────────────────
# Graceful stop (SIGTERM) — container shutdown, docker stop, systemd
# ──────────────────────────────────────────────────────────────

def _install_stop_signal() -> None:
    """Turn SIGTERM into a cancellation of the running ``main()`` task.

    Cloudflare Containers (like systemd and ``docker stop``) stop a process
    with SIGTERM and escalate to SIGKILL after a grace period. Python's
    default SIGTERM action is immediate process death: ``finally`` blocks
    never run, the aiohttp server is torn down mid-request, pooled DB
    connections are left open and SQLite WAL files never checkpoint. Routing
    SIGTERM into ``task.cancel()`` reuses the exact shutdown path Ctrl+C
    already takes (``run_webhook``'s ``finally``, ``main()``'s ``finally``).

    The handler is installed from *inside* ``main()`` so the task being
    cancelled is the one actually running the bot. ``loop.add_signal_handler``
    is unavailable for SIGTERM on Windows (Proactor loop) — the
    ``signal.signal`` fallback there still works because Python runs signal
    handlers in the main thread and merely schedules the cancellation.
    """
    try:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
    except RuntimeError:
        return  # no running loop — nothing to cancel
    if task is None:
        return

    def _request_stop() -> None:
        if not task.done():
            logger.info("SIGTERM received — shutting down cleanly.")
            task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _request_stop)
    except (NotImplementedError, RuntimeError, ValueError):
        with suppress(ValueError):
            # signal.signal handlers may only be set in the main thread.
            signal.signal(
                signal.SIGTERM,
                lambda *_: loop.call_soon_threadsafe(_request_stop),
            )


async def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    """Create the Bot and Dispatcher, register routers + middleware."""
    # ── FSM storage ──
    # MemoryStorage by default (local polling); RedisStorage when REDIS_URL
    # is set, which is what keeps a user's place in a wizard across host
    # restarts/sleeps (Render, Cloudflare, …). See build_fsm_storage.
    # verify_fsm_storage pings it once so a misconfigured/unreachable Upstash
    # is caught here, before any update is served, instead of on the first
    # profile step the user takes.
    storage = build_fsm_storage(settings.redis_url)
    storage = await verify_fsm_storage(storage)

    bot = Bot(
        token=settings.bot_token,
        session=build_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # ── Keyboard guard (installed on the session, not the dispatcher) ──
    # The rule "no ReplyKeyboardMarkup in groups" is enforced where every
    # outgoing API call funnels through, so it holds even for a handler that
    # forgets its ChatType filter. Must be installed after the Bot exists.
    install_keyboard_guard(bot)

    # ── Middleware (order matters: keyboard-guard → ban-block → force-join) ──
    # The keyboard guard goes first so it is the OUTERMOST wrapper: it publishes
    # the incoming chat's type for the duration of the handler, which lets the
    # session guard skip a get_chat call even for messages sent by the
    # force-join middleware below.
    dp.message.outer_middleware(ReplyKeyboardGuardMiddleware())
    dp.callback_query.outer_middleware(ReplyKeyboardGuardMiddleware())
    # First-sight watcher: records every group the bot hears from and clears
    # its cached reply keyboard once — covers a group the bot was JUST added
    # to, which the startup sweep below cannot know about yet.
    dp.message.outer_middleware(GroupKeyboardWatchMiddleware())
    dp.callback_query.outer_middleware(GroupKeyboardWatchMiddleware())
    # Group roster bookkeeping: every group message proves its sender is a
    # member, which is what lets the inline picker resolve a whisper recipient
    # from memory instead of the database on every keystroke.
    dp.message.outer_middleware(GroupMemberWatchMiddleware())
    dp.callback_query.outer_middleware(GroupMemberWatchMiddleware())
    dp.message.outer_middleware(BlockBannedMiddleware())
    dp.callback_query.outer_middleware(BlockBannedMiddleware())
    dp.message.outer_middleware(ForceJoinMiddleware())
    dp.callback_query.outer_middleware(ForceJoinMiddleware())
    # The inline picker is a third door into the bot, so it needs the same gate.
    # A user who has not joined is answered with ONE result pointing at the PM
    # instead of the channel list, which is what keeps a stranger's chat free
    # of channel-join prompts.
    dp.inline_query.outer_middleware(ForceJoinMiddleware())
    # Last of the callback gates: every ``admin:*`` tap is re-checked against
    # the admin filter before the dispatcher is allowed to look at it. The
    # router already refuses non-admins; this is the backstop that holds if a
    # handler or filter on the panel ever stops refusing them itself.
    dp.callback_query.outer_middleware(AdminPanelGuardMiddleware())

    # ── Routers (specific → general) ──
    from handlers import (
        group_lifecycle_router,
        inline_anon_router,
        anon_chat_router,
        navigation_router,
        keyboard_fix_router,
        admin_router,
        support_router,
        whisper_router,
        anonymous_router,
        chat_router,
        profile_router,
        start_router,
    )
    # group_lifecycle_router only listens for membership service updates
    # (bot added/removed) plus chat_member, which feeds the in-memory group
    # roster. On the way in it also posts the welcome / rights cards, all of
    # which are plain messages in the group — they cannot collide with a
    # message router because they fire on a different update type.
    dp.include_router(group_lifecycle_router)
    # inline_anon_router goes FIRST: it captures the message the user produced
    # from our own inline result, and the state-gated catch-alls below
    # (ChatState.in_chat, AnonChatStates.in_session) would otherwise swallow it.
    dp.include_router(inline_anon_router)
    # anon_chat_router must sit ABOVE every state-gated catch-all for the same
    # reason, and above navigation_router as well: it owns a bare
    # ``@router.message()`` for private chats, and the only thing that keeps it
    # from eating /start, /menu and the main-menu labels is its own explicit
    # bail-out. Every other message router below it therefore has to get its
    # turn after it declines with SkipHandler.
    dp.include_router(anon_chat_router)
    # navigation_router must stay first among the *menu* routers: it owns
    # "/start" and the "back to main menu" button, and the wizard routers below
    # would otherwise swallow both with their catch-all state handlers.
    dp.include_router(navigation_router)
    # keyboard_fix_router sits ABOVE every state-gated catch-all so the
    # emergency /fix_keyboard command can never be swallowed mid-wizard.
    dp.include_router(keyboard_fix_router)
    dp.include_router(admin_router)
    # support_router sits above the state-gated relay catch-alls for the same
    # reason navigation_router does: during a live chat every text message is
    # relayed to the partner, so the «🎧 پشتیبانی» label has to be claimed here
    # first. Its entry handler refuses to open inside a live session (it just
    # warns), so the partner is never stranded by a state swap.
    dp.include_router(support_router)
    dp.include_router(whisper_router)
    dp.include_router(anonymous_router)
    dp.include_router(chat_router)
    dp.include_router(profile_router)
    dp.include_router(start_router)

    # ── Startup hook: rebuild the chat state from the durable mirror ──
    # MUST be registered FIRST: pair_map/search_queue/chat_opened_at are
    # written through to the chat_pairs table (handlers/chat.py), and every
    # hook below — the expiry sweep above all — assumes those maps are
    # already real. On a restart of any kind this is what re-pairs the
    # conversations that were live, re-queues the searchers, and restores
    # «چت بعدی»'s last mode. Needs both the bot (its id keys the FSM) and
    # the storage (to force the restored users back into in_chat/in_queue).
    dp.startup.register(_hook("rebuild_chat_state", rebuild_chat_state))

    # ── Startup hook: purge cached group reply keyboards ──
    # Telegram keys its reply-keyboard cache per chat and never flushes it when
    # the bot is removed and re-added, so a group that ever received a
    # ReplyKeyboardMarkup keeps it stuck at the bottom. This re-sends
    # ReplyKeyboardRemove into every known group on each start; the guard above
    # is what stops it from ever coming back. aiogram injects "bot" here.
    # Spawned as a background task — see _spawn_keyboard_cleanup.
    dp.startup.register(
        _hook("cleanup_group_keyboards", _spawn_keyboard_cleanup)
    )

    # ── Startup hook: backfill the group roster ──
    # The in-memory roster the whisper pickers resolve recipients against only
    # learns from incremental feeds (chat_member updates, group messages), so on
    # a cold start it knows almost nobody. getChatAdministrators tops it up with
    # every admin of every known group — the Bot API has no equivalent listing
    # for ordinary members, which is why those come from the incremental feeds.
    # Registered on startup/shutdown rather than awaited inline so a long fleet
    # cannot stall start_polling.
    dp.startup.register(_hook("start_roster_sync", start_roster_sync))
    dp.shutdown.register(_hook("stop_roster_sync", stop_roster_sync))

    # The roster's durable half: the in-memory index above dies with the process,
    # so every membership seen in group traffic is also queued there and written
    # in batches by a background task. That is what keeps an ordinary member —
    # whom the Bot API will never list — resolvable as a whisper target after a
    # restart.
    dp.startup.register(_hook("start_persistence", start_persistence))
    dp.shutdown.register(_hook("stop_persistence", stop_persistence))

    # ── Startup hook: close chats that outlive their lifetime ──
    # The per-message check in handlers/chat.py already refuses to relay past the
    # 24-hour mark, so nothing illegal can happen; this only makes the close
    # happen on its own for a chat that goes quiet. Needs the FSM storage because
    # the same teardown has to free both partners out of ChatState.in_chat.
    dp.startup.register(_hook("start_chat_expiry", start_chat_expiry))
    dp.shutdown.register(_hook("stop_chat_expiry", stop_chat_expiry))

    # Daily purge of the append-only tables (whispers, anon messages/sessions,
    # coin ledger) — utils.retention owns the schedule and the day/forever
    # policy comes from the RETENTION_* settings.
    dp.startup.register(_hook("start_retention", start_retention))
    dp.shutdown.register(_hook("stop_retention", stop_retention))

    # ── Share references for middleware/handler access ──
    # aiogram automatically injects "bot" into handler kwargs.
    # "dp" is stored here so handlers can access it when needed.
    dp.workflow_data["dp"] = dp

    # The expiry sweep is the one startup hook that also needs the FSM storage:
    # closing a chat has to free both partners out of ChatState.in_chat, and a
    # background task has no FSMContext of its own. aiogram passes workflow_data
    # into emit_startup, which is the only way a dp.startup callback can receive
    # it (EventObserver.register takes no kwargs).
    dp.workflow_data["storage"] = storage

    return bot, dp


#: Retry budget for the very first network call. Telegram unreachable at
#: startup is nearly always a temporary condition (no internet, VPN dropped,
#: DNS still warming up), so a few spaced-out retries turn a crash into a
#: normal delayed start.
_WEBHOOK_ATTEMPTS = 5
_WEBHOOK_BACKOFF = 3.0


async def _clear_webhook(bot: Bot, logger: logging.Logger) -> bool:
    """``delete_webhook`` with retries; ``False`` if Telegram stayed unreachable.

    Only *network* errors are retried — a bad token or a Telegram-side refusal
    will not fix itself, so those go straight to the caller as ``True`` being
    never reached and the log explaining why.
    """
    for attempt in range(1, _WEBHOOK_ATTEMPTS + 1):
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            return True
        except TelegramNetworkError as exc:
            if attempt == _WEBHOOK_ATTEMPTS:
                logger.error(
                    "Could not reach api.telegram.org after %d attempts: %s",
                    _WEBHOOK_ATTEMPTS,
                    exc,
                )
                return False
            logger.warning(
                "Telegram unreachable (attempt %d/%d): %s — retrying in %.0fs.",
                attempt,
                _WEBHOOK_ATTEMPTS,
                exc,
                _WEBHOOK_BACKOFF,
            )
            await asyncio.sleep(_WEBHOOK_BACKOFF)
        except Exception as exc:
            # Bad token, Telegram error, anything else: retrying is pointless.
            logger.error("delete_webhook failed: %s", exc)
            return False
    return False


# ──────────────────────────────────────────────────────────────
# Webhook mode (RUN_MODE=webhook)
# ──────────────────────────────────────────────────────────────

#: ``setWebhook``'s ``secret_token`` may only contain ``A-Z a-z 0-9 _ -``,
#: 1-256 characters — Telegram rejects anything else with a 400, which is a
#: confusing error to hit after the server is already up.
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def build_webhook_url(base_url: str, path: str) -> str:
    """Join ``WEBHOOK_BASE_URL`` + ``WEBHOOK_PATH`` and validate the result.

    Telegram only delivers webhook updates to an **https://** origin (it
    answers ``400 bad webhook: An HTTPS URL must be provided`` for anything
    else), so a scheme that is not https is rejected here — at startup,
    with a message that says what to change — rather than as a cryptic
    Telegram error after the server is running.

    The local aiohttp server deliberately stays plain HTTP: TLS is
    terminated by the tunnel or reverse proxy in front of it (see
    ``WEBHOOK.md``), which is what makes a local test possible without
    generating a certificate.

    Raises:
        ValueError: base URL empty, not a URL, or not https.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError(
            "WEBHOOK_BASE_URL is empty — webhook mode needs the public "
            "https:// address Telegram should POST updates to. "
            "«آدرس پایهٔ وبهوک (WEBHOOK_BASE_URL) خالی است.»"
        )
    try:
        parts = urlsplit(base)
    except ValueError as exc:
        raise ValueError(f"WEBHOOK_BASE_URL is not a URL: {base!r} ({exc})") from exc
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(
            f"WEBHOOK_BASE_URL must start with https:// (got {parts.scheme or 'no scheme'!r}): "
            f"{base}. Telegram rejects plain http:// webhooks. "
            "For a local test run `cloudflared tunnel --url http://HOST:PORT` "
            "and paste the https:// address it prints — see WEBHOOK.md."
        )
    route = (path or "").strip() or "/telegram/webhook"
    if not route.startswith("/"):
        route = f"/{route}"
    return f"{base}{route}"


def webhook_secret_token() -> str:
    """The ``secret_token`` to hand to Telegram *and* verify on each request.

    A value configured in ``WEBHOOK_SECRET`` is used as-is (validated
    against Telegram's charset, because an invalid one only fails later as
    an opaque 400 from setWebhook). An empty setting generates a fresh
    random token per boot — safe because ``set_webhook`` re-registers it on
    every start, so the header Telegram sends always matches the value the
    handler expects.

    Raises:
        ValueError: configured secret contains characters Telegram forbids.
    """
    configured = (settings.webhook_secret or "").strip()
    if configured:
        if not _WEBHOOK_SECRET_RE.match(configured):
            raise ValueError(
                "WEBHOOK_SECRET must be 1-256 characters of A-Z, a-z, 0-9, "
                f"_ and - only (got {len(configured)} chars). "
                "«WEBHOOK_SECRET فقط باید حروف، عدد، _ و - داشته باشد.»"
            )
        return configured
    return secrets.token_urlsafe(24)


def build_set_webhook_kwargs(dp: Dispatcher, *, url: str, secret: str) -> dict:
    """Arguments for ``bot.set_webhook(...)`` — one place, testable offline.

    * ``drop_pending_updates=False`` is a hard rule (see the polling branch
      in ``main()``): dropping the queue would kill the only record behind
      a live inline-whisper card.
    * ``allowed_updates`` mirrors polling exactly — aiogram resolves the
      update types from the handlers that are actually registered. Without
      it Telegram falls back to its default list, which EXCLUDES
      ``chat_member`` and would silently stop the group roster and the
      onboarding card.
    * ``secret_token`` becomes the ``X-Telegram-Bot-Api-Secret-Token``
      header Telegram attaches to every delivery; the aiohttp handler
      compares it before the dispatcher ever sees the request.
    """
    return {
        "url": url,
        "secret_token": secret,
        "drop_pending_updates": False,
        "allowed_updates": dp.resolve_used_update_types(),
    }


async def _webhook_health(request: web.Request) -> web.Response:
    """``GET /health`` — a 200 the tunnel/proxy/scripts can probe."""
    return web.json_response({"status": "ok"})


def build_webhook_app(bot: Bot, dp: Dispatcher, secret_token: str) -> web.Application:
    """Build the aiohttp application: the Telegram route + ``/health``.

    Only *builds* — startup/shutdown wiring (``setup_application``) happens
    in :func:`run_webhook`, so a smoke test can mount this app and POST at
    it without triggering the bot's startup hooks (those would message real
    groups and sweep the real database).

    ``handle_in_background=True`` answers Telegram within milliseconds and
    lets the handler finish in the background; Telegram only waits 60s
    before re-delivering, and a slow reply here is otherwise indistinguishable
    from a down server.
    """
    app = web.Application()
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=True,
        secret_token=secret_token,
    )
    handler.register(app, settings.webhook_path)
    app.router.add_get("/health", _webhook_health)
    return app


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Serve updates over webhook until interrupted. Never returns normally.

    Order matters: the socket must be listening **before** ``set_webhook``
    succeeds, or Telegram starts delivering into a connection that is not
    there yet. On the way out the webhook is deliberately left registered —
    Telegram keeps retrying delivery while the process is down, and the
    next start (in either mode) re-asserts or clears it explicitly.

    Raises SystemExit(1) on configuration or registration problems, after
    cleaning the server up; ``main()``'s ``finally`` closes the session and
    the database either way.
    """
    try:
        url = build_webhook_url(settings.webhook_base_url, settings.webhook_path)
        secret = webhook_secret_token()
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error(
            "Webhook mode needs a public https:// URL. Quick local test "
            "WITHOUT any SSL setup / تست لوکال بدون SSL:\n"
            "  1) cloudflared tunnel --url http://%s:%s\n"
            "  2) copy the https://... address it prints into "
            "WEBHOOK_BASE_URL in .env, then restart the bot.\n"
            "Details: WEBHOOK.md",
            settings.webapp_host,
            settings.webapp_port,
        )
        raise SystemExit(1)

    app = build_webhook_app(bot, dp, secret)
    # Wires dp.startup/dp.shutdown (keyboard sweep, roster sync, retention,
    # chat expiry) and the handler's session close to the app lifecycle.
    # Must be called AFTER build_bot_and_dispatcher(): workflow_data is
    # captured by value here.
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, settings.webapp_host, settings.webapp_port)
    await site.start()
    logger.info(
        "Webhook server listening on http://%s:%s%s",
        settings.webapp_host,
        settings.webapp_port,
        settings.webhook_path,
    )

    try:
        await bot.set_webhook(**build_set_webhook_kwargs(dp, url=url, secret=secret))
    except TelegramNetworkError as exc:
        log_connection_advice(f"set_webhook failed: {type(exc).__name__}: {exc}", bot)
        await runner.cleanup()
        raise SystemExit(1)
    except Exception as exc:
        logger.error("set_webhook rejected by Telegram: %s", exc)
        logger.error(
            "Common causes / علل رایج:\n"
            "  * URL must be https:// and reachable from the internet — "
            "Telegram refuses plain http:// (use a tunnel for local tests).\n"
            "  * When Telegram connects DIRECTLY, only ports 443, 80, 88 and "
            "8443 are accepted; a tunnel or reverse proxy hides any port.\n"
            "  * WEBHOOK_SECRET characters must be A-Z a-z 0-9 _ - only.\n"
            "Details: WEBHOOK.md"
        )
        await runner.cleanup()
        raise SystemExit(1)

    logger.info("Webhook registered: %s (pending updates NOT dropped).", url)
    logger.info(
        "Health check: http://%s:%s/health",
        settings.webapp_host,
        settings.webapp_port,
    )

    try:
        # Run until the task is cancelled: Ctrl+C raises KeyboardInterrupt
        # inside the event loop, asyncio.run then cancels this task, and
        # the finally below still runs (a second cancel never arrives, so
        # the awaits inside it complete). handle_signals=False keeps that
        # single story on every platform — aiohttp's SIGINT handler would
        # otherwise raise GracefulExit on POSIX and KeyboardInterrupt on
        # Windows, i.e. two different shutdown paths to maintain.
        await asyncio.Future()
    finally:
        # NOT bot.delete_webhook(): Telegram retries failed deliveries for
        # a while, so a quick restart picks the queue back up instead of
        # losing whatever arrived during the gap. Switching to polling
        # clears it explicitly on the next start anyway.
        await runner.cleanup()


async def register_commands(bot, *, whisper_on: bool, admin_ids) -> None:
    """Publish the Telegram ``/`` menu — in private chats only.

    A group gets NO commands. Typing ``/`` in a group used to pop up
    ``/start@bot /help@bot /w@bot /inline@bot /fix_keyboard@bot``, which has
    nothing to do with whatever the room was actually discussing. Getting rid of
    that needs two separate things, and doing only the first leaves the popup in
    place:

    * the private list is scoped to ``AllPrivateChats``. An unscoped
      ``set_my_commands`` means *everywhere*, so it would keep surfacing in
      groups no matter what the group scope said.
    * the group and default scopes are **deleted**, not overwritten with a short
      list. Telegram picks a chat's commands from the highest-priority scope that
      is set, then falls back down the chain; and because ``set_my_commands``
      replaces a scope wholesale, any scope an earlier run filled stays filled
      until something clears it. Deleting is the only instruction that reliably
      leaves a scope empty, and it is idempotent — every start re-asserts it, so
      a bot upgraded from the old behaviour cleans up after itself.

    Args:
        whisper_on: whether the whisper commands are in the menus at all.
        admin_ids: chats that also get ``/admin``.

    Never raises. A menu that failed to publish is a cosmetic problem, and it
    used to abort the rest of startup behind a broad ``except``.
    """
    try:
        await bot.set_my_commands(
            [
                BotCommand(command=cmd, description=usage)
                for cmd, usage in private_command_rows(whisper=whisper_on)
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        await bot.delete_my_commands(scope=BotCommandScopeDefault())

        for admin_id in admin_ids:
            await bot.set_my_commands(
                [
                    BotCommand(command=cmd, description=usage)
                    for cmd, usage in admin_command_rows(whisper=whisper_on)
                ],
                # Keyword arg, not positional: pydantic models reject positional
                # init ("__init__() takes 1 positional argument"), and that
                # exception used to abort this whole block with a WARNING on
                # every start.
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except Exception as exc:
        logger.warning("Could not register commands: %s", exc)


async def main() -> None:
    _setup_logging()
    _install_stop_signal()

    # ── Ephemeral-host safety rail ──
    # A Cloudflare container's filesystem is ephemeral: a SQLite file written
    # there is gone the moment the platform stops the instance, i.e. the whole
    # user database silently resets on the first idle sleep. Refusing to start
    # is the only honest answer — set DATABASE_URL to a managed PostgreSQL
    # (store it as the platform's DATABASE_URL environment variable) instead.
    if settings.in_cloudflare_container and settings.database_dialect == "sqlite":
        logger.error(
            "Running inside a Cloudflare container with a SQLite database: "
            "the container filesystem is EPHEMERAL, so database.db would be "
            "wiped on the next stop/sleep. Aborting. "
            "Set DATABASE_URL to postgresql+asyncpg://… as a platform "
            "environment variable."
        )
        raise SystemExit(1)

    # A managed host assigns PORT and usually has no persistent disk either
    # (e.g. Render's free plan). SQLite there loses every user on redeploy or
    # restart — warn loudly, but do not abort: an operator may know what they
    # are doing (a paid plan with a mounted disk, or a throwaway test).
    if settings.port and settings.database_dialect == "sqlite":
        logger.warning(
            "PORT is set (a managed host) but DATABASE_URL is SQLite. Most "
            "hosts wipe the filesystem between deploys/sleeps, so user data "
            "will be lost — use managed PostgreSQL in production."
        )

    # ── Database ──
    await init_db()
    logger.info("Database initialized (%s).", settings.database_dialect)

    if not settings.bot_token or settings.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN is not set in .env. Aborting.")
        return

    # ── Bot + Dispatcher ──
    bot, dp = await build_bot_and_dispatcher()

    # ── Command menu ──
    await register_commands(
        bot,
        whisper_on=(await get_whisper_config()).enabled,
        admin_ids=settings.admin_ids_list,
    )

    # ── Drop any stale webhook, then serve updates ──
    #
    # ``drop_pending_updates=False`` on purpose (both modes). A
    # ``chosen_inline_result``
    # that arrives while the bot is offline is the ONLY record of the row
    # behind a live inline-whisper card — Telegram posts the card the moment
    # the sender picks it, bot or no bot. Dropping pending updates (the old
    # behaviour) discarded that record, the card stayed in the group with a
    # button that had nothing behind it, and the receiver got exactly
    # «این نجوا دیگر قابل نمایش نیست». Replaying ordinary pending updates on
    # startup is harmless; losing a chosen result never is.
    #
    # This call is the first one that MUST reach the network, so it doubles as
    # the reachability probe. A dead network used to escape from here as an
    # unhandled ``TelegramNetworkError``: a wall of traceback, no polling, and
    # an aiohttp session left open ("Unclosed client session"). It is now
    # retried with a backoff; if Telegram still cannot be reached the bot
    # falls back to a direct connection (when a proxy was in play), then
    # exits cleanly with a troubleshooting block instead of a stack trace.
    # Webhook mode runs it too: clearing the old registration both probes the
    # network and guarantees no stale URL survives a mode/config change.
    reachable = await _clear_webhook(bot, logger)

    if not reachable and await _fallback_to_direct(bot):
        logger.warning(
            "PROXY_URL did not reach Telegram — retrying WITHOUT the proxy "
            "(direct connection to api.telegram.org).",
        )
        reachable = await _clear_webhook(bot, logger)

    if not reachable:
        log_connection_advice("delete_webhook kept failing after all retries", bot)
        await bot.session.close()
        raise SystemExit(1)

    logger.info("Telegram reachable; stale webhook cleared.")

    try:
        if settings.webhook_mode:
            await run_webhook(bot, dp)
        else:
            logger.info("Starting long polling...")
            await dp.start_polling(bot)
    except TelegramNetworkError as exc:
        # Polling runs for days: the proxy can die mid-run (VPN dropped, the
        # proxy process was closed, the network changed). Same treatment as a
        # failed start — advice, clean shutdown, non-zero exit code.
        log_connection_advice(f"updates stopped: {type(exc).__name__}: {exc}", bot)
        raise SystemExit(1)
    finally:
        await bot.session.close()
        # Close the FSM storage too: a RedisStorage holds a connection pool
        # that would otherwise be left open on shutdown (and a container
        # stop/SIGTERM would leak it). MemoryStorage.close() is a no-op.
        try:
            await dp.storage.close()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Closing the FSM storage failed: %s", exc)
        # Flush/close every pooled SQLite connection (WAL files checkpoint on
        # close). Without this the process could exit with -wal/-shm siblings
        # still held open.
        try:
            await engine.dispose()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Disposing the database engine failed: %s", exc)
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        # KeyboardInterrupt: Ctrl+C — main()'s own ``finally`` already
        # reports the stop, and a second line used to print "Bot stopped."
        # twice on every Ctrl+C.
        # CancelledError: the SIGTERM handler cancelled main() so the SAME
        # finally could run (see _install_stop_signal) — a container stop
        # must look exactly like an interrupted local run.
        # ``SystemExit`` is deliberately NOT caught: a connectivity failure
        # exits with code 1 (no traceback) so scripts can detect it.
        pass
