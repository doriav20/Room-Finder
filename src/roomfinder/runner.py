"""Runs one check of one source: fetch, store, notify, and keep track of failures."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.models import (
    Listing,
    ListingMark,
    ListingStatus,
    Role,
    RunStatus,
    SourceRun,
    SourceState,
    User,
)
from roomfinder.notify import Notifier
from roomfinder.pipeline import IngestOutcome, apply_details, ingest, interested, mark_removed
from roomfinder.schemas import ListingData, SearchCriteria
from roomfinder.searchers import FetchScope, Searcher, combined_searches, load_searchers
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
    baseline: bool
    recovered: bool


@dataclass
class RunReport:
    source: str
    status: RunStatus
    found: int = 0
    new: int = 0
    baseline: bool = False
    error: str | None = None


def fetch_plan(searchers: Sequence[Searcher], custom_url: str | None) -> list[SearchCriteria]:
    """The searches to read a source with: the active users' searches combined, one per city.

    A custom URL replaces them and is read once. The source still gets a search with it,
    which some use for the parameters of the site's own API.
    """
    searches = combined_searches(searcher.criteria for searcher in searchers if searcher.active)
    if custom_url:
        return searches[:1] or [SearchCriteria()]
    return searches


async def get_source_state(session: AsyncSession, name: str) -> SourceState:
    state = await session.get(SourceState, name)
    if state is None:
        state = SourceState(source=name, enabled=True, consecutive_failures=0)
        session.add(state)
    return state


async def _count_listings(session: AsyncSession, name: str) -> int:
    query = select(func.count(Listing.id)).where(
        Listing.source == name, Listing.removed_at.is_(None)
    )
    return await session.scalar(query) or 0


async def _admin_ids(session: AsyncSession) -> list[int]:
    query = select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True))
    return list(await session.scalars(query))


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
            searchers = await load_searchers(session)
            custom_url = state.custom_url
            plan = fetch_plan(searchers, custom_url)
            if not plan:
                await session.commit()
                log.info("%s: nobody is searching yet", source.name)
                return RunReport(source.name, RunStatus.OK)
            run = SourceRun(source=source.name, started_at=utcnow(), status=RunStatus.RUNNING)
            session.add(run)
            await session.commit()
            run_id = run.id
        scope = FetchScope.of(plan, custom_url)

        try:
            async with self.browser.session() as page:
                result = await self._fetch(source, page, plan, custom_url)
                stored = await self._store(source, run_id, searchers, scope, result)
                if isinstance(stored, RunReport):
                    return stored
                if source.has_details:
                    await self._add_details(source, page, run_id, stored.outcome, searchers)
        except BlockedError as exc:
            return await self._fail(source, run_id, RunStatus.BLOCKED, str(exc) or "bot check")
        except Exception as exc:
            log.exception("Source %s failed", source.name)
            return await self._fail(source, run_id, RunStatus.ERROR, f"{type(exc).__name__}: {exc}")

        outcome = stored.outcome
        log.info(
            "%s: found %d, new %d, price drops %d, removed %d%s",
            source.name, outcome.found, len(outcome.new), len(outcome.price_drops),
            outcome.removed, " (the search changed)" if stored.baseline else "",
        )  # fmt: skip
        await self._notify(source, outcome, searchers, stored.recovered)
        return RunReport(
            source.name, RunStatus.OK, outcome.found, len(outcome.new), stored.baseline
        )

    @staticmethod
    async def _fetch(
        source: Source, page: Any, plan: list[SearchCriteria], custom_url: str | None
    ) -> FetchResult:
        """Read the source once for each search in the plan (one per city that users search)."""
        listings: list[ListingData] = []
        complete = True
        for index, search in enumerate(plan):
            if index:
                await human_pause(5.0, 15.0)
            result = await source.fetch(page, search, custom_url)
            listings += result.listings
            complete = complete and result.complete
        return FetchResult(listings, complete)

    async def _store(
        self,
        source: Source,
        run_id: int,
        searchers: list[Searcher],
        scope: FetchScope,
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

            previous = FetchScope.model_validate(state.fetch_scope) if state.fetch_scope else None
            outcome = await ingest(
                session,
                source.name,
                result.listings,
                searchers,
                previous=previous,
                current=scope,
                now=now,
            )
            if result.complete:
                outcome.removed = await mark_removed(session, source.name, now)

            recovered = state.failure_alert_sent
            state.fetch_scope = scope.model_dump(mode="json")
            state.consecutive_failures = 0
            state.failure_alert_sent = False
            state.last_run_at = state.last_success_at = now
            state.last_error = None

            # The source was read differently: what only the new reading reaches was stored quietly
            baseline = previous != scope
            run = await session.get(SourceRun, run_id)
            run.status = RunStatus.OK
            run.finished_at = now
            run.found = outcome.found
            run.new = len(outcome.new)
            run.baseline = baseline
            await session.commit()
        return _Stored(outcome, baseline, recovered)

    async def _add_details(
        self,
        source: Source,
        page: Any,
        run_id: int,
        outcome: IngestOutcome,
        searchers: list[Searcher],
    ) -> None:
        """Read the pages of new listings (and a few older ones) for furniture, parking and text.

        A new listing's page can rule it out (e.g. unfurnished when furniture is required), so
        this happens before notifying.
        """
        new_ids = [listing.id for listing in outcome.new]
        async with self.sessionmaker() as session:
            # Older listings that someone wants: they fit an active user's search, who kept them
            wanted = (
                select(ListingMark.listing_id)
                .join(User, User.id == ListingMark.user_id)
                .where(
                    ListingMark.matches.is_(True),
                    ListingMark.status != ListingStatus.HIDDEN,
                    User.is_active.is_(True),
                )
            )
            backfill = (
                select(Listing.id)
                .where(
                    Listing.source == source.name,
                    Listing.details_checked_at.is_(None),
                    Listing.removed_at.is_(None),
                    Listing.duplicate_of_id.is_(None),
                    Listing.id.in_(wanted),
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
                apply_details(listing, details, searchers, utcnow())
                await session.commit()

        if not new_ids:
            return
        async with self.sessionmaker() as session:
            still_new = []
            for listing_id in new_ids:
                listing = await session.get(Listing, listing_id)
                for found in listing.marks.values():
                    if found.status == ListingStatus.NEW and not found.matches:
                        found.status = ListingStatus.SEEN
                if any(found.status == ListingStatus.NEW for found in listing.marks.values()):
                    still_new.append(listing)
            run = await session.get(SourceRun, run_id)
            run.new = len(still_new)
            await session.commit()
        outcome.new = still_new

    async def _notify(
        self,
        source: Source,
        outcome: IngestOutcome,
        searchers: list[Searcher],
        recovered: bool,
    ) -> None:
        """Tell each user what fits their own search, quietly in their own quiet hours."""
        now = local_time(utcnow(), self.settings.timezone)
        for searcher in searchers:
            if not searcher.active:
                continue
            user, preferences = searcher.user_id, searcher.preferences
            silent = in_daily_window(
                now, preferences.quiet_hours_start, preferences.quiet_hours_end
            )
            if recovered and searcher.is_admin:
                text = f"✅ {source.label} חזר לעבוד"
                await self._safely(self.notifier.message(user, text, silent=silent))
            if preferences.notifications_paused:
                continue
            if count := outcome.already_listed[user]:
                text = (
                    f"🔎 {source.label}: נמצאו {count} מודעות מתאימות שכבר מפורסמות "
                    f"(אפשר לראות אותן בדשבורד).\nמעכשיו תגיע התראה רק על מודעות חדשות."
                )
                await self._safely(self.notifier.message(user, text, silent=silent))
            for listing in outcome.new:
                found = listing.marks.get(user)
                if found is not None and found.status == ListingStatus.NEW:
                    await self._safely(self.notifier.new_listing(user, listing, silent=silent))
            if not preferences.notify_price_drops:
                continue
            for drop in outcome.price_drops:
                if user in interested(drop.listing):
                    notification = self.notifier.price_drop(
                        user, drop.listing, drop.old_price, drop.new_price, silent=silent
                    )
                    await self._safely(notification)

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
            admins = await _admin_ids(session)
            await session.commit()

        log.warning("%s: %s (%s), failure #%d", source.name, status, error, failures)
        if send_alert:
            # Only admins control the sources and the system's browser
            reason = "חסימת בוט / קפצ'ה" if status == RunStatus.BLOCKED else "שגיאה"
            text = (
                f"⚠️ {source.label} נכשל {failures} פעמים ברצף ({reason}).\n{error}\n"
                f"אפשר לראות מה הדפדפן של המערכת מציג ולפתור קפצ'ה ידנית כאן:\n"
                f"{self.settings.novnc_url}"
            )
            for admin in admins:
                await self._safely(self.notifier.message(admin, text))
        return RunReport(source.name, status, error=error)

    @staticmethod
    async def _safely(notification: Awaitable[None]) -> None:
        try:
            await notification
        except Exception:
            log.exception("Sending a notification failed")
