"""Signing in, invite-only sign-up, the user's own account page, and user management."""

from collections import defaultdict
from typing import Any

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder import auth
from roomfinder.auth import SESSION_COOKIE, AuthError
from roomfinder.db import utcnow
from roomfinder.models import Invite, InviteKind, Role, TelegramChat, User, UserSession
from roomfinder.notify.telegram import PAIRING_TTL
from roomfinder.web.routes import render_page, render_partial, templates
from roomfinder.web.security import (
    clear_session_cookie,
    current_user,
    require_admin,
    require_user,
    safe_next,
    set_session_cookie,
)

public_router = APIRouter()
account_router = APIRouter(dependencies=[Depends(require_user)])
admin_router = APIRouter(dependencies=[Depends(require_admin)])

NOTICES = {
    "password": "✔ הסיסמה הוחלפה. שאר המכשירים נותקו.",
    "sessions": "✔ נותקת מכל שאר המכשירים.",
    "last_admin": "אי אפשר: זה המנהל הפעיל האחרון. קודם צריך להפוך משתמש אחר למנהל.",
    "self": "את החשבון שלך לא משנים מכאן.",
    "deleted": "✔ המשתמש נמחק.",
}


def _sessionmaker(request: Request):
    return request.app.state.sessionmaker


def _auth_page(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """Pages for signed-out visitors, without the dashboard's menus."""
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _qr(link: str) -> str:
    return segno.make(link, error="m").svg_inline(scale=4, border=1, omitsize=True)


def _signed_in(request: Request, target: str, token: str) -> RedirectResponse:
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, request, token)
    return response


# --- signing in ------------------------------------------------------------------


@public_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> Response:
    if await current_user(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _auth_page(request, "login.html", {"next": safe_next(next)})


@public_router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    target = safe_next(str(form.get("next", "/")))
    context = {"next": target, "username": username}
    throttle = request.app.state.login_throttle
    keys = (f"user:{auth.username_key(username)}", f"address:{_client_address(request)}")

    if wait := throttle.wait_seconds(*keys):
        error = f"יותר מדי ניסיונות. אפשר לנסות שוב בעוד {max(1, round(wait / 60))} דקות."
        return _auth_page(request, "login.html", context | {"error": error}, 429)
    async with _sessionmaker(request)() as session:
        user = await auth.authenticate(session, username, password)
        if user is None:
            throttle.failed(*keys)
            error = "שם המשתמש או הסיסמה שגויים"
            return _auth_page(request, "login.html", context | {"error": error}, 401)
        token = await auth.start_session(session, user, request.headers.get("user-agent"))
        await session.commit()
    throttle.succeeded(*keys)
    return _signed_in(request, target, token)


@public_router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await auth.end_session(session, request.cookies.get(SESSION_COOKIE))
        await session.commit()
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


# --- invite links ----------------------------------------------------------------


@public_router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, token: str = "") -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        invite = await auth.valid_invite(session, token, InviteKind.REGISTER)
    if invite is None:
        return _auth_page(request, "invite_invalid.html", {}, 404)
    context = {"token": token, "role": invite.role, "note": invite.note}
    return _auth_page(request, "register.html", context)


@public_router.post("/register", response_class=HTMLResponse)
async def register(request: Request) -> Response:
    form = await request.form()
    token = str(form.get("token", ""))
    username = str(form.get("username", ""))
    async with _sessionmaker(request)() as session:
        invite = await auth.valid_invite(session, token, InviteKind.REGISTER)
        if invite is None:
            return _auth_page(request, "invite_invalid.html", {}, 404)
        try:
            user = await auth.register(
                session,
                invite,
                username,
                str(form.get("password", "")),
                str(form.get("confirm", "")),
            )
        except AuthError as exc:
            context = {"token": token, "role": invite.role, "note": invite.note}
            context |= {"username": username, "error": str(exc)}
            return _auth_page(request, "register.html", context, 400)
        session_token = await auth.start_session(session, user, request.headers.get("user-agent"))
        await session.commit()
    return _signed_in(request, "/", session_token)


async def _reset_target(session: AsyncSession, token: str) -> tuple[Invite, User] | None:
    invite = await auth.valid_invite(session, token, InviteKind.RESET)
    user = await session.get(User, invite.user_id) if invite and invite.user_id else None
    return (invite, user) if invite and user and user.is_active else None


@public_router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = "") -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        target = await _reset_target(session, token)
    if target is None:
        return _auth_page(request, "invite_invalid.html", {}, 404)
    return _auth_page(request, "reset.html", {"token": token, "username": target[1].username})


