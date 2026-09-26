"""Nothing is shown and nothing happens without signing in; admins-only stays admins-only."""

import re
import socketserver
import threading

import pytest
from sqlalchemy import delete
from starlette.websockets import WebSocketDisconnect

from conftest import PASSWORD, add_listing, add_user, sign_in
from roomfinder.auth import SESSION_COOKIE
from roomfinder.models import Listing, ListingStatus, Role, User, UserSession
from roomfinder.web import novnc

PAGES = [
    "/",
    "/areas",
    "/?tab=all",
    "/listings/cards?offset=0",
    "/sources",
    "/sources/yad2/row",
    "/settings",
    "/account",
    "/users",
    "/export.csv",
    "/novnc/vnc.html",
    "/no-such-page",
]
ACTIONS = [
    "/areas",
    "/listings/1/status",
    "/listings/1/notes",
    "/listings/mark-seen",
    "/sources/yad2/run",
    "/sources/yad2/toggle",
    "/sources/yad2/settings",
    "/settings/criteria",
    "/settings/preferences",
    "/browser/restart",
    "/account/password",
    "/account/telegram/link",
    "/users/invites",
    "/users/1/active",
]


def invite_link(client, role: str = "member", note: str = "") -> str:
    html = client.post("/users/invites", data={"role": role, "note": note}).text
    return re.search(r'value="(http://testserver/register\?token=[^"]+)"', html).group(1)


def register(client, link: str, username: str, password: str = PASSWORD):
    token = link.split("token=")[1]
    return client.post(
        "/register",
        data={"token": token, "username": username, "password": password, "confirm": password},
        follow_redirects=False,
    )


# --- signed out ------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_pages_redirect_to_sign_in_and_reveal_nothing(anonymous, db, path):
    add_listing(db, "secret", street="רחוב סודי")

    response = anonymous.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert "סודי" not in response.text


