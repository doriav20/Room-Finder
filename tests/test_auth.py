from datetime import timedelta

import pytest
from sqlalchemy import select

from roomfinder import auth
from roomfinder.auth import (
    AuthError,
    LoginThrottle,
    authenticate,
    change_password,
    create_invite,
    delete_account,
    end_other_sessions,
    end_session,
    hash_password,
    register,
    reset_password,
    set_active,
    set_role,
    start_session,
    user_for_token,
    valid_invite,
    verify_password,
)
from roomfinder.db import utcnow
from roomfinder.models import Invite, InviteKind, Role, TelegramChat, User, UserSession

PASSWORD = "correct horse battery"


async def make_user(session, username="dor", role=Role.ADMIN, active=True) -> User:
    user = User(
        username=username,
        username_key=username.casefold(),
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=active,
    )
    session.add(user)
    await session.flush()
    return user


def test_passwords_are_hashed_and_verified():
    hashed = hash_password(PASSWORD)

    assert hashed != PASSWORD and hashed.startswith("$argon2")
    assert verify_password(hashed, PASSWORD)
    assert not verify_password(hashed, "wrong password!")
    assert not verify_password("not a hash", PASSWORD)


async def test_authenticate(session):
    user = await make_user(session, "Dor")

    assert await authenticate(session, "dor", PASSWORD) is user  # any letter case
    assert user.last_login_at is not None
    assert await authenticate(session, "dor", "wrong password") is None
    assert await authenticate(session, "nobody", PASSWORD) is None
    user.is_active = False
    assert await authenticate(session, "dor", PASSWORD) is None


async def test_sessions(session):
    user = await make_user(session)
    token = await start_session(session, user, "Chrome")
    other = await start_session(session, user, "Phone")
    await session.flush()

    assert await user_for_token(session, token) is user
    assert await user_for_token(session, "made-up") is None
    assert await user_for_token(session, None) is None

    await end_other_sessions(session, user.id, keep_token=token)
    assert await user_for_token(session, other) is None
    assert await user_for_token(session, token) is user

    await end_session(session, token)
    assert await user_for_token(session, token) is None


async def test_expired_sessions_and_disabled_users_are_signed_out(session):
    user = await make_user(session)
    token = await start_session(session, user, None)
    await session.flush()
    row = await session.scalar(select(UserSession))

    user.is_active = False
    assert await user_for_token(session, token) is None
    user.is_active = True
    row.expires_at = utcnow() - timedelta(seconds=1)
    assert await user_for_token(session, token) is None


async def test_active_sessions_are_extended(session):
    user = await make_user(session)
    token = await start_session(session, user, None)
    await session.flush()
    row = await session.scalar(select(UserSession))
    row.last_seen_at = utcnow() - timedelta(hours=1)
    old_expiry = row.expires_at

    await user_for_token(session, token)

    assert row.expires_at > old_expiry


async def test_invites_work_once_and_expire(session):
    admin = await make_user(session)
    token, invite = await create_invite(session, created_by=admin, note="Noa")

    assert invite.note == "Noa" and invite.role == Role.MEMBER
    assert await valid_invite(session, token, InviteKind.REGISTER) is invite
    assert await valid_invite(session, token, InviteKind.RESET) is None
    assert await valid_invite(session, "made-up", InviteKind.REGISTER) is None

    await register(session, invite, "noa", PASSWORD, PASSWORD)
    assert await valid_invite(session, token, InviteKind.REGISTER) is None

    other_token, other = await create_invite(session)
    other.expires_at = utcnow() - timedelta(seconds=1)
    assert await valid_invite(session, other_token, InviteKind.REGISTER) is None


async def test_register_creates_an_account_with_the_invite_role(session):
    _, invite = await create_invite(session, role=Role.ADMIN)

    user = await register(session, invite, " Dor ", PASSWORD, PASSWORD)

    assert (user.username, user.username_key, user.role) == ("Dor", "dor", Role.ADMIN)
    assert verify_password(user.password_hash, PASSWORD)
    assert invite.used_at is not None


