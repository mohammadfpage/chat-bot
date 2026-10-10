from handlers.navigation import router as navigation_router
from handlers.keyboard_fix import router as keyboard_fix_router
from handlers.group_lifecycle import router as group_lifecycle_router
from handlers.start import router as start_router
from handlers.profile import router as profile_router
from handlers.chat import router as chat_router
from handlers.admin import router as admin_router
from handlers.anonymous import router as anonymous_router
from handlers.whisper import router as whisper_router
from handlers.inline_anon import router as inline_anon_router
from handlers.anon_chat import router as anon_chat_router
from handlers.support import router as support_router

__all__ = [
    "navigation_router",
    "keyboard_fix_router",
    "group_lifecycle_router",
    "start_router",
    "profile_router",
    "chat_router",
    "admin_router",
    "anonymous_router",
    "whisper_router",
    "inline_anon_router",
    "anon_chat_router",
    "support_router",
]
