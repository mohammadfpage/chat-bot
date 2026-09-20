from keyboards.reply import (
    main_menu_kb,
    queue_menu_kb,
    chat_menu_kb,
    age_kb,
    city_kb,
    height_kb,
    confirm_profile_kb,
    admin_dual_panel_kb,
    anonymous_chat_menu_kb,
)
from keyboards.inline import (
    force_join_kb,
    blocked_list_kb,
    unread_inbox_kb,
    read_message_kb,
)
from keyboards.admin import (
    BACK_TO_PANEL,
    admin_panel_kb,
    admin_stats_kb,
    broadcast_confirm_kb,
    admin_users_kb,
    admin_manage_admins_kb,
    admin_cancel_kb,
)
from keyboards.user import (
    main_menu_inline_kb,
    welcome_inline_kb,
)

__all__ = [
    # reply
    "main_menu_kb",
    "queue_menu_kb",
    "chat_menu_kb",
    "age_kb",
    "city_kb",
    "height_kb",
    "confirm_profile_kb",
    "admin_dual_panel_kb",
    "anonymous_chat_menu_kb",
    # inline
    "force_join_kb",
    "blocked_list_kb",
    "unread_inbox_kb",
    "read_message_kb",
    # admin inline
    "BACK_TO_PANEL",
    "admin_panel_kb",
    "admin_stats_kb",
    "broadcast_confirm_kb",
    "admin_users_kb",
    "admin_manage_admins_kb",
    "admin_cancel_kb",
    # user inline
    "main_menu_inline_kb",
    "welcome_inline_kb",
]
