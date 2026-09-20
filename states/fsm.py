from aiogram.fsm.state import State, StatesGroup


class ProfileSetup(StatesGroup):
    """Step-by-step profile completion flow."""
    waiting_for_age = State()
    waiting_for_city = State()
    waiting_for_height = State()
    waiting_for_confirm = State()


class ChatState(StatesGroup):
    """Three-state machine for chat lifecycle."""
    idle = State()
    in_queue = State()
    in_chat = State()


class AdminBroadcast(StatesGroup):
    """Admin broadcast flow with confirmation."""
    waiting_for_message = State()
    confirm_send = State()


class AdminPIS(StatesGroup):
    """
    Admin "personal input" flows: each action asks for a numeric Telegram ID
    or a channel username and then commits the change.
    """
    # Manage admins
    waiting_for_promote_id = State()
    waiting_for_demote_id = State()
    # Ban / unban
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()


class AnonymousStates(StatesGroup):
    """FSM for the asynchronous anonymous message flow."""
    waiting_for_message = State()


class AnonChatStates(StatesGroup):
    """FSM for hybrid anonymous chat sessions.
    
    State data:
        partner_id: int  - The other user's telegram_id
        is_owner: bool   - True if this user is the link owner (receiver)
    """
    in_session = State()