@public_router.post("/reset", response_class=HTMLResponse)
async def reset(request: Request) -> Response:
    form = await request.form()
    token = str(form.get("token", ""))
    async with _sessionmaker(request)() as session:
        target = await _reset_target(session, token)
        if target is None:
            return _auth_page(request, "invite_invalid.html", {}, 404)
        invite, user = target
        try:
            await auth.reset_password(
                session, invite, str(form.get("password", "")), str(form.get("confirm", ""))
            )
        except AuthError as exc:
            context = {"token": token, "username": user.username, "error": str(exc)}
            return _auth_page(request, "reset.html", context, 400)
        session_token = await auth.start_session(session, user, request.headers.get("user-agent"))
        await session.commit()
    return _signed_in(request, "/", session_token)


# --- the signed-in user's account --------------------------------------------------


async def _telegram_context(request: Request, session: AsyncSession, user: User) -> dict[str, Any]:
    telegram = request.app.state.telegram
    chats = await session.scalars(
        select(TelegramChat)
        .where(TelegramChat.account_id == user.id)
        .order_by(TelegramChat.created_at)
    )
    return {
        "telegram": {
            "configured": telegram is not None,
            "username": telegram.username if telegram else None,
            "chats": list(chats),
        },
        "link_minutes": int(PAIRING_TTL.total_seconds() // 60),
    }


@account_router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, notice: str = "") -> HTMLResponse:
    return await _render_account(request, notice=notice)


async def _render_account(
    request: Request, notice: str = "", error: str = "", status_code: int = 200
) -> HTMLResponse:
    user = request.state.user
    async with _sessionmaker(request)() as session:
        signed_in = await session.scalar(
            select(func.count(UserSession.id)).where(
                UserSession.user_id == user.id, UserSession.expires_at > utcnow()
            )
        )
        context = {
            "active": "account",
            "notice": NOTICES.get(notice),
            "error": error,
            "signed_in_count": signed_in,
        } | await _telegram_context(request, session, user)
        response = await render_page(request, session, "account.html", context)
    response.status_code = status_code
    return response


@account_router.post("/account/password", response_class=HTMLResponse)
async def change_password(request: Request) -> Response:
    form = await request.form()
    async with _sessionmaker(request)() as session:
        user = await session.get(User, request.state.user.id)
        try:
            await auth.change_password(
                session,
                user,
                str(form.get("current", "")),
                str(form.get("password", "")),
                str(form.get("confirm", "")),
                keep_token=request.cookies.get(SESSION_COOKIE),
            )
        except AuthError as exc:
            return await _render_account(request, error=str(exc), status_code=400)
        await session.commit()
    return RedirectResponse("/account?notice=password", status_code=303)


@account_router.post("/account/sessions/end-others")
async def end_other_sessions(request: Request) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await auth.end_other_sessions(
            session, request.state.user.id, keep_token=request.cookies.get(SESSION_COOKIE)
        )
        await session.commit()
    return RedirectResponse("/account?notice=sessions", status_code=303)


def _telegram(request: Request):
    telegram = request.app.state.telegram
    if telegram is None:
        raise HTTPException(503, "Telegram is not configured")
    return telegram


async def _render_telegram(request: Request, **extra: Any) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        context = await _telegram_context(request, session, request.state.user)
    return render_partial(request, "partials/account_telegram.html", context | extra)


@account_router.post("/account/telegram/link", response_class=HTMLResponse)
async def telegram_link(request: Request) -> HTMLResponse:
    link = _telegram(request).pairing_link(request.state.user.id)
    return await _render_telegram(request, link=link, qr_svg=_qr(link))


@account_router.post("/account/telegram/{chat_id}/remove", response_class=HTMLResponse)
async def telegram_disconnect(request: Request, chat_id: int) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        await session.execute(
            delete(TelegramChat).where(
                TelegramChat.chat_id == chat_id,
                TelegramChat.account_id == request.state.user.id,
            )
        )
        await session.commit()
    return await _render_telegram(request, message="✔ הטלגרם נותק.")


@account_router.post("/account/telegram/test", response_class=HTMLResponse)
async def telegram_test(request: Request) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        chat_ids = list(
            await session.scalars(
                select(TelegramChat.chat_id).where(TelegramChat.account_id == request.state.user.id)
            )
        )
    await _telegram(request).send_text(chat_ids, "✅ הודעת בדיקה מהדשבורד של RoomFinder")
    return await _render_telegram(request, message="✔ נשלחה הודעת בדיקה.")


