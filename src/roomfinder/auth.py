"""Dashboard accounts: passwords, sign-in sessions and one-time invite links.

There is no open sign-up. An account can only be created from an invite link that an admin
made (the very first admin's link comes from the command line), and each link works once.
"""

import hashlib
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.db import utcnow
from roomfinder.models import Invite, InviteKind, Role, TelegramChat, User, UserSession

SESSION_COOKIE = "roomfinder_session"
SESSION_LIFETIME = timedelta(days=30)
# How often a session's expiry is pushed forward while it's being used
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
INVITE_LIFETIME = timedelta(days=3)
RESET_LIFETIME = timedelta(hours=24)
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
USERNAME_PATTERN = re.compile(r"^[\w.\-]{3,32}$")

_hasher = PasswordHasher()
# Checked for unknown usernames, so they take as long to reject as wrong passwords
_DUMMY_HASH = _hasher.hash("not a real password")


class AuthError(Exception):
    """Why an account action was refused, in words the user can see."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def username_key(username: str) -> str:
    return username.strip().casefold()


def validate_password(password: str, confirm: str, username: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"הסיסמה צריכה להיות לפחות {MIN_PASSWORD_LENGTH} תווים")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError("הסיסמה ארוכה מדי")
    if password != confirm:
        raise AuthError("הסיסמאות לא זהות")
    if username and password.casefold() == username.casefold():
        raise AuthError("הסיסמה לא יכולה להיות שם המשתמש")


async def count_users(session: AsyncSession) -> int:
    return await session.scalar(select(func.count(User.id))) or 0


async def active_admin_count(session: AsyncSession) -> int:
    query = select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
    return await session.scalar(query) or 0


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    user = await session.scalar(select(User).where(User.username_key == username_key(username)))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    return user


async def start_session(session: AsyncSession, user: User, user_agent: str | None) -> str:
    """Sign a browser in; returns the token for its cookie."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_LIFETIME,
            user_agent=(user_agent or "")[:256] or None,
        )
    )
    return token


async def user_for_token(session: AsyncSession, token: str | None) -> User | None:
    """The signed-in user for a cookie token, keeping an active session alive."""
    if not token:
        return None
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    )
    now = utcnow()
    if row is None or row.expires_at <= now:
        return None
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if now - row.last_seen_at >= SESSION_TOUCH_INTERVAL:
        row.last_seen_at = now
        row.expires_at = now + SESSION_LIFETIME
    return user


async def end_session(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(
            delete(UserSession).where(UserSession.token_hash == hash_token(token))
        )


async def end_other_sessions(session: AsyncSession, user_id: int, keep_token: str | None) -> None:
    query = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        query = query.where(UserSession.token_hash != hash_token(keep_token))
    await session.execute(query)


async def create_invite(
    session: AsyncSession,
    *,
    kind: InviteKind = InviteKind.REGISTER,
    role: Role = Role.MEMBER,
    created_by: User | None = None,
    user: User | None = None,
    note: str | None = None,
) -> tuple[str, Invite]:
    """A one-time link token. A reset link (kind RESET) sets the given user's password."""
    token = secrets.token_urlsafe(24)
    lifetime = INVITE_LIFETIME if kind == InviteKind.REGISTER else RESET_LIFETIME
    invite = Invite(
        token_hash=hash_token(token),
        kind=kind,
        role=role,
        user_id=user.id if user else None,
        created_by_id=created_by.id if created_by else None,
        note=(note or "").strip()[:128] or None,
        created_at=utcnow(),
        expires_at=utcnow() + lifetime,
    )
    session.add(invite)
    await session.flush()
    return token, invite


async def valid_invite(session: AsyncSession, token: str | None, kind: InviteKind) -> Invite | None:
    if not token:
        return None
    invite = await session.scalar(
        select(Invite).where(Invite.token_hash == hash_token(token), Invite.kind == kind)
    )
    if invite is None or invite.used_at is not None or invite.expires_at <= utcnow():
        return None
    return invite


async def register(
    session: AsyncSession, invite: Invite, username: str, password: str, confirm: str
) -> User:
    username = username.strip()
    if not USERNAME_PATTERN.match(username):
        raise AuthError("שם המשתמש צריך להיות 3-32 תווים: אותיות, ספרות, נקודה, מקף או קו תחתון")
    validate_password(password, confirm, username)
    taken = await session.scalar(select(User.id).where(User.username_key == username_key(username)))
    if taken:
        raise AuthError("שם המשתמש כבר תפוס")
    user = User(
        username=username,
        username_key=username_key(username),
        password_hash=hash_password(password),
        role=invite.role,
        is_active=True,
        created_at=utcnow(),
    )
    session.add(user)
    invite.used_at = utcnow()
    await session.flush()
    return user


async def reset_password(
    session: AsyncSession, invite: Invite, password: str, confirm: str
) -> User:
    user = await session.get(User, invite.user_id) if invite.user_id else None
    if user is None or not user.is_active:
        raise AuthError("המשתמש לא קיים או מושבת")
    validate_password(password, confirm, user.username)
    user.password_hash = hash_password(password)
    invite.used_at = utcnow()
    await end_other_sessions(session, user.id, keep_token=None)
    return user


async def change_password(
    session: AsyncSession, user: User, current: str, new: str, confirm: str, keep_token: str | None
) -> None:
    if not verify_password(user.password_hash, current):
        raise AuthError("הסיסמה הנוכחית שגויה")
    validate_password(new, confirm, user.username)
    user.password_hash = hash_password(new)
    # Anyone else signed in with the old password is signed out
    await end_other_sessions(session, user.id, keep_token=keep_token)


async def _ensure_other_admin(session: AsyncSession, user: User) -> None:
    if user.is_admin and user.is_active and await active_admin_count(session) <= 1:
        raise AuthError("זה המנהל הפעיל האחרון; קודם צריך להפוך משתמש אחר למנהל")


async def set_active(session: AsyncSession, user: User, active: bool) -> None:
    if not active:
        await _ensure_other_admin(session, user)
        await end_other_sessions(session, user.id, keep_token=None)
        # A disabled account stops getting Telegram alerts right away
        await session.execute(delete(TelegramChat).where(TelegramChat.account_id == user.id))
    user.is_active = active


async def set_role(session: AsyncSession, user: User, role: Role) -> None:
    if role != Role.ADMIN:
        await _ensure_other_admin(session, user)
    user.role = role


async def delete_account(session: AsyncSession, user: User) -> None:
    await set_active(session, user, False)
    await session.execute(delete(Invite).where(Invite.user_id == user.id))
    await session.delete(user)


class LoginThrottle:
    """Slows password guessing: after a few failures, a username or address has to wait."""

    def __init__(self, max_failures: int = 5, window_seconds: float = 15 * 60) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        while failures and now - failures[0] > self.window:
            failures.popleft()
        return failures

    def wait_seconds(self, *keys: str) -> int:
        now = time.monotonic()
        waits = [
            int(self.window - (now - failures[0])) + 1
            for key in keys
            if len(failures := self._recent(key, now)) >= self.max_failures
        ]
        return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        now = time.monotonic()
        for key in keys:
            self._recent(key, now).append(now)

    def succeeded(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)
