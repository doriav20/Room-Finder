"""Who is signed in: request dependencies and the session cookie."""

from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from roomfinder.auth import SESSION_COOKIE, SESSION_LIFETIME, user_for_token
from roomfinder.models import User

_UNSET = object()


class LoginRequired(Exception):
    """Raised by require_user; turned into a redirect to the sign-in page."""


async def current_user(request: Request) -> User | None:
    if (cached := getattr(request.state, "user", _UNSET)) is not _UNSET:
        return cached
    user = None
    if token := request.cookies.get(SESSION_COOKIE):
        async with request.app.state.sessionmaker() as session:
            user = await user_for_token(session, token)
            await session.commit()
    request.state.user = user
    return user


async def require_user(request: Request) -> User:
    user = await current_user(request)
    if user is None:
        raise LoginRequired
    return user


async def require_admin(request: Request) -> User:
    user = await require_user(request)
    if not user.is_admin:
        raise HTTPException(403, "רק מנהלים יכולים לעשות את זה")
    return user


def login_required_response(request: Request, _exc: LoginRequired) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    if request.headers.get("HX-Request"):
        # htmx follows this header even on an error status
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)


def safe_next(target: str | None) -> str:
    """Only local paths, so a crafted link can't send someone to another site after sign-in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    secure = request.app.state.settings.cookie_secure or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
