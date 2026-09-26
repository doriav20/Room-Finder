import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from roomfinder import __version__
from roomfinder.auth import LoginThrottle
from roomfinder.browser import BrowserManager
from roomfinder.config import Settings, get_settings
from roomfinder.db import create_engine, create_sessionmaker
from roomfinder.notify import LogNotifier, Notifier
from roomfinder.notify.telegram import TelegramService
from roomfinder.pipeline import refresh_areas
from roomfinder.runner import SourceRunner
from roomfinder.scheduler import Scheduler
from roomfinder.settings_store import get_criteria
from roomfinder.sources import build_sources
from roomfinder.web.accounts import account_router, admin_router, public_router
from roomfinder.web.novnc import add_novnc
from roomfinder.web.routes import configure_templates
from roomfinder.web.routes import router as web_router
from roomfinder.web.security import LoginRequired, current_user, login_required_response

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"
# Everything else needs a signed-in user
PUBLIC_PATHS = frozenset({"/login", "/logout", "/register", "/reset", "/health"})
PUBLIC_PREFIXES = ("/static/",)
# And these need an admin (noVNC and /browser control the container's browser)
ADMIN_PREFIXES = ("/novnc", "/users", "/browser")


class RedactSecrets(logging.Filter):
    """Keeps secrets such as the bot token (which libraries put in URLs) out of the logs."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(secret in message for secret in self.secrets):
            for secret in self.secrets:
                message = message.replace(secret, "<redacted>")
            record.msg, record.args = message, None
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    redact = RedactSecrets(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    for noisy in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


async def start_telegram(settings: Settings, sessionmaker, sources) -> TelegramService | None:
    if not settings.telegram_bot_token:
        log.info("TELEGRAM_BOT_TOKEN is not set; notifications go to the log only")
        return None
    labels = {name: source.label for name, source in sources.items()}
    telegram = TelegramService(settings, sessionmaker, labels)
    try:
        await telegram.start()
    except Exception:
        log.exception("Telegram failed to start (is the token right?)")
        with contextlib.suppress(Exception):
            await telegram.stop()
        return None
    return telegram


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, [settings.telegram_bot_token])
        engine = create_engine(settings.sqlalchemy_url)
        sessionmaker = create_sessionmaker(engine)
        browser = BrowserManager(settings.cdp_url)
        sources = build_sources()
        telegram = await start_telegram(settings, sessionmaker, sources)
        notifier: Notifier = telegram or LogNotifier()
        runner = SourceRunner(sessionmaker, browser, notifier, sources, settings)
        scheduler = Scheduler(runner, sessionmaker, sources, settings, browser)

        app.state.settings = settings
        app.state.sessionmaker = sessionmaker
        app.state.browser = browser
        app.state.sources = sources
        app.state.runner = runner
        app.state.telegram = telegram

        await runner.close_interrupted_runs()
        async with sessionmaker() as session:
            if moved := await refresh_areas(session, await get_criteria(session)):
                log.info("Placed %d listings in neighborhoods", moved)
            await session.commit()
        if settings.scheduler_enabled:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            # Checks started from the dashboard, cut off before the browser goes away
            for task in app.state.background_tasks:
                task.cancel()
            await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
            if telegram is not None:
                await telegram.stop()
            await browser.stop()
            await engine.dispose()

    # No generated API docs: they would list every endpoint to anyone
    app = FastAPI(
        title="RoomFinder",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.background_tasks = set()
    app.state.login_throttle = LoginThrottle()
    app.add_exception_handler(LoginRequired, login_required_response)

    # Middleware added later runs first: host check, headers, cross-site check, sign-in check

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        # Deny by default: a visitor who isn't signed in gets only the sign-in pages, even for
        # paths that don't exist, so nothing (not even which pages exist) is revealed
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        user = await current_user(request)
        if user is None:
            return login_required_response(request, LoginRequired())
        if path.startswith(ADMIN_PREFIXES) and not user.is_admin:
            return PlainTextResponse("Admins only", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def reject_cross_site_writes(request: Request, call_next):
        # Another site open in the browser must not be able to change anything here
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and origin
            and urlsplit(origin).hostname not in settings.allowed_hosts
        ):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # No framing (clickjacking), no guessing content types, no leaking invite links
        # to other sites through the Referer header, and no caching of private pages
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith(PUBLIC_PREFIXES):
            # Stylesheets and scripts are checked with the server on every load (a cheap
            # "not modified"), so after an update no page runs with last version's files
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    # The loopback names stay allowed for Docker's health check; DNS rebinding can't use them
    hosts = list(dict.fromkeys([*settings.allowed_hosts, "localhost", "127.0.0.1"]))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        # Only for Docker's check from inside the container; hidden from everyone else
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(404)
        return {"status": "ok"}

    configure_templates(settings)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    add_novnc(app)
    app.include_router(public_router)
    app.include_router(account_router)
    app.include_router(admin_router)
    app.include_router(web_router)
    return app


app = create_app()
