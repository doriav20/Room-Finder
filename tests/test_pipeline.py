from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select

from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, PriceChange
from roomfinder.pipeline import (
    REMOVAL_GRACE,
    apply_details,
    ingest,
    interested,
    judge,
    mark,
    mark_removed,
    refresh_listings,
    rematch,
)
from roomfinder.schemas import ListingData, ListingDetails, PropertyType, SearchCriteria
from roomfinder.searchers import FetchScope, Searcher, combined_searches
from roomfinder.settings_store import Preferences

CRITERIA = SearchCriteria()
ME = 1


def searchers(*criteria: SearchCriteria) -> list[Searcher]:
    """A user per search, numbered from 1."""
    return [
        Searcher(user_id, True, False, search, Preferences())
        for user_id, search in enumerate(criteria or (CRITERIA,), 1)
    ]


def scope(*criteria: SearchCriteria) -> FetchScope:
    return FetchScope.of(combined_searches(criteria or (CRITERIA,)), None)


async def store(
    session,
    items: list[ListingData],
    *criteria: SearchCriteria,
    first: bool = False,
    previous: FetchScope | None = None,
    source: str = "yad2",
    now=None,
):
    """One run's listings, read with these users' searches combined.

    first: the source's first reading; previous: what it was read with before, if different.
    """
    current = scope(*criteria)
    return await ingest(
        session,
        source,
        items,
        searchers(*criteria),
        previous=None if first else previous or current,
        current=current,
        now=now or utcnow(),
    )


def item(source_id: str = "a1", source: str = "yad2", **overrides) -> ListingData:
    fields = {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.com/{source_id}",
        "city": "ראשון לציון",
        "rooms": 4,
        "price": 6000,
        "sqm": 95,
        "property_type": PropertyType.APARTMENT,
        "street": "הרצל",
        "house_number": "12",
        "neighborhood": "רמז",
    } | overrides
    return ListingData(**fields)


async def get_listing(session, source_id: str) -> Listing:
    return await session.scalar(select(Listing).where(Listing.source_id == source_id))


async def test_first_run_stores_everything_silently(session):
    items = [item("a1"), item("a2", street="ביאליק")]

    outcome = await store(session, items, first=True)

    assert outcome.inserted == 2
    assert outcome.new == []
    assert outcome.already_listed == {ME: 2}
    statuses = {listing.marks[ME].status for listing in await session.scalars(select(Listing))}
    assert statuses == {ListingStatus.SEEN}


