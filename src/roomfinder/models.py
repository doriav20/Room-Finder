from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from roomfinder.db import Base, UTCDateTime, utcnow


class ListingStatus(StrEnum):
    NEW = "new"
    SEEN = "seen"
    FAVORITE = "favorite"
    CONTACTED = "contacted"
    HIDDEN = "hidden"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None]
    rooms: Mapped[float | None]
    sqm: Mapped[int | None]
    floor: Mapped[int | None]
    total_floors: Mapped[int | None]
    property_type: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(64))
    neighborhood: Mapped[str | None] = mapped_column(String(128))
    street: Mapped[str | None] = mapped_column(String(128))
    house_number: Mapped[str | None] = mapped_column(String(16))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    is_agency: Mapped[bool | None]
    entry_date: Mapped[str | None] = mapped_column(String(64))
    images: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Amenity tags from the site, e.g. "חניה", "ממ"ד"
    features: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    furnished: Mapped[bool | None]
    furniture_details: Mapped[str | None] = mapped_column(Text)
    # Whether the apartment comes with its own parking (street parking doesn't count)
    parking: Mapped[bool | None]
    parking_spaces: Mapped[int | None]
    # When the listing's own page was read for furniture, parking and the ad text
    details_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    matches: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(16), default=ListingStatus.NEW, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    # Normalized "street|house number", used to spot the same apartment on several sites
    address_key: Mapped[str | None] = mapped_column(String(160), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    price_changes: Mapped[list["PriceChange"]] = relationship(
        back_populates="listing",
        cascade="all, delete-orphan",
        order_by="PriceChange.seen_at",
    )


class PriceChange(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    old_price: Mapped[int | None]
    new_price: Mapped[int | None]
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="price_changes")


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING)
    found: Mapped[int] = mapped_column(default=0)
    new: Mapped[int] = mapped_column(default=0)
    baseline: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)


class SourceState(Base):
    __tablename__ = "source_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    interval_minutes: Mapped[int | None]
    custom_url: Mapped[str | None] = mapped_column(Text)
    # Hash of everything that changes what the source returns; a change triggers a baseline run
    fetch_signature: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    failure_alert_sent: Mapped[bool] = mapped_column(default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TelegramChat(Base):
    """A private chat of a user allowed to use the bot, paired with a one-time link.

    The bot ignores every update from users that aren't in this table.
    """

    __tablename__ = "telegram_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # The dashboard account that connected this chat; its chats go when the account does
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class User(Base):
    """A dashboard account. Accounts are only created through invite links."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    # Case-folded username, so "Dor" and "dor" can't both exist
    username_key: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


class UserSession(Base):
    """A signed-in browser. The cookie holds a random token; only its hash is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    user_agent: Mapped[str | None] = mapped_column(String(256))


class InviteKind(StrEnum):
    REGISTER = "register"
    RESET = "reset"


class Invite(Base):
    """A one-time link: to create an account, or to set a new password for one."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default=InviteKind.REGISTER)
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER)
    # For a reset link: the account whose password it sets
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Who it's for, so the admin can tell invites apart
    note: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
