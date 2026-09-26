import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from roomfinder import __version__
from roomfinder.browser import BrowserManager
from roomfinder.config import Settings, get_settings
from roomfinder.db import create_engine, create_sessionmaker
from roomfinder.notify import LogNotifier
from roomfinder.runner import SourceRunner
from roomfinder.scheduler import Scheduler
from roomfinder.sources import build_sources


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for noisy in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        engine = create_engine(settings.sqlalchemy_url)
        sessionmaker = create_sessionmaker(engine)
        browser = BrowserManager(settings.cdp_url)
        sources = build_sources()
        runner = SourceRunner(sessionmaker, browser, LogNotifier(), sources, settings)
        scheduler = Scheduler(runner, sessionmaker, sources, settings, browser)

        app.state.settings = settings
        app.state.sessionmaker = sessionmaker
        app.state.browser = browser
        app.state.sources = sources
        app.state.runner = runner

        if settings.scheduler_enabled:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await browser.stop()
            await engine.dispose()

    app = FastAPI(title="RoomFinder", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "browser_connected": app.state.browser.connected}

    return app


app = create_app()
