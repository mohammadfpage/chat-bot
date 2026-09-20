from handlers.start import router as start_router
from handlers.profile import router as profile_router
from handlers.chat import router as chat_router
from handlers.admin import router as admin_router
from handlers.anonymous import router as anonymous_router

__all__ = [
    "start_router",
    "profile_router",
    "chat_router",
    "admin_router",
    "anonymous_router",
]
