import contextlib
import random
from datetime import time, timedelta

import pytest
from sqlalchemy import func, select

from conftest import HOME, ROUTING_URL, bus_line
from roomfinder import runner as runner_module
from roomfinder import walking
from roomfinder.config import Settings
from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, Role, RunStatus, SourceRun, SourceState, User
from roomfinder.runner import DETAILS_BACKFILL_PER_RUN, FAILURE_ALERT_THRESHOLD, SourceRunner
from roomfinder.scheduler import MAX_BACKOFF_MINUTES, first_delay_seconds, next_delay_seconds
from roomfinder.schemas import ListingData, ListingDetails, PropertyType, SearchCriteria
from roomfinder.settings_store import Preferences, set_user_criteria, set_user_preferences
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
        # Listing pages by source id: ListingDetails, None (page gone) or an exception
        self.pages: dict[str, ListingDetails | Exception | None] = {}
        self.pages_read: list[str] = []
        # The search each fetch was made with
        self.searches: list[SearchCriteria] = []

    def search_url(self, criteria: SearchCriteria) -> str:
        return "https://example.com/search"

    async def fetch(self, page, criteria, custom_url) -> FetchResult:
        self.searches.append(criteria)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fetch_details(self, page, source_id, url):
        self.pages_read.append(source_id)
        details = self.pages.get(source_id, ListingDetails())
        if isinstance(details, Exception):
            raise details
        return details


class RecordingNotifier:
    def __init__(self) -> None:
        # (user, source id, silent)
        self.new: list[tuple[int, str, bool]] = []
        self.new_listings: list[Listing] = []
        self.drops: list[tuple[int, str, int, int]] = []
        self.messages: list[tuple[int, str]] = []

    async def new_listing(self, user_id, listing, *, silent):
        self.new.append((user_id, listing.source_id, silent))
        self.new_listings.append(listing)

    async def price_drop(self, user_id, listing, old_price, new_price, *, silent):
        self.drops.append((user_id, listing.source_id, old_price, new_price))

    async def message(self, user_id, text, *, silent=False):
        self.messages.append((user_id, text))

    def texts(self, user_id: int | None = None) -> list[str]:
        return [text for user, text in self.messages if user_id in (None, user)]


def listings(*ids: str, price: int = 6000, rooms: float = 4) -> FetchResult:
    return FetchResult(
        [
            ListingData(
                source="fake",
                source_id=source_id,
                url=f"https://example.com/{source_id}",
                city="ראשון לציון",
                rooms=rooms,
                price=price,
                property_type=PropertyType.APARTMENT,
                street=f"רחוב {source_id}",
                house_number="1",
            )
            for source_id in ids
        ]
    )


async def add_user(
    sessionmaker,
    username: str,
    role: Role = Role.MEMBER,
    criteria: SearchCriteria | None = None,
    preferences: Preferences | None = None,
    active: bool = True,
) -> int:
    async with sessionmaker() as session:
        user = User(
            username=username,
            username_key=username,
            password_hash="x",
            role=role,
            is_active=active,
        )
        if criteria is not None:
            set_user_criteria(user, criteria)
        if preferences is not None:
            set_user_preferences(user, preferences)
        session.add(user)
        await session.commit()
        return user.id


async def change_user(sessionmaker, user_id: int, **changes) -> None:
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        if "criteria" in changes:
            set_user_criteria(user, changes.pop("criteria"))
        if "preferences" in changes:
            set_user_preferences(user, changes.pop("preferences"))
        for name, value in changes.items():
            setattr(user, name, value)
        await session.commit()


# Quiet hours that never start, so notifications aren't silent
LOUD = Preferences(quiet_hours_start=time(0), quiet_hours_end=time(0))


@pytest.fixture
def source():
    return FakeSource()


@pytest.fixture
def detailed_source(source, monkeypatch):
    """The fake source, with listing pages to read."""

    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(runner_module, "human_pause", instant)
    monkeypatch.setattr(FakeSource, "has_details", True)
    return source


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
async def admin(sessionmaker) -> int:
    return await add_user(sessionmaker, "admin", Role.ADMIN, preferences=LOUD)


