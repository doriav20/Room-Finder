"""Spots the same apartment when it is posted on several sites or re-posted."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.models import Listing
from roomfinder.normalize import normalize_neighborhood

PRICE_TOLERANCE = 0.03
SQM_TOLERANCE = 3
LOOKBACK = timedelta(days=60)


def _prices_close(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= PRICE_TOLERANCE * max(a, b)


def same_apartment(a: Listing, b: Listing) -> bool:
    """Conservative: when in doubt, report a different apartment rather than hide a new one."""
    if a.rooms is None or a.rooms != b.rooms or not _prices_close(a.price, b.price):
        return False
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return False
    if a.address_key and b.address_key:
        return a.address_key == b.address_key
    # At least one side has no exact address - fall back to neighborhood and size
    neighborhood = normalize_neighborhood(a.neighborhood)
    return (
        neighborhood is not None
        and neighborhood == normalize_neighborhood(b.neighborhood)
        and a.sqm is not None
        and b.sqm is not None
        and abs(a.sqm - b.sqm) <= SQM_TOLERANCE
    )


async def find_duplicate(session: AsyncSession, listing: Listing, now: datetime) -> Listing | None:
    """The original listing that this one duplicates, if any."""
    if listing.rooms is None or listing.price is None:
        return None
    candidates = await session.scalars(
        select(Listing)
        .where(
            Listing.id != listing.id,
            Listing.rooms == listing.rooms,
            Listing.price.between(
                listing.price * (1 - PRICE_TOLERANCE), listing.price * (1 + PRICE_TOLERANCE)
            ),
            Listing.last_seen_at >= now - LOOKBACK,
        )
        .order_by(Listing.first_seen_at)
    )
    for candidate in candidates:
        if same_apartment(listing, candidate):
            if candidate.duplicate_of_id is not None:
                return await session.get(Listing, candidate.duplicate_of_id) or candidate
            return candidate
    return None