# --- user management (admins) ------------------------------------------------------


@admin_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, notice: str = "") -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        users = list(await session.scalars(select(User).order_by(User.created_at)))
        chats = list(await session.scalars(select(TelegramChat).order_by(TelegramChat.created_at)))
        invites = await session.scalars(
            select(Invite)
            .where(Invite.used_at.is_(None), Invite.expires_at > utcnow())
            .order_by(Invite.created_at.desc())
        )
        chats_by_account: defaultdict[int, list[TelegramChat]] = defaultdict(list)
        for chat in chats:
            if chat.account_id is not None:
                chats_by_account[chat.account_id].append(chat)
        context = {
            "active": "users",
            "notice": NOTICES.get(notice),
            "users": users,
            "usernames": {user.id: user.username for user in users},
            "chats_by_account": chats_by_account,
            "unassigned_chats": [chat for chat in chats if chat.account_id is None],
            "invites": list(invites),
        }
        return await render_page(request, session, "users.html", context)


@admin_router.post("/users/invites", response_class=HTMLResponse)
async def create_invite(request: Request) -> HTMLResponse:
    form = await request.form()
    role = Role.ADMIN if form.get("role") == Role.ADMIN else Role.MEMBER
    note = str(form.get("note", "")).strip()
    async with _sessionmaker(request)() as session:
        token, _ = await auth.create_invite(
            session, role=role, created_by=request.state.user, note=note
        )
        await session.commit()
    link = str(request.url_for("register_page").include_query_params(token=token))
    context = {"link": link, "qr_svg": _qr(link), "kind": "register", "note": note}
    return render_partial(request, "partials/invite_link.html", context)


@admin_router.post("/users/invites/{invite_id}/revoke")
async def revoke_invite(request: Request, invite_id: int) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await session.execute(delete(Invite).where(Invite.id == invite_id))
        await session.commit()
    return RedirectResponse("/users", status_code=303)


class _Refused(Exception):
    def __init__(self, notice: str) -> None:
        self.notice = notice


async def _managed_user(session: AsyncSession, request: Request, user_id: int) -> User:
    """Another user than the admin doing the change (their own account is left alone)."""
    if user_id == request.state.user.id:
        raise _Refused("self")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    return user


async def _change_user(request: Request, user_id: int, change) -> RedirectResponse:
    try:
        async with _sessionmaker(request)() as session:
            user = await _managed_user(session, request, user_id)
            try:
                notice = await change(session, user)
            except AuthError:
                raise _Refused("last_admin") from None
            await session.commit()
    except _Refused as refused:
        return RedirectResponse(f"/users?notice={refused.notice}", status_code=303)
    return RedirectResponse(f"/users?notice={notice or ''}", status_code=303)


@admin_router.post("/users/{user_id}/active")
async def toggle_active(request: Request, user_id: int) -> RedirectResponse:
    async def change(session: AsyncSession, user: User) -> None:
        await auth.set_active(session, user, not user.is_active)

    return await _change_user(request, user_id, change)


@admin_router.post("/users/{user_id}/role")
async def change_role(request: Request, user_id: int) -> RedirectResponse:
    role = Role.ADMIN if (await request.form()).get("role") == Role.ADMIN else Role.MEMBER

    async def change(session: AsyncSession, user: User) -> None:
        await auth.set_role(session, user, role)

    return await _change_user(request, user_id, change)


@admin_router.post("/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int) -> RedirectResponse:
    async def change(session: AsyncSession, user: User) -> str:
        await auth.delete_account(session, user)
        return "deleted"

    return await _change_user(request, user_id, change)


@admin_router.post("/users/{user_id}/reset-link", response_class=HTMLResponse)
async def create_reset_link(request: Request, user_id: int) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            raise HTTPException(404)
        token, _ = await auth.create_invite(
            session, kind=InviteKind.RESET, created_by=request.state.user, user=user
        )
        await session.commit()
    link = str(request.url_for("reset_page").include_query_params(token=token))
    context = {"link": link, "qr_svg": _qr(link), "kind": "reset", "note": user.username}
    return render_partial(request, "partials/invite_link.html", context)


@admin_router.post("/users/telegram/{chat_id}/remove")
async def remove_telegram_chat(request: Request, chat_id: int) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await session.execute(delete(TelegramChat).where(TelegramChat.chat_id == chat_id))
        await session.commit()
    return RedirectResponse("/users", status_code=303)
