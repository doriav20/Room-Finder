from datetime import timedelta

from sqlalchemy import select

from roomfinder.db import utcnow
from roomfinder.models import Listing, ListingStatus, PriceChange
from roomfinder.pipeline import (
    REMOVAL_GRACE,
    apply_details,
    ingest,
    mark_removed,
    refresh_areas,
)
from roomfinder.schemas import ListingData, ListingDetails, PropertyType, SearchCriteria

CRITERIA = SearchCriteria()


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


async def test_baseline_run_stores_everything_silently(session):
    items = [item("a1"), item("a2", street="ביאליק")]

    outcome = await ingest(session, "yad2", items, CRITERIA, baseline=True, now=utcnow())

    assert outcome.inserted == 2
    assert outcome.to_notify == []
    assert {listing.status for listing in await session.scalars(select(Listing))} == {
        ListingStatus.SEEN
    }


async def test_new_matching_listing_is_notified_once(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    items = [item("a1"), item("a2", street="ביאליק")]

    first = await ingest(session, "yad2", items, CRITERIA, baseline=False, now=now)
    second = await ingest(session, "yad2", items, CRITERIA, baseline=False, now=now)

    assert [listing.source_id for listing in first.to_notify] == ["a2"]
    assert first.to_notify[0].status == ListingStatus.NEW
    assert second.to_notify == []


async def test_non_matching_listing_is_stored_without_notification(session):
    outcome = await ingest(
        session, "yad2", [item("a1", rooms=3)], CRITERIA, baseline=False, now=utcnow()
    )

    listing = await get_listing(session, "a1")
    assert outcome.to_notify == []
    assert listing.matches is False
    assert listing.status == ListingStatus.SEEN


async def test_price_drop_is_recorded_and_reported(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1", price=6500)], CRITERIA, baseline=True, now=now)
    later = now + timedelta(hours=1)

    outcome = await ingest(
        session, "yad2", [item("a1", price=6100)], CRITERIA, baseline=False, now=later
    )

    assert [(drop.old_price, drop.new_price) for drop in outcome.price_drops] == [(6500, 6100)]
    changes = (await session.scalars(select(PriceChange))).all()
    assert [(change.old_price, change.new_price) for change in changes] == [(6500, 6100)]


async def test_price_increase_is_recorded_but_not_reported(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1", price=6000)], CRITERIA, baseline=True, now=now)

    outcome = await ingest(
        session, "yad2", [item("a1", price=6300)], CRITERIA, baseline=False, now=now
    )

    assert outcome.price_drops == []
    assert len((await session.scalars(select(PriceChange))).all()) == 1


async def test_price_drop_of_hidden_listing_is_not_reported(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1", price=6500)], CRITERIA, baseline=True, now=now)
    (await get_listing(session, "a1")).status = ListingStatus.HIDDEN

    outcome = await ingest(
        session, "yad2", [item("a1", price=6000)], CRITERIA, baseline=False, now=now
    )

    assert outcome.price_drops == []


async def test_price_drop_into_the_budget_is_reported(session):
    criteria = SearchCriteria(price_max=6000)
    now = utcnow()
    await ingest(session, "yad2", [item("a1", price=6500)], criteria, baseline=False, now=now)
    assert (await get_listing(session, "a1")).matches is False

    outcome = await ingest(
        session, "yad2", [item("a1", price=5900)], criteria, baseline=False, now=now
    )

    assert [drop.new_price for drop in outcome.price_drops] == [5900]
    assert (await get_listing(session, "a1")).matches is True


async def test_same_apartment_on_another_site_is_not_notified_again(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    copy = item("m1", source="madlan", street="רח' הרצל", price=6100)

    outcome = await ingest(session, "madlan", [copy], CRITERIA, baseline=False, now=now)

    assert outcome.to_notify == []
    original = await get_listing(session, "a1")
    assert (await get_listing(session, "m1")).duplicate_of_id == original.id


async def test_duplicate_of_a_hidden_listing_is_hidden_too(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    (await get_listing(session, "a1")).status = ListingStatus.HIDDEN

    await ingest(
        session, "madlan", [item("m1", source="madlan")], CRITERIA, baseline=False, now=now
    )

    assert (await get_listing(session, "m1")).status == ListingStatus.HIDDEN


async def test_apartment_posted_again_after_removal_is_notified(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    later = now + REMOVAL_GRACE + timedelta(hours=1)
    assert await mark_removed(session, "yad2", later) == 1

    outcome = await ingest(session, "yad2", [item("a9")], CRITERIA, baseline=False, now=later)

    assert [listing.source_id for listing in outcome.to_notify] == ["a9"]


async def test_listings_missing_for_the_grace_period_are_marked_removed(session):
    now = utcnow()
    both = [item("a1"), item("a2", street="ביאליק")]
    await ingest(session, "yad2", both, CRITERIA, baseline=True, now=now)
    soon = now + timedelta(hours=1)
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=False, now=soon)
    assert await mark_removed(session, "yad2", soon) == 0

    later = now + REMOVAL_GRACE + timedelta(hours=1)
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=False, now=later)

    assert await mark_removed(session, "yad2", later) == 1
    assert (await get_listing(session, "a2")).removed_at == later
    assert (await get_listing(session, "a1")).removed_at is None


async def test_listing_that_comes_back_is_active_again(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    later = now + REMOVAL_GRACE + timedelta(hours=1)
    await mark_removed(session, "yad2", later)

    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=False, now=later)

    assert (await get_listing(session, "a1")).removed_at is None


async def test_missing_values_keep_what_was_known(session):
    now = utcnow()
    first = item("a1", sqm=95, images=["https://img/1.jpg"])
    await ingest(session, "yad2", [first], CRITERIA, baseline=True, now=now)

    await ingest(session, "yad2", [item("a1", sqm=None)], CRITERIA, baseline=False, now=now)

    listing = await get_listing(session, "a1")
    assert listing.sqm == 95
    assert listing.images == ["https://img/1.jpg"]
    assert listing.address_key == "הרצל|12"


async def test_details_fill_in_furniture_parking_and_text(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1", features=["חניה"])], CRITERIA, baseline=True, now=now)
    listing = await get_listing(session, "a1")

    details = ListingDetails(
        description="דירה מרווחת",
        furnished=True,
        furniture_details="ריהוט מלא",
        parking=True,
        parking_spaces=2,
        features=["חניה", "מעלית"],
    )
    apply_details(listing, details, CRITERIA, now)

    assert (listing.furnished, listing.furniture_details) == (True, "ריהוט מלא")
    assert (listing.parking, listing.parking_spaces) == (True, 2)
    assert listing.description == "דירה מרווחת"
    assert listing.features == ["חניה", "מעלית"]
    assert listing.details_checked_at == now


async def test_details_fall_back_to_the_ad_text(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    listing = await get_listing(session, "a1")

    apply_details(listing, ListingDetails(description="ללא ריהוט, חניה בטאבו"), CRITERIA, now)

    assert (listing.furnished, listing.parking) == (False, True)


async def test_details_can_rule_a_listing_out(session):
    criteria = SearchCriteria(furnished="yes")
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], criteria, baseline=True, now=now)
    listing = await get_listing(session, "a1")
    assert listing.matches is True

    apply_details(listing, ListingDetails(furnished=False), criteria, now)

    assert listing.matches is False


async def test_missing_page_still_counts_as_checked(session):
    now = utcnow()
    await ingest(session, "yad2", [item("a1")], CRITERIA, baseline=True, now=now)
    listing = await get_listing(session, "a1")

    apply_details(listing, None, CRITERIA, now)

    assert listing.details_checked_at == now
    assert listing.furnished is None


async def test_repeated_items_in_one_run_are_ingested_once(session):
    outcome = await ingest(
        session, "yad2", [item("a1"), item("a1")], CRITERIA, baseline=False, now=utcnow()
    )

    assert outcome.inserted == 1
    assert len(outcome.to_notify) == 1


async def test_listings_are_placed_in_their_neighborhood(session):
    await ingest(
        session, "yad2", [item("a1", neighborhood="רמבם")], CRITERIA, baseline=True, now=utcnow()
    )
    listing = await get_listing(session, "a1")
    assert listing.area == 'רמב"ם'

    await ingest(
        session, "yad2", [item("a1", neighborhood="נוה ים")], CRITERIA, baseline=False, now=utcnow()
    )
    assert listing.area == "נווה ים"


async def test_listings_outside_the_chosen_neighborhoods_are_not_notified(session):
    criteria = SearchCriteria(areas=["רמז"])
    items = [item("a1"), item("a2", neighborhood="נווה ים", street="ביאליק")]

    outcome = await ingest(session, "yad2", items, criteria, baseline=False, now=utcnow())

    assert [listing.source_id for listing in outcome.to_notify] == ["a1"]
    assert (await get_listing(session, "a2")).matches is False


async def test_refresh_places_stored_listings_and_judges_them_again(session):
    now = utcnow()
    listing = Listing(
        source="komo", source_id="k1", url="https://example.com/k1", city="ראשון לציון",
        rooms=4.0, price=6000, property_type="apartment", neighborhood="נווה ים",
        images=[], features=[], first_seen_at=now, last_seen_at=now, matches=True,
    )  # fmt: skip
    session.add(listing)
    await session.flush()
    criteria = SearchCriteria(areas=["רמז"])

    assert await refresh_areas(session, criteria) == 1
    assert (listing.area, listing.matches) == ("נווה ים", False)
    assert await refresh_areas(session, criteria) == 0
