import contextlib
import random
from datetime import time, timedelta

import pytest
from sqlalchemy import select

from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, RunStatus, SourceRun, SourceState
from roomfinder.runner import FAILURE_ALERT_THRESHOLD, SourceRunner
from roomfinder.scheduler import MAX_BACKOFF_MINUTES, first_delay_seconds, next_delay_seconds
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.settings_store import Preferences, save_criteria, save_preferences
from roomfinder.sources.base import BlockedError, FetchResult, Source


class FakeBrowser:
    @contextlib.asynccontextmanager
    async def session(self):
        yield None


class FakeSource(Source):
    name = "fake"
    label = "אתר בדיקה"

    def __init__(self) -> None:
        self.results: list[FetchResult | Exception] = []

    def search_url(self, criteria: SearchCriteria) -> str:
        return "https://example.com/search"

    async def fetch(self, page, criteria, custom_url) -> FetchResult:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordingNotifier:
    def __init__(self) -> None:
        self.new: list[tuple[str, bool]] = []
        self.drops: list[tuple[str, int, int]] = []
        self.messages: list[str] = []

    async def new_listing(self, listing, *, silent):
        self.new.append((listing.source_id, silent))

    async def price_drop(self, listing, old_price, new_price, *, silent):
        self.drops.append((listing.source_id, old_price, new_price))

    async def message(self, text, *, silent=False):
        self.messages.append(text)


def listings(*ids: str, price: int = 6000) -> FetchResult:
    return FetchResult(
        [
            ListingData(
                source="fake",
                source_id=source_id,
                url=f"https://example.com/{source_id}",
                city="ראשון לציון",
                rooms=4,
                price=price,
                property_type=PropertyType.APARTMENT,
                street=f"רחוב {source_id}",
                house_number="1",
            )
            for source_id in ids
        ]
    )


@pytest.fixture
def source():
    return FakeSource()


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def runner(sessionmaker, source, notifier):
    settings = Settings(timezone="Asia/Jerusalem")
    return SourceRunner(sessionmaker, FakeBrowser(), notifier, {"fake": source}, settings)


@pytest.fixture
async def no_quiet_hours(sessionmaker):
    async with sessionmaker() as session:
        await save_preferences(
            session, Preferences(quiet_hours_start=time(0), quiet_hours_end=time(0))
        )
        await session.commit()


async def test_first_run_is_a_silent_baseline_with_a_summary(runner, source, notifier):
    source.results = [listings("a", "b")]

    report = await runner.run("fake")

    assert report.status == RunStatus.OK
    assert report.baseline is True
    assert notifier.new == []
    assert len(notifier.messages) == 1
    assert "2 מודעות" in notifier.messages[0]


async def test_new_listing_after_baseline_is_notified(runner, source, notifier, no_quiet_hours):
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.new == 1
    assert notifier.new == [("b", False)]


async def test_quiet_hours_send_silently(runner, source, notifier, sessionmaker):
    async with sessionmaker() as session:
        # A window covering the whole day
        await save_preferences(
            session, Preferences(quiet_hours_start=time(0), quiet_hours_end=time(23, 59, 59))
        )
        await session.commit()
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.new == [("b", True)]


async def test_changing_the_search_starts_a_new_baseline(runner, source, notifier, sessionmaker):
    source.results = [listings("a"), listings("a", "b")]
    await runner.run("fake")
    async with sessionmaker() as session:
        await save_criteria(session, SearchCriteria(rooms_min=3.5, rooms_max=4.5))
        await session.commit()

    report = await runner.run("fake")

    assert report.baseline is True
    assert notifier.new == []


async def test_paused_notifications_still_store_new_listings(
    runner, source, notifier, sessionmaker
):
    async with sessionmaker() as session:
        await save_preferences(session, Preferences(notifications_paused=True))
        await session.commit()
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.new == []
    assert notifier.messages == []
    async with sessionmaker() as session:
        new = await session.scalar(select(Listing).where(Listing.source_id == "b"))
        assert new.status == ListingStatus.NEW


async def test_price_drop_is_notified(runner, source, notifier, no_quiet_hours):
    source.results = [listings("a", price=6500), listings("a", price=6000)]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.drops == [("a", 6500, 6000)]


async def test_repeated_failures_alert_once_and_recovery_is_reported(
    runner, source, notifier, sessionmaker
):
    source.results = [BlockedError("captcha")] * (FAILURE_ALERT_THRESHOLD + 1) + [listings("a")]

    reports = [await runner.run("fake") for _ in range(FAILURE_ALERT_THRESHOLD + 2)]

    assert [report.status for report in reports[:-1]] == [RunStatus.BLOCKED] * (
        FAILURE_ALERT_THRESHOLD + 1
    )
    alerts = [text for text in notifier.messages if text.startswith("⚠️")]
    assert len(alerts) == 1
    assert any(text.startswith("✅") for text in notifier.messages)
    async with sessionmaker() as session:
        state = await session.get(SourceState, "fake")
        assert state.consecutive_failures == 0
        runs = (await session.scalars(select(SourceRun))).all()
        assert len(runs) == FAILURE_ALERT_THRESHOLD + 2


async def test_unexpected_errors_are_recorded(runner, source):
    source.results = [ValueError("bad json")]

    report = await runner.run("fake")

    assert report.status == RunStatus.ERROR
    assert "bad json" in report.error


async def test_empty_result_from_a_busy_source_is_an_error(runner, source):
    source.results = [listings("a", "b", "c", "d", "e"), FetchResult([])]

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.status == RunStatus.ERROR


async def test_runs_cut_off_by_a_restart_are_closed(runner, sessionmaker):
    async with sessionmaker() as session:
        session.add(SourceRun(source="fake", status=RunStatus.RUNNING))
        session.add(SourceRun(source="fake", status=RunStatus.OK))
        await session.commit()

    assert await runner.close_interrupted_runs() == 1

    async with sessionmaker() as session:
        statuses = [run.status for run in await session.scalars(select(SourceRun))]
        assert sorted(statuses) == [RunStatus.ERROR, RunStatus.OK]


def test_first_check_after_start_continues_the_schedule():
    rng = random.Random(1)
    now = utcnow()

    never = first_delay_seconds(None, 10, now, rng)
    recent = first_delay_seconds(now - timedelta(minutes=2), 10, now, rng)
    overdue = first_delay_seconds(now - timedelta(hours=1), 10, now, rng)

    assert 5 <= never <= 45
    assert recent == pytest.approx(8 * 60, abs=1)
    assert 5 <= overdue <= 45


def test_next_delay_uses_night_interval_jitter_and_backoff():
    rng = random.Random(1)
    day = next_delay_seconds(10, 30, failures=0, is_night=False, rng=rng)
    night = next_delay_seconds(10, 30, failures=0, is_night=True, rng=rng)
    backoff = next_delay_seconds(10, 30, failures=10, is_night=False, rng=rng)

    assert 10 * 60 * 0.75 <= day <= 10 * 60 * 1.25
    assert 30 * 60 * 0.75 <= night <= 30 * 60 * 1.25
    assert backoff <= MAX_BACKOFF_MINUTES * 60 * 1.25