async def test_new_matching_listing_is_notified_once(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    items = [item("a1"), item("a2", street="ביאליק")]

    first = await store(session, items, now=now)
    second = await store(session, items, now=now)

    assert [listing.source_id for listing in first.new] == ["a2"]
    assert first.new[0].marks[ME].status == ListingStatus.NEW
    assert first.already_listed == {}
    assert second.new == []


async def test_non_matching_listing_is_stored_without_notification(session):
    outcome = await store(session, [item("a1", rooms=3)])

    listing = await get_listing(session, "a1")
    assert outcome.new == []
    assert listing.marks[ME].matches is False
    assert listing.marks[ME].status == ListingStatus.SEEN


async def test_each_user_is_judged_by_their_own_search(session):
    four, five = (
        SearchCriteria(rooms_min=4, rooms_max=5),
        SearchCriteria(rooms_min=5, rooms_max=5.5),
    )
    await store(session, [], four, five, first=True)
    items = [item("a1", rooms=4), item("a2", rooms=5, street="ביאליק"), item("a3", rooms=5.5)]

    outcome = await store(session, items, four, five)

    marks = {listing.source_id: listing.marks for listing in outcome.new}
    assert set(marks) == {"a1", "a2", "a3"}
    assert (marks["a1"][1].status, marks["a1"][2].status) == ("new", "seen")
    assert (marks["a2"][1].status, marks["a2"][2].status) == ("new", "new")
    assert (marks["a3"][1].status, marks["a3"][2].status) == ("seen", "new")
    assert marks["a3"][1].matches is False


async def test_a_wider_search_stores_what_only_it_reaches_quietly(session):
    four, five = SearchCriteria(rooms_min=4, rooms_max=4), SearchCriteria(rooms_min=5, rooms_max=5)
    now = utcnow()
    await store(session, [item("a1")], four, first=True, now=now)
    items = [item("a1"), item("a2", street="ביאליק"), item("b1", rooms=5, street="הגפן")]

    # The second user joined: the source is read for 4-5 rooms now
    outcome = await store(session, items, four, five, previous=scope(four), now=now)

    # A 4-room listing would have come up before too, so it's new; the 5-room one was just
    # out of reach until now
    assert [listing.source_id for listing in outcome.new] == ["a2"]
    assert outcome.already_listed == {2: 1}
    b1 = await get_listing(session, "b1")
    assert (b1.marks[2].matches, b1.marks[2].status) == (True, ListingStatus.SEEN)


async def test_price_drop_is_recorded_and_reported(session):
    now = utcnow()
    await store(session, [item("a1", price=6500)], first=True, now=now)
    later = now + timedelta(hours=1)

    outcome = await store(session, [item("a1", price=6100)], now=later)

    assert [(drop.old_price, drop.new_price) for drop in outcome.price_drops] == [(6500, 6100)]
    assert interested(outcome.price_drops[0].listing) == [ME]
    changes = (await session.scalars(select(PriceChange))).all()
    assert [(change.old_price, change.new_price) for change in changes] == [(6500, 6100)]


async def test_price_increase_is_recorded_but_not_reported(session):
    now = utcnow()
    await store(session, [item("a1", price=6000)], first=True, now=now)

    outcome = await store(session, [item("a1", price=6300)], now=now)

    assert outcome.price_drops == []
    assert len((await session.scalars(select(PriceChange))).all()) == 1


async def test_price_drop_is_not_for_users_who_hid_the_listing(session):
    now = utcnow()
    await store(session, [item("a1", price=6500)], CRITERIA, CRITERIA, first=True, now=now)
    (await get_listing(session, "a1")).marks[1].status = ListingStatus.HIDDEN

    outcome = await store(session, [item("a1", price=6000)], CRITERIA, CRITERIA, now=now)

    assert interested(outcome.price_drops[0].listing) == [2]


async def test_price_drop_into_the_budget_is_reported(session):
    criteria = SearchCriteria(price_max=6000)
    now = utcnow()
    await store(session, [item("a1", price=6500)], criteria, now=now)
    assert (await get_listing(session, "a1")).marks[ME].matches is False

    outcome = await store(session, [item("a1", price=5900)], criteria, now=now)

    assert [drop.new_price for drop in outcome.price_drops] == [5900]
    assert (await get_listing(session, "a1")).marks[ME].matches is True


async def test_price_drop_seen_only_through_a_wider_search_is_not_reported(session):
    cheap, any_price = SearchCriteria(price_max=6000), SearchCriteria()
    now = utcnow()
    await store(session, [item("a1", price=6500)], any_price, first=True, now=now)
    # Read for a while only up to 6,000 ₪, and then for any price again
    outcome = await store(
        session, [item("a1", price=6200)], any_price, previous=scope(cheap), now=now
    )

    assert outcome.price_drops == []


async def test_same_apartment_on_another_site_is_not_notified_again(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    copy = item("m1", source="madlan", street="רח' הרצל", price=6100)

    outcome = await store(session, [copy], source="madlan", now=now)

    assert outcome.new == []
    original = await get_listing(session, "a1")
    assert (await get_listing(session, "m1")).duplicate_of_id == original.id


async def test_duplicate_of_a_hidden_listing_is_hidden_for_whoever_hid_it(session):
    now = utcnow()
    await store(session, [item("a1")], CRITERIA, CRITERIA, first=True, now=now)
    (await get_listing(session, "a1")).marks[1].status = ListingStatus.HIDDEN

    await store(session, [item("m1", source="madlan")], CRITERIA, CRITERIA, source="madlan")

    marks = (await get_listing(session, "m1")).marks
    assert (marks[1].status, marks[2].status) == (ListingStatus.HIDDEN, ListingStatus.SEEN)


async def test_apartment_posted_again_after_removal_is_notified(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    later = now + REMOVAL_GRACE + timedelta(hours=1)
    assert await mark_removed(session, "yad2", later) == 1

    outcome = await store(session, [item("a9")], now=later)

    assert [listing.source_id for listing in outcome.new] == ["a9"]


async def test_listings_missing_for_the_grace_period_are_marked_removed(session):
    now = utcnow()
    both = [item("a1"), item("a2", street="ביאליק")]
    await store(session, both, first=True, now=now)
    soon = now + timedelta(hours=1)
    await store(session, [item("a1")], now=soon)
    assert await mark_removed(session, "yad2", soon) == 0

    later = now + REMOVAL_GRACE + timedelta(hours=1)
    await store(session, [item("a1")], now=later)

    assert await mark_removed(session, "yad2", later) == 1
    assert (await get_listing(session, "a2")).removed_at == later
    assert (await get_listing(session, "a1")).removed_at is None


async def test_listing_that_comes_back_is_active_again(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    later = now + REMOVAL_GRACE + timedelta(hours=1)
    await mark_removed(session, "yad2", later)

    await store(session, [item("a1")], now=later)

    assert (await get_listing(session, "a1")).removed_at is None


async def test_missing_values_keep_what_was_known(session):
    now = utcnow()
    first = item("a1", sqm=95, images=["https://img/1.jpg"])
    await store(session, [first], first=True, now=now)

    await store(session, [item("a1", sqm=None)], now=now)

    listing = await get_listing(session, "a1")
    assert listing.sqm == 95
    assert listing.images == ["https://img/1.jpg"]
    assert listing.address_key == "הרצל|12"


async def test_details_fill_in_furniture_parking_and_text(session):
    now = utcnow()
    await store(session, [item("a1", features=["חניה"])], first=True, now=now)
    listing = await get_listing(session, "a1")

    details = ListingDetails(
        description="דירה מרווחת",
        furnished=True,
        furniture_details="ריהוט מלא",
        parking=True,
        parking_spaces=2,
        features=["חניה", "מעלית"],
    )
    apply_details(listing, details, searchers(), now)

    assert (listing.furnished, listing.furniture_details) == (True, "ריהוט מלא")
    assert (listing.parking, listing.parking_spaces) == (True, 2)
    assert listing.description == "דירה מרווחת"
    assert listing.features == ["חניה", "מעלית"]
    assert listing.details_checked_at == now


async def test_details_fall_back_to_the_ad_text(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    listing = await get_listing(session, "a1")

    apply_details(listing, ListingDetails(description="ללא ריהוט, חניה בטאבו"), searchers(), now)

    assert (listing.furnished, listing.parking) == (False, True)


async def test_details_can_rule_a_listing_out(session):
    criteria = SearchCriteria(furnished="yes")
    now = utcnow()
    await store(session, [item("a1")], criteria, first=True, now=now)
    listing = await get_listing(session, "a1")
    assert listing.marks[ME].matches is True

    apply_details(listing, ListingDetails(furnished=False), searchers(criteria), now)

    assert listing.marks[ME].matches is False


async def test_missing_page_still_counts_as_checked(session):
    now = utcnow()
    await store(session, [item("a1")], first=True, now=now)
    listing = await get_listing(session, "a1")

    apply_details(listing, None, searchers(), now)

    assert listing.details_checked_at == now
    assert listing.furnished is None


async def test_repeated_items_in_one_run_are_ingested_once(session):
    outcome = await store(session, [item("a1"), item("a1")])

    assert outcome.inserted == 1
    assert len(outcome.new) == 1


async def test_listings_are_placed_in_their_neighborhood(session):
    await store(session, [item("a1", neighborhood="רמבם")], first=True)
    listing = await get_listing(session, "a1")
    assert listing.area == 'רמב"ם'

    await store(session, [item("a1", neighborhood="נוה ים")])
    assert listing.area == "נווה ים"


async def test_listings_outside_the_chosen_neighborhoods_are_not_notified(session):
    criteria = SearchCriteria(areas=["רמז"])
    items = [item("a1"), item("a2", neighborhood="נווה ים", street="ביאליק")]

    outcome = await store(session, items, criteria)

    assert [listing.source_id for listing in outcome.new] == ["a1"]
    assert (await get_listing(session, "a2")).marks[ME].matches is False


async def test_a_changed_search_judges_every_stored_listing_again(session):
    await store(session, [item("a1", rooms=4), item("a2", rooms=5, street="ביאליק")], first=True)

    await rematch(session, searchers(SearchCriteria(rooms_min=5, rooms_max=5)), {ME})

    assert (await get_listing(session, "a1")).marks[ME].matches is False
    assert (await get_listing(session, "a2")).marks[ME].matches is True


def admin(user_id: int, scope: str, criteria: SearchCriteria = CRITERIA) -> Searcher:
    return Searcher(user_id, True, True, criteria, Preferences(search_scope=scope))


def test_an_admin_can_follow_everyone_elses_searches():
    four, five = searchers(SearchCriteria(), SearchCriteria(rooms_min=5, rooms_max=5))
    away = replace(five, user_id=3, active=False, criteria=SearchCriteria(rooms_min=3))
    watcher = admin(9, "everyone", SearchCriteria(rooms_min=6, rooms_max=6))
    fitting = {}
    for rooms in (3.0, 4.0, 5.0, 6.0):
        listing = Listing(source="yad2", source_id=str(rooms), rooms=rooms, images=[])
        judge(listing, [four, five, away, watcher])
        fitting[rooms] = listing.marks[9].matches

    # Inactive users' searches and the admin's own are left out
    assert fitting == {3.0: False, 4.0: True, 5.0: True, 6.0: False}


def test_an_admin_can_turn_their_search_off():
    listing = Listing(source="yad2", source_id="a1", rooms=4.0, images=[])

    judge(listing, [admin(1, "none"), *searchers(CRITERIA, CRITERIA)[1:]])

    assert listing.marks[1].matches is False
    assert listing.marks[2].matches is True


def test_members_always_search_with_their_own_search():
    member = Searcher(1, True, False, CRITERIA, Preferences(search_scope="none"))
    listing = Listing(source="yad2", source_id="a1", rooms=4.0, images=[])

    judge(listing, [member])

    assert listing.marks[1].matches is True


async def test_another_users_new_search_is_judged_again_for_admins_who_follow_everyone(session):
    await store(session, [item("a1", rooms=5)], first=True)
    widened = replace(searchers(SearchCriteria(rooms_min=4, rooms_max=5))[0], user_id=2)

    await rematch(session, [admin(ME, "everyone"), widened], {2})

    assert (await get_listing(session, "a1")).marks[ME].matches is True


async def test_refresh_places_stored_listings_and_judges_them_again(session):
    now = utcnow()
    listing = Listing(
        source="komo", source_id="k1", url="https://example.com/k1", city="ראשון לציון",
        rooms=4.0, price=6000, property_type="apartment", neighborhood="נווה ים",
        images=[], features=[], first_seen_at=now, last_seen_at=now,
    )  # fmt: skip
    session.add(listing)
    mark(listing, ME).matches = True
    await session.flush()
    criteria = SearchCriteria(areas=["רמז"])

    assert await refresh_listings(session, searchers(criteria)) == 1
    assert (listing.area, listing.marks[ME].matches) == ("נווה ים", False)
    assert await refresh_listings(session, searchers(criteria)) == 0


async def test_refresh_gives_every_user_a_mark_on_every_listing(session):
    await store(session, [item("a1")], first=True)
    listing = await get_listing(session, "a1")
    assert set(listing.marks) == {ME}

    await refresh_listings(session, searchers(CRITERIA, SearchCriteria(rooms_min=5, rooms_max=5)))

    assert (listing.marks[1].matches, listing.marks[2].matches) == (True, False)
