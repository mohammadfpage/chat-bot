from aiogram.fsm.state import State, StatesGroup


class ProfileSetup(StatesGroup):
    """Step-by-step profile completion flow."""
    waiting_for_age = State()
    waiting_for_city = State()
    waiting_for_gender = State()
    waiting_for_height = State()
    waiting_for_confirm = State()
    waiting_for_photo_choice = State()
    waiting_for_photo = State()


class ProfileEdit(StatesGroup):
    """Editing ONE field of an already-complete profile.

    One state per editable field (not a re-run of :class:`ProfileSetup`), so the
    text or photo the user sends lands on exactly the field they picked in the
    «ویرایش پروفایل» menu. The setup wizard's states cannot be reused here: they
    chain age → city → gender → height and would force the user through every
    step just to change one value.
    """

    waiting_for_age = State()
    waiting_for_city = State()
    waiting_for_gender = State()
    waiting_for_height = State()
    waiting_for_photo = State()


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
    # Special flags (VIP / subscription / exempt)
    waiting_for_flags_id = State()


class AdminGift(StatesGroup):
    """Admin gifting flow: pick type → pick WHO → enter id/count → amount.

    State data:
        gift_type: str       - "coins" | "premium"
        target_scope: str    - "one" (a single telegram id), "all" (every
                               active user) or "random" (n sampled users)
        target_id: int       - the recipient's telegram_id (scope "one")
        recipient_count: int - how many users to sample (scope "random")
        amount: float|int    - the per-recipient amount (bulk scopes only,
                               parked between the amount prompt and the
                               confirmation tap)
    """
    waiting_for_user_id = State()
    waiting_for_count = State()
    waiting_for_amount = State()
    confirm_send = State()


class AdminPolicyEdit(StatesGroup):
    """Admin editing a single bot-policy numeric field.

    State data:
        field:   str - the ``bot_policy`` column being written
        section: str - which screen sent us here (``costs`` / ``rewards`` /
                 ``limits``), so the saved value returns the admin to the
                 section they were working in instead of a generic menu
    """
    waiting_for_value = State()


class AdminReport(StatesGroup):
    """Admin looking up ONE user inside the coin/referral report.

    ``waiting_for_user_id``  a numeric Telegram ID whose ledger card is wanted
    """

    waiting_for_user_id = State()


class SupportStates(StatesGroup):
    """User composing a message to the support team.

    ``waiting_for_message`` — the «🎧 پشتیبانی» screen asked for text; the next
    message the user sends becomes a new ticket (or a follow-up to their open
    one).
    """

    waiting_for_message = State()


class AdminSupport(StatesGroup):
    """Admin composing a reply to one support ticket.

    State data:
        ticket_id: int - which ticket the next typed text answers
    """

    waiting_for_reply = State()


class AnonChatStates(StatesGroup):
    """FSM for hybrid anonymous chat sessions.
    
    State data:
        partner_id: int  - The other user's telegram_id
        is_owner: bool   - True if this user is the link owner (receiver)
    """
    in_session = State()


class WhisperStates(StatesGroup):
    """FSM for the group "whisper" (نجوا) flow.

    Both states park a half-written whisper in the *private-chat* storage key
    (chat_id == user_id), because that is where the "عضو شدم" button and the
    follow-up message are received. A state set inside a group is keyed by the
    group and would be invisible in the DM.

    ``waiting_for_join``  — the user must join the admin's required
        channel/group first; press "عضو شدم" to resume.
    ``waiting_for_body``  — the target is known, the message body is not yet.
        The next text / photo / voice sent in the DM becomes the whisper.

    Shared state data:
        pending_receiver_id:      int  - who the whisper is for
        pending_receiver_name:    str  - their display name
        pending_receiver_username: str - their @username, if any
        pending_chat_id:          int  - the group it was written in
        pending_chat_title:       str  - the group title
        pending_kind:             str  - "text" | "photo" | "voice"
        pending_text:             str  - text body / photo caption
        pending_file_id:          str  - Telegram file_id (media only)
        pending_whisper_id:       int  - already-created whisper to open
                                    (receiver side of the flow)
    """
    waiting_for_join = State()
    waiting_for_body = State()


class AdminWhisper(StatesGroup):
    """Admin flows for the force-join (عضویت اجباری) section.

    ``waiting_for_channel`` captures the NEXT channel to append to the
        multi-channel ``required_channels`` list: a numeric id, an @username,
        a t.me invite link, or a message forwarded from that channel/group.
        It adds a row instead of overwriting one field, so it can be driven
        repeatedly to build a list of N.

    (The old ``waiting_for_chat`` state served the single legacy forced-join
    chat. That field is migrated into ``required_channels`` on first open of
    the force-join section, so the second capture path is gone with it.)

    ``waiting_for_max_length`` captures a new ``WhisperConfig.max_length``
        (characters per whisper) typed by the admin in the whisper panel.
    """

    waiting_for_channel = State()
    waiting_for_max_length = State()