@pytest.fixture
def runner(sessionmaker, source, notifier, admin):
    settings = Settings(timezone="Asia/Jerusalem")
    return SourceRunner(sessionmaker, FakeBrowser(), notifier, {"fake": source}, settings)


async def test_first_run_is_a_silent_baseline_with_a_summary(runner, source, notifier, admin):
    source.results = [listings("a", "b")]

    report = await runner.run("fake")

    assert report.status == RunStatus.OK
    assert report.baseline is True
    assert notifier.new == []
    [summary] = notifier.texts(admin)
    assert "2 מודעות" in summary


async def test_new_listing_after_baseline_is_notified(runner, source, notifier, admin):
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.new == 1
    assert notifier.new == [(admin, "b", False)]


async def test_quiet_hours_send_silently(runner, source, notifier, sessionmaker, admin):
    # A window covering the whole day
    whole_day = Preferences(quiet_hours_start=time(0), quiet_hours_end=time(23, 59, 59))
    await change_user(sessionmaker, admin, preferences=whole_day)
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.new == [(admin, "b", True)]


async def test_each_user_hears_about_their_own_search_only(
    runner, source, notifier, sessionmaker, admin
):
    five = SearchCriteria(rooms_min=5, rooms_max=5)
    member = await add_user(sessionmaker, "noa", criteria=five, preferences=LOUD)
    source.results = [
        listings("a"),
        FetchResult(listings("a", "b").listings + listings("c", rooms=5).listings),
    ]

    await runner.run("fake")
    await runner.run("fake")

    assert sorted(notifier.new) == [(admin, "b", False), (member, "c", False)]
    # One reading of the site for both of them, covering both searches
    assert len(source.searches) == 2
    assert (source.searches[0].rooms_min, source.searches[0].rooms_max) == (4, 5)


async def test_widening_a_search_does_not_flood_or_silence_anyone(
    runner, source, notifier, sessionmaker, admin
):
    member = await add_user(sessionmaker, "noa", preferences=LOUD)
    four_and_five = listings("a", "b").listings + listings("c", rooms=5).listings
    source.results = [listings("a"), FetchResult(four_and_five)]
    await runner.run("fake")
    await change_user(sessionmaker, member, criteria=SearchCriteria(rooms_min=4, rooms_max=5))
    notifier.messages.clear()

    report = await runner.run("fake")

    assert report.baseline is True  # the source was read for 4-5 rooms instead of 4
    # The 4-room listing is new to both; the 5-room one was there before anyone looked for it
    assert sorted(notifier.new) == [(admin, "b", False), (member, "b", False)]
    assert any("נמצאו 1 מודעות" in text for text in notifier.texts(member))
    assert not any("נמצאו 1 מודעות" in text for text in notifier.texts(admin))


async def test_disabled_users_are_not_searched_for_or_notified(
    runner, source, notifier, sessionmaker, admin
):
    member = await add_user(
        sessionmaker, "noa", criteria=SearchCriteria(rooms_min=6, rooms_max=6), active=False
    )
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert (source.searches[0].rooms_min, source.searches[0].rooms_max) == (4, 4)
    assert all(user != member for user, *_ in notifier.new)


async def test_nothing_is_read_while_nobody_searches(sessionmaker, source, notifier):
    runner = SourceRunner(sessionmaker, FakeBrowser(), notifier, {"fake": source}, Settings())

    report = await runner.run("fake")

    assert report.status == RunStatus.OK and source.searches == []