@pytest.mark.parametrize(
    ("username", "password", "confirm", "message"),
    [
        ("DOR", PASSWORD, PASSWORD, "תפוס"),
        ("a", PASSWORD, PASSWORD, "3-32"),
        ("with space", PASSWORD, PASSWORD, "3-32"),
        ("noa", "short", "short", "לפחות"),
        ("noa", PASSWORD, PASSWORD + "x", "לא זהות"),
        ("noa-noa-noa", "noa-noa-noa", "noa-noa-noa", "שם המשתמש"),
    ],
)
async def test_register_rejects_bad_details(session, username, password, confirm, message):
    await make_user(session, "dor")
    _, invite = await create_invite(session)

    with pytest.raises(AuthError, match=message):
        await register(session, invite, username, password, confirm)
    assert invite.used_at is None


async def test_reset_link_sets_a_new_password_and_signs_out_everywhere(session):
    user = await make_user(session)
    old_session = await start_session(session, user, None)
    _, invite = await create_invite(session, kind=InviteKind.RESET, user=user)

    await reset_password(session, invite, "a brand new password", "a brand new password")

    assert verify_password(user.password_hash, "a brand new password")
    assert await user_for_token(session, old_session) is None
    assert invite.used_at is not None


async def test_change_password(session):
    user = await make_user(session)
    current = await start_session(session, user, None)
    other = await start_session(session, user, None)

    with pytest.raises(AuthError):
        await change_password(
            session, user, "wrong", "new password 123", "new password 123", current
        )
    await change_password(session, user, PASSWORD, "new password 123", "new password 123", current)

    assert verify_password(user.password_hash, "new password 123")
    assert await user_for_token(session, current) is user
    assert await user_for_token(session, other) is None


async def test_disabling_an_account_signs_it_out_and_stops_its_alerts(session):
    await make_user(session, "admin")
    member = await make_user(session, "noa", role=Role.MEMBER)
    token = await start_session(session, member, None)
    session.add(TelegramChat(chat_id=5, user_id=5, name="Noa", account_id=member.id))
    await session.flush()

    await set_active(session, member, False)

    assert await user_for_token(session, token) is None
    assert (await session.scalars(select(TelegramChat))).all() == []


async def test_the_last_admin_is_protected(session):
    admin = await make_user(session, "admin")

    with pytest.raises(AuthError):
        await set_active(session, admin, False)
    with pytest.raises(AuthError):
        await set_role(session, admin, Role.MEMBER)
    with pytest.raises(AuthError):
        await delete_account(session, admin)

    second = await make_user(session, "second")
    await set_role(session, admin, Role.MEMBER)
    assert admin.role == Role.MEMBER and second.is_admin


async def test_deleting_an_account_removes_its_reset_links(session):
    await make_user(session, "admin")
    member = await make_user(session, "noa", role=Role.MEMBER)
    await create_invite(session, kind=InviteKind.RESET, user=member)

    await delete_account(session, member)
    await session.flush()

    assert (await session.scalars(select(Invite))).all() == []
    assert await session.get(User, member.id) is None


def test_login_throttle(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    throttle = LoginThrottle(max_failures=3, window_seconds=60)

    for _ in range(2):
        throttle.failed("user:dor", "ip:1")
    assert throttle.wait_seconds("user:dor") == 0
    throttle.failed("user:dor", "ip:1")
    assert 0 < throttle.wait_seconds("user:dor") <= 61
    assert throttle.wait_seconds("user:other", "ip:1") > 0

    now[0] += 61
    assert throttle.wait_seconds("user:dor", "ip:1") == 0
    throttle.failed("user:dor")
    throttle.succeeded("user:dor")
    assert throttle.wait_seconds("user:dor") == 0