@pytest.mark.parametrize("path", ACTIONS)
def test_actions_do_nothing_when_signed_out(anonymous, db, path):
    listing = add_listing(db, "a", status=ListingStatus.NEW)

    response = anonymous.post(path, data={"status": "hidden"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    db.expire_all()
    assert db.get(Listing, listing.id).status == ListingStatus.NEW


def test_htmx_and_api_requests_get_401_without_data(anonymous, db):
    add_listing(db, "a", status=ListingStatus.NEW, street="רחוב סודי")

    htmx = anonymous.post("/listings/1/status", headers={"HX-Request": "true"})
    api = anonymous.get("/api/updates?since_id=0")

    assert htmx.status_code == 401 and htmx.headers["HX-Redirect"] == "/login"
    assert api.status_code == 401 and "סודי" not in api.text


def test_no_generated_api_docs(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_static_files_and_sign_in_page_are_public(anonymous):
    stylesheet = anonymous.get("/static/app.css")
    assert stylesheet.status_code == 200 and stylesheet.headers["Cache-Control"] == "no-cache"
    again = anonymous.get("/static/app.css", headers={"If-None-Match": stylesheet.headers["ETag"]})
    assert again.status_code == 304
    page = anonymous.get("/login")
    assert page.status_code == 200 and "כניסה" in page.text


def test_security_headers(anonymous, client):
    response = client.get("/")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_browser_socket_needs_an_admin(anonymous, db):
    with pytest.raises(WebSocketDisconnect), anonymous.websocket_connect("/novnc/websockify"):
        pass
    add_user(db, "noa", role=Role.MEMBER)
    sign_in(anonymous, "noa")
    with pytest.raises(WebSocketDisconnect), anonymous.websocket_connect("/novnc/websockify"):
        pass


class _EchoVnc(socketserver.BaseRequestHandler):
    """Stands in for x11vnc: greets like a VNC server, then echoes what it gets."""

    def handle(self):
        self.request.sendall(b"RFB 003.008\n")
        while data := self.request.recv(1024):
            self.request.sendall(data)


@pytest.fixture
def vnc_server(monkeypatch):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoVnc)
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(novnc, "VNC_SERVER", server.server_address)
    yield
    server.shutdown()
    server.server_close()


def test_admins_reach_the_browser_through_the_dashboard(client, vnc_server):
    headers = {"Origin": "http://localhost:8000"}
    with client.websocket_connect("/novnc/websockify", headers=headers) as browser:
        assert browser.receive_bytes() == b"RFB 003.008\n"
        browser.send_bytes(b"click")
        assert browser.receive_bytes() == b"click"


def test_the_browser_socket_refuses_other_sites(client, vnc_server):
    headers = {"Origin": "https://evil.example"}
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/novnc/websockify", headers=headers),
    ):
        pass


def test_an_open_browser_view_closes_when_the_sign_in_ends(client, db, vnc_server, monkeypatch):
    monkeypatch.setattr(novnc, "SESSION_RECHECK_SECONDS", 0.05)
    with client.websocket_connect("/novnc/websockify") as browser:
        assert browser.receive_bytes() == b"RFB 003.008\n"

        db.execute(delete(UserSession))  # signed out everywhere, e.g. the account was disabled
        db.commit()

        with pytest.raises(WebSocketDisconnect):
            browser.receive_bytes()


# --- signing in ------------------------------------------------------------------


def test_sign_in_sets_a_protected_cookie_and_returns_to_the_page(anonymous, admin):
    response = anonymous.post(
        "/login",
        data={"username": "ADMIN", "password": PASSWORD, "next": "/sources"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/sources"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anonymous.get("/sources").status_code == 200


def test_wrong_details_get_the_same_answer(anonymous, admin):
    wrong_password = anonymous.post("/login", data={"username": "admin", "password": "nope"})
    no_such_user = anonymous.post("/login", data={"username": "ghost", "password": "nope"})

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert "שם המשתמש או הסיסמה שגויים" in wrong_password.text
    assert "שם המשתמש או הסיסמה שגויים" in no_such_user.text


def test_repeated_failures_lock_the_sign_in(anonymous, admin):
    for _ in range(5):
        anonymous.post("/login", data={"username": "admin", "password": "wrong"})

    response = anonymous.post("/login", data={"username": "admin", "password": PASSWORD})

    assert response.status_code == 429
    assert anonymous.cookies.get(SESSION_COOKIE) is None


def test_redirects_after_sign_in_stay_on_this_site(anonymous, admin):
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        response = anonymous.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "next": target},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"


def test_sign_out_ends_the_session(client):
    token = client.cookies.get(SESSION_COOKIE)

    client.post("/logout")
    client.cookies.set(SESSION_COOKIE, token)

    assert client.get("/", follow_redirects=False).status_code == 303


def test_disabled_users_cannot_sign_in(anonymous, db):
    add_user(db, "old", role=Role.MEMBER, active=False)

    response = anonymous.post("/login", data={"username": "old", "password": PASSWORD})

    assert response.status_code == 401


# --- invites and accounts ----------------------------------------------------------


def test_invite_link_creates_one_account(client, db):
    link = invite_link(client, note="נועה")
    client.post("/logout")

    assert "נועה" in client.get(link).text
    response = register(client, link, "noa")
    assert response.status_code == 303
    assert "noa" in client.get("/").text  # signed in right away
    client.post("/logout")
    assert register(client, link, "someone-else").status_code == 404
    assert db.query(User).filter_by(username_key="noa").one().role == Role.MEMBER


def test_made_up_or_missing_invite_tokens_are_refused(anonymous):
    assert anonymous.get("/register?token=made-up").status_code == 404
    assert anonymous.get("/register").status_code == 404
    assert anonymous.get("/reset?token=made-up").status_code == 404
    response = anonymous.post(
        "/register",
        data={"token": "made-up", "username": "x" * 5, "password": PASSWORD, "confirm": PASSWORD},
    )
    assert response.status_code == 404


def test_members_cannot_reach_admin_pages(anonymous, db):
    add_user(db, "noa", role=Role.MEMBER)
    sign_in(anonymous, "noa")

    assert anonymous.get("/users").status_code == 403
    assert anonymous.post("/users/invites", data={"role": "admin"}).status_code == 403
    assert anonymous.get("/novnc/vnc.html").status_code == 403
    assert anonymous.post("/browser/restart").status_code == 403
    page = anonymous.get("/sources").text
    assert "/users" not in page and "/novnc" not in page
    assert anonymous.get("/").status_code == 200  # the rest of the dashboard works


def test_disabling_a_user_signs_them_out_everywhere(client, db):
    member = add_user(db, "noa", role=Role.MEMBER)
    admin_token = client.cookies.get(SESSION_COOKIE)
    sign_in(client, "noa")
    member_token = client.cookies.get(SESSION_COOKIE)
    client.cookies.set(SESSION_COOKIE, admin_token)

    client.post(f"/users/{member.id}/active")

    client.cookies.set(SESSION_COOKIE, member_token)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_admins_do_not_change_their_own_account_from_the_users_page(client, admin, db):
    response = client.post(f"/users/{admin.id}/active", follow_redirects=False)

    assert response.headers["location"] == "/users?notice=self"
    db.expire_all()
    assert db.get(User, admin.id).is_active


def test_reset_link_sets_a_new_password(client, db):
    member = add_user(db, "noa", role=Role.MEMBER)
    html = client.post(f"/users/{member.id}/reset-link").text
    link = re.search(r'value="(http://testserver/reset\?token=[^"]+)"', html).group(1)
    client.post("/logout")
    token = link.split("token=")[1]

    response = client.post(
        "/reset",
        data={"token": token, "password": "a new password!", "confirm": "a new password!"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    client.post("/logout")
    assert client.post("/login", data={"username": "noa", "password": PASSWORD}).status_code == 401
    sign_in(client, "noa", "a new password!")
    assert client.get(link).status_code == 404  # used up


def test_change_own_password(client):
    wrong = client.post(
        "/account/password",
        data={"current": "wrong", "password": "another password", "confirm": "another password"},
    )
    right = client.post(
        "/account/password",
        data={"current": PASSWORD, "password": "another password", "confirm": "another password"},
        follow_redirects=False,
    )

    assert wrong.status_code == 400 and "הסיסמה הנוכחית שגויה" in wrong.text
    assert right.headers["location"] == "/account?notice=password"
    client.post("/logout")
    sign_in(client, "admin", "another password")
