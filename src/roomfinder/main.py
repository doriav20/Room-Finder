import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from roomfinder import __version__
from roomfinder.browser import BrowserManager
from roomfinder.config import Settings, get_settings
from roomfinder.db import create_engine, create_sessionmaker
from roomfinder.notify import LogNotifier, Notifier
from roomfinder.notify.telegram import TelegramService
from roomfinder.runner import SourceRunner
from roomfinder.scheduler import Scheduler
from roomfinder.sources import build_sources
from roomfinder.web.routes import configure_templates
from roomfinder.web.routes import router as web_router

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


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
        if settings.scheduler_enabled:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            if telegram is not None:
                await telegram.stop()
            await browser.stop()
            await engine.dispose()

    app = FastAPI(title="RoomFinder", version=__version__, lifespan=lifespan)
    app.state.background_tasks = set()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

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

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "browser_connected": app.state.browser.connected,
            "telegram": app.state.telegram is not None,
        }

    configure_templates(settings)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(web_router)
    return app


app = create_app()
