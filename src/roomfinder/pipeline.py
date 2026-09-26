"""Stores the listings a source returned and works out what the user should hear about."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.dedupe import find_duplicate
from roomfinder.matching import listing_matches
from roomfinder.models import Listing, ListingStatus, PriceChange
from roomfinder.normalize import address_key
from roomfinder.schemas import ListingData, SearchCriteria

# A listing that disappeared from a healthy source for this long is considered taken down
REMOVAL_GRACE = timedelta(hours=48)


@dataclass
class PriceDrop:
    listing: Listing
    old_price: int
    new_price: int


@dataclass
class IngestOutcome:
    found: int = 0
    inserted: int = 0
    to_notify: list[Listing] = field(default_factory=list)
    price_drops: list[PriceDrop] = field(default_factory=list)
    removed: int = 0


def _apply(listing: Listing, item: ListingData, now: datetime) -> None:
    """Copy fresh values from the site, keeping what we knew when a value went missing."""
    listing.url = item.url
    for name in (
        "title",
        "description",
        "price",
        "rooms",
        "sqm",
        "floor",
        "total_floors",
        "property_type",
        "city",
        "neighborhood",
        "street",
        "house_number",
        "latitude",
        "longitude",
        "is_agency",
        "entry_date",
        "posted_at",
        "raw",
    ):
        value = getattr(item, name)
        if value is not None:
            setattr(listing, name, value)
    if item.images:
        listing.images = list(item.images)
    listing.address_key = address_key(listing.street, listing.house_number)
    listing.last_seen_at = now
    listing.removed_at = None


async def _insert(
    session: AsyncSession,
    item: ListingData,
    criteria: SearchCriteria,
    baseline: bool,
    now: datetime,
    outcome: IngestOutcome,
) -> None:
    listing = Listing(source=item.source, source_id=item.source_id, first_seen_at=now, images=[])
    _apply(listing, item, now)
    listing.matches = listing_matches(listing, criteria)
    session.add(listing)
    await session.flush()
    outcome.inserted += 1

    duplicate = await find_duplicate(session, listing, now)
    if duplicate is not None:
        listing.duplicate_of_id = duplicate.id
        if duplicate.status == ListingStatus.HIDDEN:
            listing.status = ListingStatus.HIDDEN
            return

    # A duplicate of a listing that is still up is old news; one of a taken-down listing is
    # the apartment coming back to the market
    is_news = duplicate is None or duplicate.removed_at is not None
    if listing.matches and is_news and not baseline:
        listing.status = ListingStatus.NEW
        outcome.to_notify.append(listing)
    else:
        listing.status = ListingStatus.SEEN


def _update(
    listing: Listing,
    item: ListingData,
    criteria: SearchCriteria,
    baseline: bool,
    now: datetime,
    session: AsyncSession,
    outcome: IngestOutcome,
) -> None:
    old_price = listing.price
    _apply(listing, item, now)
    listing.matches = listing_matches(listing, criteria)

    new_price = listing.price
    if old_price is None or new_price is None or new_price == old_price:
        return
    session.add(PriceChange(listing=listing, old_price=old_price, new_price=new_price, seen_at=now))
    if (
        new_price < old_price
        and not baseline
        and listing.matches
        and listing.status != ListingStatus.HIDDEN
        and listing.duplicate_of_id is None
    ):
        outcome.price_drops.append(PriceDrop(listing, old_price, new_price))


async def ingest(
    session: AsyncSession,
    source: str,
    items: list[ListingData],
    criteria: SearchCriteria,
    *,
    baseline: bool,
    now: datetime,
) -> IngestOutcome:
    """Insert or update the listings a source returned in one run.

    In a baseline run (the first run, or after the search changed) everything is recorded
    silently, so the user isn't flooded with listings that were already on the market.
    """
    outcome = IngestOutcome(found=len(items))
    existing = {
        listing.source_id: listing
        for listing in await session.scalars(select(Listing).where(Listing.source == source))
    }
    handled: set[str] = set()
    for item in items:
        if item.source_id in handled:
            continue
        handled.add(item.source_id)
        listing = existing.get(item.source_id)
        if listing is None:
            await _insert(session, item, criteria, baseline, now, outcome)
        else:
            _update(listing, item, criteria, baseline, now, session, outcome)
    await session.flush()
    return outcome


async def mark_removed(session: AsyncSession, source: str, now: datetime) -> int:
    """Flag listings that stopped appearing in the source's (complete) results."""
    result = await session.execute(
        update(Listing)
        .where(
            Listing.source == source,
            Listing.removed_at.is_(None),
            Listing.last_seen_at < now - REMOVAL_GRACE,
        )
        .values(removed_at=now)
    )
    return result.rowcount or 0
