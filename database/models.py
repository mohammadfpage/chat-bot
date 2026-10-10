"""
SQLAlchemy ORM models.
User metadata + economy/economy policy.
Bot runtime policy (rate limits, matching costs, rewards) lives in the
``bot_policy`` table so admins can tune it live from the panel.

Currency is COINS (سکه) and nothing else. There used to be a second unit —
"tokens" — that was charged per message. It is gone: tokens are no longer a
column, a grant function, or a word in any user-facing string.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # ── Profile fields ──
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    height: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_profile_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    #: ``"male"`` | ``"female"`` | ``None`` (not yet declared).
    #:
    #: Required by «چت با دختر» / «چت با پسر»: those buttons match a user against
    #: the OPPOSITE gender, so a user who never declared one can never be matched
    #: by anybody — they would sit in the queue forever with no explanation. It
    #: is therefore part of profile setup, not an optional extra.
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    # ── Profile photo (privacy choice) ──
    # show_profile_photo:  whether a photo is shown on the profile card
    # profile_photo:       stored file_id of a user-sent photo (None = use the
    #                      latest Telegram profile photo dynamically)
    show_profile_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_photo: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # ── Moderation (admin-only global ban) ──
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Economy: coins (the ONLY currency) ──
    #
    # One unit, one balance. The old second unit ("tokens") was charged per
    # message sent; it has been removed from the schema entirely, so there is no
    # column, no refill timer and no conversion rate left to keep in sync.
    #
    # ``Float``, not ``Integer``: a service price may be set to 0.5, so a
    # balance has to be able to hold one. ``utils.economy.round_coins`` keeps
    # every stored value canonical at two decimals.
    coins: Mapped[float] = mapped_column(Float, default=0)

    # ── Paywall ──
    #
    # Charging is per MATCH, not per message: connecting costs
    # ``BotPolicy.chat_girl_cost`` / ``chat_boy_cost`` coins and talking is then
    # free until the chat's 24 hours are up. Three flags plus the balance decide
    # whether a match costs anything, and all three are admin-controlled:
    #
    #   is_exempt         admin override — every match is free for this user,
    #                     whatever their balance says
    #   has_subscription  the user has bought unlimited matches
    #   coins             the pay-as-you-go balance
    #
    # See ``utils.economy.check_and_deduct_balance`` — the single gate every
    # entry point goes through.
    has_subscription: Mapped[bool] = mapped_column(Boolean, default=False)
    is_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    last_daily_bonus: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # ── Referral system ──
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_count: Mapped[int] = mapped_column(Integer, default=0)

    # ── Premium / subscription ──
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # ── Metadata ──
    registered_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<User {self.telegram_id} ({self.first_name})>"


class BlockList(Base):
    """Peer-to-peer block records.

    When User A blocks User B, a row is inserted with
    ``blocker_id=A`` and ``blocked_id=B``.  During matching,
    the engine skips pairs where either side has a block record.
    """

    __tablename__ = "block_list"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    blocker_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    blocked_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<BlockList {self.blocker_id} -> {self.blocked_id}>"


class UserReport(Base):
    """A «🛑 گزارش کاربر / بلاک» event, kept for the admin inbox.

    The live notification still goes to every admin's PM; this row is what
    makes reports browsable afterwards instead of scrolling chat history.
    One row per block event — re-blocking after an unblock is a new report.
    """

    __tablename__ = "user_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reporter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reported_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<UserReport {self.reporter_id} -> {self.reported_id}>"


class SupportTicket(Base):
    """A two-way «🎧 پشتیبانی» conversation between one user and the team.

    Unlike :class:`UserReport` (a one-shot block event), a ticket is
    ANSWERABLE: messages travel both ways and the panel can tell at a glance
    which tickets still need a reply.

    ``status`` is the whole routing contract:

        ``open``      the user wrote last — waiting for an admin answer
        ``answered``  the team replied; the user may still follow up
        ``closed``    resolved and filed away; a new message opens a NEW ticket

    ``updated_at`` moves on every message, so the panel list can sort by
    "most recently touched" instead of creation order.
    """

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<SupportTicket {self.id} user={self.user_id} status={self.status}>"


class SupportMessage(Base):
    """One message inside a :class:`SupportTicket` (user- or admin-authored)."""

    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[str] = mapped_column(String(4096), nullable=False)
    #: False until the recipient opens the ticket; the panel shows unread user
    #: messages in bold so an admin can see what still needs attention.
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<SupportMessage {self.id} ticket={self.ticket_id} "
            f"admin={self.is_admin}>"
        )


class AnonymousContact(Base):
    """Bidirectional anonymous contact record created via deep links.

    When user A shares a link and user B clicks it, two rows are created:
      - owner_id=A, guest_id=B  (A's inbox entry)
      - owner_id=B, guest_id=A  (B's inbox entry)
    """

    __tablename__ = "anonymous_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    guest_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AnonymousContact {self.owner_id} <-> {self.guest_id} ({self.title})>"


class AnonymousMessage(Base):
    """Hybrid anonymous message for async/real-time chat system.

    Messages are stored when the receiver is NOT in an active session.
    When both users are in session, messages route in real-time (not stored).
    """

    __tablename__ = "anonymous_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    receiver_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(4096), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_owner_replying: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AnonymousMessage {self.id} from {self.sender_id} to {self.receiver_id} read={self.is_read}>"


class AnonChatSession(Base):
    """A real-time 1-on-1 anonymous chat between exactly two users.

    Created by the «درخواست پیام ناشناس» card: user A publishes the card in a
    group, user B presses «شروع چت ناشناس» and the row is written PENDING.
    Only when A presses «✅ قبول چت» does it become ACTIVE — and that is the
    moment the pair ``{A: B, B: A}`` starts relaying messages.

    Roles are stored as they happened, NOT sorted: ``requester_id`` is whoever
    published the card. Both directions of a session are found by matching either
    column (see :func:`utils.anon_sessions._either`), so a user cannot be in two
    live conversations with the same person — ``open_request`` returns the
    existing open row instead of writing a second one. Sorting the two ids would
    make the lookup cheaper by one index and would also be wrong: the accept
    button is only ever rendered in the publisher's DM, so for every pair whose
    ids sort the "wrong" way, the publisher could not accept their own request.

    Fields:
        token:         short hex id carried by the «قبول چت»/«رد» buttons.
                       A token rather than the raw user id because a user may
                       legitimately have several requests in flight, and the
                       buttons travel as ``callback_data`` (64-byte ceiling).
        requester_id:  user A — the one who published the card
        partner_id:    user B — the one who pressed the button
        status:        ``pending`` | ``active`` | ``declined`` | ``ended``
        created_at:    when B pressed the button
        activated_at:  when A accepted. Fixed for the life of the row — it is
                       the expiry anchor, and activity never pushes it forward
                       (see ``anon_sessions._is_stale``)
        ended_at:      when the session was declined or closed
    """

    __tablename__ = "anon_chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, index=True
    )

    requester_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AnonChatSession {self.token} {self.requester_id}<->{self.partner_id} "
            f"status={self.status}>"
        )


class ChatPair(Base):
    """Durable mirror of the matching chat's in-memory state.

    ``handlers/chat.py`` keeps ``pair_map`` / ``search_queue`` / ``last_mode``
    in RAM for speed — and on a laptop that is fine, because a restart is a
    rare deliberate act. On Cloudflare Containers the process is stopped and
    restarted by the platform (idle sleep, deploy, crash), so a RAM-only pair
    means two people mid-conversation are silently unpaired and nobody is
    told. This table is the write-through copy ``chat.py`` keeps so
    ``rebuild_chat_state()`` can put the queue and the live pairs back on
    wake.

    Shape — deliberately ONE row per user, upserted, never deleted:

        user_id     the owner of this row (PK, no autoincrement: it IS the id)
        partner_id  who they are paired with right now, NULL otherwise
        status      ``queued`` | ``paired`` | ``ended``
        mode        the matching mode they last searched with
                    (NULL = اتصال شانسی / random), kept even after ``ended``
                    so «🔍 چت بعدی» repeats the right choice across restarts
        gender      snapshot of ``users.gender`` at queue time — the queue
                    stores it so the matcher never has to re-read a row per
                    candidate, and the rebuild needs it for the same reason
        opened_at   Unix epoch (seconds, written from Python) of the moment
                    the pair was made — the expiry clock, restored as
                    ``chat_opened_at`` so a restarted 23-hour-old chat still
                    ends on time. Epoch rather than ``func.now()`` because
                    SQLite and PostgreSQL disagree about what "now" means
                    (naive local vs. timezone-aware), and the whole point of
                    the column is one absolute instant both sides agree on.

    Not retained: ``last_partner`` (who you just talked to) is deliberately
    NOT rebuilt — a blocked or vanished partner would resurrect a «اتصال
    مجدد» button that can only answer "no". ``rematch_offers`` /
    ``_rematch_declined`` are likewise forgotten, which is the documented
    restart behaviour they already had.

    Rows are bounded by one-per-user and are NOT part of the retention purge:
    an ``ended`` row costs ~60 bytes and carries the mode history a returning
    user needs.
    """

    __tablename__ = "chat_pairs"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    partner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="ended", index=True
    )
    mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)
    opened_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ChatPair {self.user_id} -> {self.partner_id} "
            f"status={self.status}>"
        )


class Whisper(Base):
    """A private message ("نجوا") addressed to ONE member of a group.

    The content is NEVER published in the group. The bot only posts a card
    that mentions ``receiver_id`` and carries a "view" button; pressing it
    delivers the content in the receiver's private chat. Everyone else who
    taps the button is rejected.

    Fields:
        sender_id:      who wrote the whisper (never revealed to the group)
        receiver_id:    the only user allowed to open it
        chat_id/chat_title: the group the card was published in
        card_message_id: message id of that card, so the "read" state can be
                    written back no matter where the button was pressed
        notice_message_id: message id of the PRIVATE notice sent to the
                    receiver («یک پیام مخفی دارید»). Kept so the first read can
                    edit that notice into «پیام خوانده شد» instead of leaving a
                    stale "you have a message" alert on their phone forever.
                    NULL when the receiver never started the bot, which is
                    exactly the case the sender gets told about instead.
        content:        text body (None when the whisper carries media)
        media_type:     "photo" | "voice" | None
        media_file_id:  reusable Telegram file_id for the media
        is_viewed:      set to True the first time the receiver opens it
        snoopers:       JSON list of everyone who pressed the card's read
                    button WITHOUT being the sender or the receiver — one
                    ``{"user_id", "name", "at"}`` dict per press, de-duplicated
                    by ``user_id``
    """

    __tablename__ = "whispers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    receiver_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    card_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    notice_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )

    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)

    is_viewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    snoopers: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<Whisper {self.id} {self.sender_id}->{self.receiver_id} "
            f"chat={self.chat_id} viewed={self.is_viewed}>"
        )


class InlineWhisper(Base):
    """A "نجوا" composed through INLINE mode and read via a callback alert.

    The secret text never enters any chat: the group only ever holds a
    neutral placeholder card with a ``whisper:<token>`` button, and the
    content is revealed exclusively through a private ``show_alert`` modal
    rendered on the recipient's own client.

    Fields:
        token:              the whisper id baked into ``callback_data`` as
                            ``read_whisper:<token>`` (32 hex chars, i.e.
                            ``uuid4().hex``; ``callback_data`` is capped at 64
                            bytes and 32 is this column's width). The row is
                            committed when the inline query is ANSWERED, not
                            when the result is chosen — see
                            ``handlers/inline_anon.py::_reserve_row``
        sender_id:          who typed the inline query (never revealed)
        target_id:          the ONLY user allowed to open it
        secret_text:        the payload
        delivery:           "group" (read via the card) | "dm" (delivered
                            straight into the target's private chat)
        inline_message_id:  the card, set on the chosen-result update; NULL
                            while the row is an un-picked reservation, which
                            is also how abandoned rows are identified
        is_active:          False until the send clears validation, forced-join
                            and the economy gate; an inactive row cannot be read
                            (and is not charged)
        is_viewed:          set the first time the recipient opens it
        target_viewed:      set the first time THE TARGET opens it — narrower
                            than ``is_viewed``, which either party sets. This
                            is the flag that unlocks the card's action buttons
                            («📊 آمار» / «⚙️ گزینه‌ها» / «🗑️ حذف» / «↩️ پاسخ»):
                            a whisper that has not been opened by the person it
                            was written for carries the read button and nothing
                            else, so a card nobody has claimed yet does not
                            advertise snooper statistics or a delete button to
                            the whole group
        snoopers:           JSON list of everyone who pressed the card's read
                            button WITHOUT being the sender or the target — one
                            ``{"user_id", "name", "at"}`` dict per press,
de-duplicated by ``user_id``. This is what the
                             card's «📊 آمار» button reports back to the two
                             parties, so a card nobody else is allowed to open
                             still tells them who tried.

    .. note::
       There is deliberately **no** ``notice_message_id`` here. Inline cards are
       published by Telegram itself, so there is no send step that can fail and
       leave a row stranded — which was the only reason the ``/نجوا`` road kept
       a private notice (see :class:`Whisper`). The recipient is told nothing
       privately; the group card is the entire delivery.
    """

    __tablename__ = "inline_whispers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )

    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    secret_text: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[str] = mapped_column(String(8), nullable=False, default="group")
    inline_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_viewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    target_viewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    snoopers: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<InlineWhisper {self.token} {self.sender_id}->{self.target_id} "
            f"delivery={self.delivery} active={self.is_active} "
            f"viewed={self.is_viewed} target_viewed={self.target_viewed}>"
        )


class GroupChat(Base):
    """A group/supergroup the bot has actually interacted with.

    The reply-keyboard sweep in ``utils/group_cleanup.py`` used to discover
    groups only through ``whispers.chat_id`` / ``whisper_config`` — so a group
    that had never produced a whisper (a fresh group the bot was just added
    to) was invisible to it, and a keyboard left behind by an older build
    stayed stuck at the bottom for every member. Remembering every group the
    bot sees closes that hole: first sight registers the row, and every later
    startup sweep includes it.
    """

    __tablename__ = "group_chats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<GroupChat {self.chat_id} ({self.title!r})>"


class GroupRoster(Base):
    """Who we have seen inside which group — the durable half of the roster.

    ``utils.group_members`` answers "is this person in a group the bot can hear"
    from memory, which is fast and free but dies with the process. That left a
    gap the Bot API cannot fill any other way: it exposes no listing for ordinary
    group members, so ``getChatAdministrators`` yields only admins and a
    ``chat_member`` update only reaches a promoted bot. Every ordinary member who
    was already in the group before the bot arrived was therefore unresolvable,
    which is what made whispering to a non-admin look broken.

    This table closes it passively: every group message proves its author is in
    that group, so the roster is filled by traffic alone (see
    ``middleware.group_member_watch``). Surviving a restart is the point — a
    target the bot can only resolve until the next restart is not really
    resolvable.

    Deliberately NOT the ``users`` table. That table means "has a private
    conversation with this bot" and carries the economy, ban and referral state
    attached; writing every group chatter into it would make
    ``utils.target_lookup.has_started_bot`` — defined as *a row exists here* —
    answer yes for people who never pressed /start, silently dropping the
    "recipient has not started the bot" caveat and misrouting the DM fallback.

    One row per (user, chat) rather than per user: membership is a property of a
    pair, and it is that pair which ``recipient_verdict`` needs as evidence.
    """

    __tablename__ = "group_roster"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    username: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<GroupRoster {self.user_id} in {self.chat_id} ({self.username!r})>"


class WhisperConfig(Base):
    """Singleton row (id=1) with whisper feature settings.

    Fields:
        enabled:                master switch for the whole feature
        require_join:           enforce the forced-join requirement below
        required_chat_id:       numeric id of the channel/group members must join
        required_chat_username: public @username of that chat (for the join link)
        required_chat_title:    human title, shown on the join button
        max_length:             max characters per whisper
        daily_limit:            DEPRECATED and no longer enforced. How many
                                whispers a user may send per 24h used to be the
                                whole access rule; it is now a coin balance
                                (:class:`User.coins`, gated by
                                ``utils.economy.check_and_deduct_balance``) with
                                an admin-set exemption
                                (:attr:`User.is_exempt`) and a subscription
                                (:attr:`User.has_subscription`) on top.

                                The column is KEPT so the schema stays
                                back-compatible with an existing database and so
                                an admin who had tuned a number can see what it
                                used to be instead of silently losing it. It is
                                read by nothing that can refuse a send. Dropping
                                it is a one-line migration once no deployed
                                database needs the old value back.
    """

    __tablename__ = "whisper_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    require_join: Mapped[bool] = mapped_column(Boolean, default=True)

    required_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    required_chat_username: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    required_chat_title: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    max_length: Mapped[int] = mapped_column(Integer, default=700)
    daily_limit: Mapped[int] = mapped_column(Integer, default=10)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<WhisperConfig enabled={self.enabled} require_join={self.require_join} "
            f"chat={self.required_chat_username or self.required_chat_id}>"
        )


class RequiredChannel(Base):
    """One channel/group a user must be inside before the bot serves them.

    This is the multi-channel form of the forced-join requirement. The older
    single-chat fields on :class:`WhisperConfig` (``required_chat_id`` and
    friends) are still honoured — :func:`utils.membership.required_chats`
    unions this table with those and with the ``.env`` channel — so an admin
    who configured one before this table existed does not silently lose it.
    Existing config therefore keeps working, and this table is the way to
    reach N channels.

    Fields:
        chat_id:      the real Telegram id; UNIQUE, so the same channel can
                      never be added twice by two different admins
        username:     public @username when the chat has one, used to build
                      the join link
        title:        human title shown on the join button
        link:         a full invite URL the admin supplied, kept verbatim
                      when it is not derivable from ``username`` (private
                      channels, paid invite links, …)
        position:     display order in the admin list and in the join prompt
        is_active:    soft switch so a channel can be retired without losing
                      its row
    """

    __tablename__ = "required_channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)

    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<RequiredChannel {self.chat_id} {self.title!r} active={self.is_active}>"


class BotPolicy(Base):
    """Singleton row of tunable bot policy (rate limits, costs, rewards).

    Admin edits these values from the admin panel — the row is lazily
    created on first access by ``utils.economy.get_policy()``.

    Every number a user can feel lives here, not in a constant. Nothing in this
    class is hardcoded into a handler: the matching price, the chat lifespan and
    the referral reward are all read at the moment they are charged or shown, so
    the admin can change them without a deploy.

    Fields:
        enabled:                master switch for the anti-spam limiter
        messages_per_minute:    burst cap on messages inside a live chat. This
                                is the anti-flood number: sending faster than
                                this gets a warning and the message is dropped,
                                and it never costs anything.
        messages_per_hour:      sustained cap over the same window
        vip_multiplier:         how much VIP/premium users may exceed both caps
        chat_lifetime_hours:    how long one chat may live before it is closed
                                automatically. 24h by default; a chat that
                                reaches it ends for BOTH sides on the next
                                message.
        chat_girl_cost:         coins charged for «چت با دختر» when a match is
                                actually established
        chat_boy_cost:          coins charged for «چت با پسر» when a match is
                                actually established
        welcome_coins:          coins given to a brand-new user
        daily_bonus_coins:      coins claimable once per 24h from the wallet
        referral_coin_reward:   coins the INVITER gets per new referral — read
                                live, so the admin sets the price of an invite
        referral_premium_days:  premium days the inviter gets per referral
        referral_invitee_coins: coins the person who JOINS via a link gets
        random_chat_cost:       coins «اتصال شانسی» charges per match. 0 (the
                                default) keeps it free; the admin flips it to a
                                paid service from the panel without a deploy
        whisper_cost:           coins one group whisper costs. Was a module
                                constant; it lives here so the admin can make
                                نجوا free or repriced like every other service
    """

    __tablename__ = "bot_policy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Anti-spam ──
    messages_per_minute: Mapped[int] = mapped_column(Integer, default=20)
    messages_per_hour: Mapped[int] = mapped_column(Integer, default=300)
    vip_multiplier: Mapped[int] = mapped_column(Integer, default=3)

    # ── Chat rules ──
    chat_lifetime_hours: Mapped[int] = mapped_column(Integer, default=24)
    # The three match prices are Float because the admin may set 0.5 — a
    # fractional price is the whole point of this column being a price and not
    # a counter.
    chat_girl_cost: Mapped[float] = mapped_column(Float, default=1)
    chat_boy_cost: Mapped[float] = mapped_column(Float, default=1)

    # ── Rewards ──
    welcome_coins: Mapped[int] = mapped_column(Integer, default=30)
    daily_bonus_coins: Mapped[int] = mapped_column(Integer, default=25)
    referral_coin_reward: Mapped[int] = mapped_column(Integer, default=50)
    referral_premium_days: Mapped[int] = mapped_column(Integer, default=1)
    referral_invitee_coins: Mapped[int] = mapped_column(Integer, default=15)

    # ── Per-service pricing (0 = رایگان) ──
    #
    # Every feature an admin can bill lives here rather than in a constant, so
    # "free or paid, and how much" is one panel edit away. ``random_chat_cost``
    # defaults to 0 on purpose — «اتصال شانسی» has always been the free door
    # into the matcher, and a schema upgrade must never start charging for it.
    random_chat_cost: Mapped[float] = mapped_column(Float, default=0)
    whisper_cost: Mapped[float] = mapped_column(Float, default=1)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<BotPolicy id={self.id}>"


class CoinTransaction(Base):
    """Ledger of every coin a user has received or spent.

    ``users.coins`` is the balance, and it is the only number the economy ever
    charges against — this table never gates anything. It exists so the admin
    panel can answer "how many coins did THIS user get, and from what?" without
    guessing: the referral price changes over time, so multiplying
    ``referral_count`` by today's reward would misreport everyone who was paid
    under an older one.

    One row per movement, written in the same transaction as the balance change
    it describes, so the two can never disagree:

        welcome      the sign-up bonus (``ensure_user``)
        daily        the once-per-24h wallet bonus
        referral     paid to the INVITER per successful invite
        invitee      paid to the person who joined through a link
        gift         an admin gift
        whisper      charged for one group whisper
        match_*      charged for a match (``girl`` / ``boy`` / ``random``)
        refund_*     a charge handed back (failed match / rejected whisper)

    Fields:
        user_id:  the TELEGRAM id (same convention as every other table here)
        amount:   positive = received, negative = spent
        reason:   one of the codes above — small, stable and translated in the
                  panel, so renaming a feature does not rewrite history
        detail:   optional context (e.g. who referred them)
    """

    __tablename__ = "coin_ledger"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<CoinTransaction {self.user_id} {self.amount:+g} {self.reason}>"
        )
