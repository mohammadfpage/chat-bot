"""
SQLAlchemy ORM models.
Only user metadata is stored — NO message history, NO system settings.
System config lives in .env via config.py.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
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

    # ── Moderation (admin-only global ban) ──
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

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
