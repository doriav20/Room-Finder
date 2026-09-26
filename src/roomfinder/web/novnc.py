"""The container's browser (noVNC) inside the dashboard, for signed-in admins only.

noVNC's page is served under /novnc behind the sign-in check, and its WebSocket is bridged
here to the VNC server, which listens inside the container only. Without an admin's session
nothing about the browser can be seen or controlled.
"""

import asyncio
import contextlib
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from roomfinder.auth import SESSION_COOKIE, user_for_token

NOVNC_DIR = Path("/usr/share/novnc")
VNC_SERVER = ("127.0.0.1", 5900)
# An open view is cut off this soon after its sign-in ends (sign-out, disabled, no longer admin)
SESSION_RECHECK_SECONDS = 30

router = APIRouter()


async def _signed_in_admin(websocket: WebSocket, token: str | None) -> bool:
    async with websocket.app.state.sessionmaker() as session:
        user = await user_for_token(session, token)
        await session.commit()
    return user is not None and user.is_admin


@router.websocket("/novnc/websockify")
async def vnc_bridge(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).hostname not in websocket.app.state.settings.allowed_hosts:
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(SESSION_COOKIE)
    if not await _signed_in_admin(websocket, token):
        await websocket.close(code=1008)
        return
    try:
        reader, writer = await asyncio.open_connection(*VNC_SERVER)
    except OSError:
        await websocket.close(code=1011)
        return
    await websocket.accept()

    async def browser_to_vnc() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                writer.write(await websocket.receive_bytes())
                await writer.drain()

    async def vnc_to_browser() -> None:
        while data := await reader.read(65536):
            await websocket.send_bytes(data)

    async def while_signed_in() -> None:
        while True:
            await asyncio.sleep(SESSION_RECHECK_SECONDS)
            if not await _signed_in_admin(websocket, token):
                return

    tasks = {
        asyncio.create_task(browser_to_vnc()),
        asyncio.create_task(vnc_to_browser()),
        asyncio.create_task(while_signed_in()),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()


def add_novnc(app: FastAPI) -> None:
    # The WebSocket route must come before the static mount, which would otherwise take it
    app.include_router(router)
    if NOVNC_DIR.is_dir():
        app.mount("/novnc", StaticFiles(directory=NOVNC_DIR), name="novnc")
