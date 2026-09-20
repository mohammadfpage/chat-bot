"""
Anonymous Chat Bot – Main Entry Point (Long Polling)
=====================================================
Initializes the Bot and Dispatcher, applies middleware, registers routers,
clears any stale webhook, and runs long polling for simple local execution.
"""

import logging
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database import init_db
from middleware import BlockBannedMiddleware, ForceJoinMiddleware


async def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    """Create the Bot and Dispatcher, register routers + middleware."""
    # ── FSM storage ──
    # Production: swap MemoryStorage with RedisStorage for multi-worker support:
    #   from aiogram.fsm.storage.redis import RedisStorage
    #   storage = RedisStorage.from_url("redis://localhost:6379")
    storage = MemoryStorage()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # ── Middleware (order matters: ban-block → force-join) ──
    dp.message.outer_middleware(BlockBannedMiddleware())
    dp.callback_query.outer_middleware(BlockBannedMiddleware())
    dp.message.outer_middleware(ForceJoinMiddleware())
    dp.callback_query.outer_middleware(ForceJoinMiddleware())

    # ── Routers (specific → general) ──
    from handlers import (
        admin_router,
        anonymous_router,
        chat_router,
        profile_router,
        start_router,
    )
    dp.include_router(admin_router)
    dp.include_router(anonymous_router)
    dp.include_router(chat_router)
    dp.include_router(profile_router)
    dp.include_router(start_router)

    # ── Share references for middleware/handler access ──
    # aiogram automatically injects "bot" into handler kwargs.
    # "dp" is stored here so handlers can access it when needed.
    dp.workflow_data["dp"] = dp

    return bot, dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    logger = logging.getLogger(__name__)

    # ── Database ──
    await init_db()
    logger.info("Database initialized.")

    if not settings.bot_token or settings.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN is not set in .env. Aborting.")
        return

    # ── Bot + Dispatcher ──
    bot, dp = await build_bot_and_dispatcher()

    # ── Drop any stale webhook, then poll for updates ──
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook cleared. Starting long polling...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        import asyncio

        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped.")
