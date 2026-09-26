"""Runs one check of one source: fetch, store, notify, and keep track of failures."""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, RunStatus, SourceRun, SourceState
from roomfinder.notify import Notifier
from roomfinder.pipeline import IngestOutcome, apply_details, ingest, mark_removed
from roomfinder.schemas import SearchCriteria
from roomfinder.settings_store import Preferences, get_criteria, get_preferences
from roomfinder.sources.base import BlockedError, FetchResult, Source, human_pause
from roomfinder.timeutils import in_daily_window, local_time

log = logging.getLogger(__name__)

FAILURE_ALERT_THRESHOLD = 3
# Older listings whose pages are read per run, besides the new ones
DETAILS_BACKFILL_PER_RUN = 4
# An empty result from a source that had this many live listings means the site changed
SUSPICIOUS_EMPTY_THRESHOLD = 5


class BrowserSessions(Protocol):
    def session(self) -> AbstractAsyncContextManager[Any]: ...


@dataclass
class _Stored:
    outcome: IngestOutcome
    preferences: Preferences
    baseline: bool
    recovered: bool
    matching: int


@dataclass
class RunReport:
    source: str
    status: RunStatus
    found: int = 0
    new: int = 0
    baseline: bool = False
    error: str | None = None


def fetch_signature(criteria: SearchCriteria, custom_url: str | None) -> str:
    payload = json.dumps(
        {"criteria": criteria.fetch_fields(), "custom_url": custom_url or None},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def get_source_state(session: AsyncSession, name: str) -> SourceState:
    state = await session.get(SourceState, name)
    if state is None:
        state = SourceState(source=name, enabled=True, consecutive_failures=0)
        session.add(state)
    return state


async def _count_listings(session: AsyncSession, name: str, *, matching: bool = False) -> int:
    query = select(func.count(Listing.id)).where(
        Listing.source == name, Listing.removed_at.is_(None)
    )
    if matching:
        query = query.where(Listing.matches.is_(True), Listing.duplicate_of_id.is_(None))
    return await session.scalar(query) or 0


class SourceRunner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        browser: BrowserSessions,
        notifier: Notifier,
        sources: Mapping[str, Source],
        settings: Settings,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.browser = browser
        self.notifier = notifier
        self.sources = sources
        self.settings = settings
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def is_running(self, name: str) -> bool:
        return self._locks[name].locked()

    async def close_interrupted_runs(self) -> int:
        """Runs left "running" by a previous process were cut off by a restart."""
        async with self.sessionmaker() as session:
            result = await session.execute(
                update(SourceRun)
                .where(SourceRun.status == RunStatus.RUNNING)
                .values(
                    status=RunStatus.ERROR, error="Interrupted by a restart", finished_at=utcnow()
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def run(self, name: str) -> RunReport:
        lock = self._locks[name]
        if lock.locked():
            return RunReport(name, RunStatus.RUNNING, error="already running")
        async with lock:
            return await self._run(self.sources[name])

    async def _run(self, source: Source) -> RunReport:
        async with self.sessionmaker() as session:
            state = await get_source_state(session, source.name)
            criteria = await get_criteria(session)
            custom_url = state.custom_url
            run = SourceRun(source=source.name, started_at=utcnow(), status=RunStatus.RUNNING)
            session.add(run)
            await session.commit()
            run_id = run.id

        try:
            async with self.browser.session() as page:
                result = await source.fetch(page, criteria, custom_url)
                stored = await self._store(source, run_id, criteria, custom_url, result)
                if isinstance(stored, RunReport):
                    return stored
                if source.has_details:
                    await self._add_details(source, page, run_id, stored.outcome, criteria)
        except BlockedError as exc:
            return await self._fail(source, run_id, RunStatus.BLOCKED, str(exc) or "bot check")
        except Exception as exc:
            log.exception("Source %s failed", source.name)
            return await self._fail(source, run_id, RunStatus.ERROR, f"{type(exc).__name__}: {exc}")

        outcome = stored.outcome
        log.info(
            "%s: found %d, new %d, price drops %d, removed %d%s",
            source.name, outcome.found, len(outcome.to_notify), len(outcome.price_drops),
            outcome.removed, " (baseline)" if stored.baseline else "",
        )  # fmt: skip
        await self._notify(
            source, outcome, stored.preferences, stored.baseline, stored.recovered, stored.matching
        )
        return RunReport(
            source.name, RunStatus.OK, outcome.found, len(outcome.to_notify), stored.baseline
        )

    async def _store(
        self,
        source: Source,
        run_id: int,
        criteria: SearchCriteria,
        custom_url: str | None,
        result: FetchResult,
    ) -> "_Stored | RunReport":
        now = utcnow()
        async with self.sessionmaker() as session:
            state = await get_source_state(session, source.name)
            if not result.listings and (
                await _count_listings(session, source.name) >= SUSPICIOUS_EMPTY_THRESHOLD
            ):
                return await self._fail(
                    source, run_id, RunStatus.ERROR, "No listings found - the site may have changed"
                )

            signature = fetch_signature(criteria, custom_url)
            baseline = state.fetch_signature != signature
            outcome = await ingest(
                session, source.name, result.listings, criteria, baseline=baseline, now=now
            )
            if result.complete:
                outcome.removed = await mark_removed(session, source.name, now)

            recovered = state.failure_alert_sent
            state.fetch_signature = signature
            state.consecutive_failures = 0
            state.failure_alert_sent = False
            state.last_run_at = state.last_success_at = now
            state.last_error = None

            run = await session.get(SourceRun, run_id)
            run.status = RunStatus.OK
            run.finished_at = now
            run.found = outcome.found
            run.new = len(outcome.to_notify)
            run.baseline = baseline

            preferences = await get_preferences(session)
            matching = await _count_listings(session, source.name, matching=True)
            await session.commit()
        return _Stored(outcome, preferences, baseline, recovered, matching)

    async def _add_details(
        self,
        source: Source,
        page: Any,
        run_id: int,
        outcome: IngestOutcome,
        criteria: SearchCriteria,
    ) -> None:
        """Read the pages of new listings (and a few older ones) for furniture, parking and text.

        A new listing's page can rule it out (e.g. unfurnished when furniture is required), so
        this happens before notifying.
        """
        new_ids = [listing.id for listing in outcome.to_notify]
        async with self.sessionmaker() as session:
            backfill = (
                select(Listing.id)
                .where(
                    Listing.source == source.name,
                    Listing.details_checked_at.is_(None),
                    Listing.removed_at.is_(None),
                    Listing.duplicate_of_id.is_(None),
                    Listing.matches.is_(True),
                    Listing.status != ListingStatus.HIDDEN,
                    Listing.id.not_in(new_ids),
                )
                .order_by(Listing.first_seen_at.desc())
                .limit(DETAILS_BACKFILL_PER_RUN)
            )
            targets = new_ids + list(await session.scalars(backfill))

        for index, listing_id in enumerate(targets):
            if index:
                await human_pause(2.0, 5.0)
            async with self.sessionmaker() as session:
                listing = await session.get(Listing, listing_id)
                try:
                    details = await source.fetch_details(page, listing.source_id, listing.url)
                except BlockedError as exc:
                    log.warning("%s: stopped reading listing pages: %s", source.name, exc)
                    break
                except Exception:
                    log.exception("%s: reading listing %s failed", source.name, listing.url)
                    continue
                apply_details(listing, details, criteria, utcnow())
                await session.commit()

        if not new_ids:
            return
        async with self.sessionmaker() as session:
            still_matching = []
            for listing_id in new_ids:
                listing = await session.get(Listing, listing_id)
                if listing.matches:
                    still_matching.append(listing)
                else:
                    listing.status = ListingStatus.SEEN
            run = await session.get(SourceRun, run_id)
            run.new = len(still_matching)
            await session.commit()
        outcome.to_notify = still_matching

    async def _notify(
        self,
        source: Source,
        outcome: IngestOutcome,
        preferences: Preferences,
        baseline: bool,
        recovered: bool,
        matching: int,
    ) -> None:
        silent = in_daily_window(
            local_time(utcnow(), self.settings.timezone),
            preferences.quiet_hours_start,
            preferences.quiet_hours_end,
        )
        if recovered:
            await self._safely(self.notifier.message(f"✅ {source.label} חזר לעבוד", silent=silent))
        if preferences.notifications_paused:
            return
        if baseline:
            text = (
                f"🔎 {source.label}: נמצאו {matching} מודעות מתאימות שכבר מפורסמות "
                f"(אפשר לראות אותן בדשבורד).\nמעכשיו תגיע התראה רק על מודעות חדשות."
            )
            await self._safely(self.notifier.message(text, silent=silent))
        for listing in outcome.to_notify:
            await self._safely(self.notifier.new_listing(listing, silent=silent))
        if preferences.notify_price_drops:
            for drop in outcome.price_drops:
                await self._safely(
                    self.notifier.price_drop(
                        drop.listing, drop.old_price, drop.new_price, silent=silent
                    )
                )

    async def _fail(self, source: Source, run_id: int, status: RunStatus, error: str) -> RunReport:
        now = utcnow()
        async with self.sessionmaker() as session:
            state = await get_source_state(session, source.name)
            state.consecutive_failures += 1
            state.last_run_at = now
            state.last_error = error
            send_alert = (
                state.consecutive_failures >= FAILURE_ALERT_THRESHOLD
                and not state.failure_alert_sent
            )
            if send_alert:
                state.failure_alert_sent = True
            failures = state.consecutive_failures
            run = await session.get(SourceRun, run_id)
            run.status = status
            run.finished_at = now
            run.error = error
            await session.commit()

        log.warning("%s: %s (%s), failure #%d", source.name, status, error, failures)
        if send_alert:
            reason = "חסימת בוט / קפצ'ה" if status == RunStatus.BLOCKED else "שגיאה"
            text = (
                f"⚠️ {source.label} נכשל {failures} פעמים ברצף ({reason}).\n{error}\n"
                f"אפשר לראות מה הדפדפן של המערכת מציג ולפתור קפצ'ה ידנית כאן:\n"
                f"{self.settings.novnc_url}"
            )
            await self._safely(self.notifier.message(text))
        return RunReport(source.name, status, error=error)

    @staticmethod
    async def _safely(notification: Awaitable[None]) -> None:
        try:
            await notification
        except Exception:
            log.exception("Sending a notification failed")
