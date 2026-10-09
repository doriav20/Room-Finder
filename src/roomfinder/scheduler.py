"""Checks every source periodically, with jitter, a slower night pace and failure backoff."""

import asyncio
import logging
import random
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from roomfinder.browser import BrowserManager
from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.runner import SourceRunner, get_source_state
from roomfinder.settings_store import get_schedule
from roomfinder.sources.base import Source
from roomfinder.timeutils import in_daily_window, local_time

log = logging.getLogger(__name__)

JITTER = 0.25
MAX_BACKOFF_MINUTES = 120
BROWSER_RESTART_HOURS = 12


def next_delay_seconds(
    interval_minutes: float,
    night_interval_minutes: float,
    failures: int,
    is_night: bool,
    rng: random.Random,
) -> float:
    minutes = max(interval_minutes, night_interval_minutes) if is_night else interval_minutes
    if failures:
        minutes = min(minutes * 2**failures, MAX_BACKOFF_MINUTES)
    return minutes * 60 * rng.uniform(1 - JITTER, 1 + JITTER)


def first_delay_seconds(
    last_run_at: datetime | None, interval_minutes: float, now: datetime, rng: random.Random
) -> float:
    """When to check a source after the app starts.

    The schedule continues from the last check, so restarting the app doesn't make every
    site get checked again right away. A short random delay spreads the first checks.
    """
    stagger = rng.uniform(5, 45)
    if last_run_at is None:
        return stagger
    due_in = (last_run_at + timedelta(minutes=interval_minutes) - now).total_seconds()
    return max(stagger, due_in)


class Scheduler:
    def __init__(
        self,
        runner: SourceRunner,
        sessionmaker: async_sessionmaker[AsyncSession],
        sources: Mapping[str, Source],
        settings: Settings,
        browser: BrowserManager | None = None,
    ) -> None:
        self.runner = runner
        self.sessionmaker = sessionmaker
        self.sources = sources
        self.settings = settings
        self.browser = browser
        self._rng = random.Random()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        for name in self.sources:
            self._tasks.append(asyncio.create_task(self._loop(name), name=f"source:{name}"))
        if self.browser is not None:
            self._tasks.append(asyncio.create_task(self._restart_browser_periodically()))
        log.info("Scheduler started for %s", ", ".join(self.sources) or "no sources")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, name: str) -> None:
        await asyncio.sleep(await self._first_delay(name))
        while True:
            try:
                if await self._enabled(name):
                    await self.runner.run(name)
                delay = await self._next_delay(name)
            except Exception:
                log.exception("Scheduling %s failed", name)
                delay = 300
            log.debug("Next %s check in %.0f minutes", name, delay / 60)
            await asyncio.sleep(delay)

    async def _first_delay(self, name: str) -> float:
        async with self.sessionmaker() as session:
            state = await get_source_state(session, name)
            await session.commit()
        return first_delay_seconds(
            state.last_run_at,
            state.interval_minutes or self.sources[name].default_interval_minutes,
            utcnow(),
            self._rng,
        )

    async def _enabled(self, name: str) -> bool:
        async with self.sessionmaker() as session:
            state = await get_source_state(session, name)
            await session.commit()
            return state.enabled

    async def _next_delay(self, name: str) -> float:
        source = self.sources[name]
        async with self.sessionmaker() as session:
            state = await get_source_state(session, name)
            schedule = await get_schedule(session)
        now = local_time(utcnow(), self.settings.timezone)
        return next_delay_seconds(
            state.interval_minutes or source.default_interval_minutes,
            source.night_interval_minutes,
            state.consecutive_failures,
            in_daily_window(now, schedule.night_start, schedule.night_end),
            self._rng,
        )

    async def _restart_browser_periodically(self) -> None:
        while True:
            await asyncio.sleep(BROWSER_RESTART_HOURS * 3600)
            try:
                await self.browser.restart()
            except Exception:
                log.exception("Restarting Chrome failed")