async def test_changing_the_search_starts_a_new_baseline(
    runner, source, notifier, sessionmaker, admin
):
    wider = listings("a", "b").listings + listings("c", rooms=4.5).listings
    source.results = [listings("a"), FetchResult(wider)]
    await runner.run("fake")
    await change_user(sessionmaker, admin, criteria=SearchCriteria(rooms_min=3.5, rooms_max=4.5))
    notifier.messages.clear()

    report = await runner.run("fake")

    assert report.baseline is True
    # A 4-room listing, which the earlier search would have found too, is news; the 4.5-room
    # one is only summed up
    assert notifier.new == [(admin, "b", False)]
    [summary] = notifier.texts(admin)
    assert "נמצאו 1 מודעות" in summary


async def test_paused_notifications_still_store_new_listings(
    runner, source, notifier, sessionmaker, admin
):
    await change_user(sessionmaker, admin, preferences=Preferences(notifications_paused=True))
    source.results = [listings("a"), listings("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.new == []
    assert notifier.messages == []
    async with sessionmaker() as session:
        new = await session.scalar(select(Listing).where(Listing.source_id == "b"))
        assert new.marks[admin].status == ListingStatus.NEW


async def test_price_drop_is_notified(runner, source, notifier, admin):
    source.results = [listings("a", price=6500), listings("a", price=6000)]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.drops == [(admin, "a", 6500, 6000)]


async def test_repeated_failures_alert_admins_once_and_recovery_is_reported(
    runner, source, notifier, sessionmaker, admin
):
    member = await add_user(sessionmaker, "noa", preferences=LOUD)
    source.results = [BlockedError("captcha")] * (FAILURE_ALERT_THRESHOLD + 1) + [listings("a")]

    reports = [await runner.run("fake") for _ in range(FAILURE_ALERT_THRESHOLD + 2)]

    assert [report.status for report in reports[:-1]] == [RunStatus.BLOCKED] * (
        FAILURE_ALERT_THRESHOLD + 1
    )
    alerts = [text for text in notifier.texts(admin) if text.startswith("⚠️")]
    assert len(alerts) == 1
    assert any(text.startswith("✅") for text in notifier.texts(admin))
    # Members can't do anything about a source, so they don't hear about it
    assert not any(text.startswith(("⚠️", "✅")) for text in notifier.texts(member))
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


async def test_new_listing_page_is_read_before_notifying(runner, detailed_source, notifier):
    detailed_source.results = [listings("a"), listings("a", "b")]
    detailed_source.pages["b"] = ListingDetails(furnished=True, parking=True, parking_spaces=1)

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.new == 1
    [listing] = notifier.new_listings
    assert (listing.source_id, listing.furnished, listing.parking) == ("b", True, True)


async def test_listing_page_can_rule_out_a_new_listing(
    runner, detailed_source, notifier, sessionmaker, admin
):
    await change_user(sessionmaker, admin, criteria=SearchCriteria(furnished="yes"))
    detailed_source.results = [listings("a"), listings("a", "b")]
    detailed_source.pages["b"] = ListingDetails(furnished=False)

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.new == 0
    assert notifier.new == []
    async with sessionmaker() as session:
        listing = await session.scalar(select(Listing).where(Listing.source_id == "b"))
        assert (listing.marks[admin].matches, listing.marks[admin].status) == (
            False,
            ListingStatus.SEEN,
        )
        run = await session.scalar(select(SourceRun).order_by(SourceRun.id.desc()))
        assert run.new == 0


async def test_a_listing_page_rules_out_a_listing_only_for_whom_it_doesnt_fit(
    runner, detailed_source, notifier, sessionmaker, admin
):
    member = await add_user(
        sessionmaker, "noa", criteria=SearchCriteria(furnished="yes"), preferences=LOUD
    )
    detailed_source.results = [listings("a"), listings("a", "b")]
    detailed_source.pages["b"] = ListingDetails(furnished=False)

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.new == 1
    assert notifier.new == [(admin, "b", False)]
    assert member not in [user for user, *_ in notifier.new]


async def test_older_listing_pages_are_read_a_few_per_run(runner, detailed_source, sessionmaker):
    ids = [f"l{n}" for n in range(DETAILS_BACKFILL_PER_RUN + 2)]
    detailed_source.results = [listings(*ids), listings(*ids)]

    await runner.run("fake")
    assert len(detailed_source.pages_read) == DETAILS_BACKFILL_PER_RUN
    await runner.run("fake")

    assert sorted(detailed_source.pages_read) == sorted(ids)
    async with sessionmaker() as session:
        unchecked = await session.scalar(
            select(func.count()).where(Listing.details_checked_at.is_(None))
        )
        assert unchecked == 0


async def test_older_listings_nobody_wants_are_not_read(runner, detailed_source):
    detailed_source.results = [listings("a", rooms=6), listings("a", rooms=6)]

    await runner.run("fake")
    await runner.run("fake")

    assert detailed_source.pages_read == []


async def test_unreadable_listing_pages_do_not_stop_the_notification(
    runner, detailed_source, notifier, sessionmaker
):
    detailed_source.results = [listings("a"), listings("a", "b", "c")]
    detailed_source.pages["a"] = ListingDetails()
    detailed_source.pages["b"] = RuntimeError("page layout changed")
    detailed_source.pages["c"] = BlockedError("captcha")

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.status == RunStatus.OK
    assert sorted(source_id for _, source_id, _ in notifier.new) == ["b", "c"]
    async with sessionmaker() as session:
        listing = await session.scalar(select(Listing).where(Listing.source_id == "b"))
        assert listing.details_checked_at is None  # tried again later


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


def placed(*ids: str) -> FetchResult:
    """Listings with a point on the map, 110 meters from the stop of line 17 below."""
    result = listings(*ids)
    for listing in result.listings:
        listing.latitude, listing.longitude = HOME
    return result


@pytest.fixture
async def wants_a_bus(sessionmaker, runner, admin):
    runner.settings.routing_url = ROUTING_URL
    line = bus_line("17", (HOME[0] + 0.001, HOME[1]))
    await change_user(
        sessionmaker, admin, criteria=SearchCriteria(bus_lines=[line], bus_max_minutes=5)
    )
    return line


async def test_the_walk_to_the_bus_is_worked_out_before_notifying(
    runner, source, notifier, sessionmaker, admin, wants_a_bus, monkeypatch
):
    async def a_long_way_round(client, routing_url, points, stops):
        return [{stop.code: 900 for stop in stops} for _ in points]

    monkeypatch.setattr(walking, "walk_table", a_long_way_round)
    source.results = [placed("a"), placed("a", "b")]

    await runner.run("fake")
    report = await runner.run("fake")

    # Near as the crow flies, but a quarter of an hour on the streets
    assert report.status == RunStatus.OK
    assert notifier.new == []
    async with sessionmaker() as session:
        for source_id in ("a", "b"):
            listing = await session.scalar(select(Listing).where(Listing.source_id == source_id))
            assert listing.walks["stops"] == {"171": 900}
            assert (listing.marks[admin].matches, listing.marks[admin].status) == (
                False,
                ListingStatus.SEEN,
            )
        run = await session.scalar(select(SourceRun).order_by(SourceRun.id.desc()))
        assert run.new == 0


async def test_a_new_listing_near_the_bus_is_sent_with_its_walk(
    runner, source, notifier, admin, wants_a_bus, monkeypatch
):
    async def around_the_corner(client, routing_url, points, stops):
        return [{stop.code: 150 for stop in stops} for _ in points]

    monkeypatch.setattr(walking, "walk_table", around_the_corner)
    source.results = [placed("a"), placed("a", "b")]

    await runner.run("fake")
    await runner.run("fake")

    assert notifier.new == [(admin, "b", False)]
    assert notifier.new_listings[0].walks["stops"] == {"171": 150}


async def test_a_routing_server_that_is_down_does_not_stop_the_notifications(
    runner, source, notifier, admin, wants_a_bus
):
    # conftest keeps the tests offline, so no walk can be worked out
    source.results = [placed("a"), placed("a", "b")]

    await runner.run("fake")
    report = await runner.run("fake")

    assert report.status == RunStatus.OK
    assert notifier.new == [(admin, "b", False)]
