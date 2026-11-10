"""Stores the listings a source returned and works out which users should hear about them."""

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder import areas
from roomfinder.dedupe import find_duplicate
from roomfinder.matching import listing_matches
from roomfinder.models import Listing, ListingMark, ListingStatus, PriceChange
from roomfinder.normalize import address_key, furniture_from_text, parking_from_text
from roomfinder.schemas import ListingData, ListingDetails
from roomfinder.searchers import FetchScope, Searcher, in_earlier_reach, load_searchers

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
    # New on the market; marked "new" for each user whose search they fit
    new: list[Listing] = field(default_factory=list)
    price_drops: list[PriceDrop] = field(default_factory=list)
    # Listings the source returned only because it was read differently (e.g. a user widened
    # their search), stored quietly: how many of them fit each user's search, by user id
    already_listed: Counter[int] = field(default_factory=Counter)
    removed: int = 0


def mark(listing: Listing, user_id: int) -> ListingMark:
    """The user's mark on the listing, made when there's none yet."""
    found = listing.marks.get(user_id)
    if found is None:
        found = listing.marks[user_id] = ListingMark(
            user_id=user_id, matches=False, status=ListingStatus.SEEN
        )
    return found


def judge(
    listing: Listing, searchers: Sequence[Searcher], only: Collection[int] | None = None
) -> None:
    """Note on each user's mark whether the listing fits their search.

    An admin who follows everyone's searches gets what fits any active user's own search,
    and one who turned theirs off gets nothing (Searcher.scope). only: these users' marks
    alone (all the searchers are still needed, for the admins who follow everyone).
    """
    anyone: bool | None = None
    for searcher in searchers:
        if only is not None and searcher.user_id not in only:
            continue
        if searcher.scope == "own":
            fits = listing_matches(listing, searcher.criteria)
        elif searcher.scope == "everyone":
            if anyone is None:
                anyone = any(
                    listing_matches(listing, other.criteria)
                    for other in searchers
                    if other.searching
                )
            fits = anyone
        else:
            fits = False
        mark(listing, searcher.user_id).matches = fits


def interested(listing: Listing) -> list[int]:
    """The users whose search the listing fits, unless they hid it."""
    return [
        user_id
        for user_id, found in listing.marks.items()
        if found.matches and found.status != ListingStatus.HIDDEN
    ]


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
        "furnished",
        "furniture_details",
        "parking",
        "parking_spaces",
        "posted_at",
        "raw",
    ):
        value = getattr(item, name)
        if value is not None:
            setattr(listing, name, value)
    if item.images:
        listing.images = list(item.images)
    if item.features:
        listing.features = list(item.features)
    listing.address_key = address_key(listing.street, listing.house_number)
    listing.area = areas.locate(listing)
    listing.last_seen_at = now
    listing.removed_at = None


async def _insert(
    session: AsyncSession,
    item: ListingData,
    searchers: Sequence[Searcher],
    previous: FetchScope | None,
    current: FetchScope,
    now: datetime,
    outcome: IngestOutcome,
) -> None:
    listing = Listing(
        source=item.source, source_id=item.source_id, first_seen_at=now, images=[], features=[]
    )
    _apply(listing, item, now)
    judge(listing, searchers)
    session.add(listing)
    await session.flush()
    outcome.inserted += 1

    duplicate = await find_duplicate(session, listing, now)
    hidden_by: set[int] = set()
    if duplicate is not None:
        listing.duplicate_of_id = duplicate.id
        hidden_by = {
            user_id
            for user_id, found in duplicate.marks.items()
            if found.status == ListingStatus.HIDDEN
        }

    # A duplicate of a listing that is still up is old news; one of a taken-down listing is
    # the apartment coming back to the market
    is_news = duplicate is None or duplicate.removed_at is not None
    if is_news and not in_earlier_reach(previous, current, listing):
        if duplicate is None:
            outcome.already_listed.update(
                user_id for user_id, found in listing.marks.items() if found.matches
            )
        is_news = False
    for user_id, found in listing.marks.items():
        if user_id in hidden_by:
            found.status = ListingStatus.HIDDEN
        elif found.matches and is_news:
            found.status = ListingStatus.NEW
    if any(found.status == ListingStatus.NEW for found in listing.marks.values()):
        outcome.new.append(listing)


def _update(
    listing: Listing,
    item: ListingData,
    searchers: Sequence[Searcher],
    previous: FetchScope | None,
    current: FetchScope,
    now: datetime,
    session: AsyncSession,
    outcome: IngestOutcome,
) -> None:
    old_price = listing.price
    _apply(listing, item, now)
    judge(listing, searchers)

    new_price = listing.price
    if old_price is None or new_price is None or new_price == old_price:
        return
    session.add(PriceChange(listing=listing, old_price=old_price, new_price=new_price, seen_at=now))
    # Seen only through a new reading of the source, the drop may be long past
    if (
        new_price < old_price
        and listing.duplicate_of_id is None
        and in_earlier_reach(previous, current, listing)
    ):
        outcome.price_drops.append(PriceDrop(listing, old_price, new_price))


async def ingest(
    session: AsyncSession,
    source: str,
    items: list[ListingData],
    searchers: Sequence[Searcher],
    *,
    previous: FetchScope | None,
    current: FetchScope,
    now: datetime,
) -> IngestOutcome:
    """Insert or update the listings a source returned in one run, and judge them for everyone.

    previous is what the source was read with before (None the first time), and current what
    it was read with now. Listings that only the current reading reaches are recorded quietly,
    so nobody is flooded with listings that were already on the market.
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
            await _insert(session, item, searchers, previous, current, now, outcome)
        else:
            _update(listing, item, searchers, previous, current, now, session, outcome)
    await session.flush()
    return outcome


def apply_details(
    listing: Listing, details: ListingDetails | None, searchers: Sequence[Searcher], now: datetime
) -> None:
    """Add what the listing's own page says, then judge the listing again."""
    listing.details_checked_at = now
    if details is not None:
        for name in (
            "description",
            "furnished",
            "furniture_details",
            "parking",
            "parking_spaces",
            "sqm",
            "entry_date",
            "posted_at",
        ):
            value = getattr(details, name)
            if value is not None:
                setattr(listing, name, value)
        if details.features:
            listing.features = list(dict.fromkeys([*listing.features, *details.features]))
    # Sites without a structured field often say it in the ad's text
    if listing.furnished is None:
        listing.furnished = furniture_from_text(
            " ".join(filter(None, [listing.furniture_details, listing.description]))
        )
    if listing.parking is None:
        listing.parking = parking_from_text(listing.description)
    judge(listing, searchers)


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


async def rematch(session: AsyncSession, searchers: Sequence[Searcher], user_ids: set[int]) -> None:
    """Judge every stored listing again for these users, after their search changed (or the
    user is new, or was turned on or off). The admins who follow everyone's searches are
    judged again too, since what they see is made of the others' searches."""
    targets = user_ids | {
        searcher.user_id for searcher in searchers if searcher.scope == "everyone"
    }
    for listing in await session.scalars(select(Listing)):
        judge(listing, searchers, targets)


async def rematch_user(session: AsyncSession, user_id: int) -> None:
    """rematch for one user, with everyone's settings as saved in this session."""
    await rematch(session, await load_searchers(session), {user_id})


async def refresh_listings(session: AsyncSession, searchers: Sequence[Searcher]) -> int:
    """Place stored listings in neighborhoods again (e.g. after the neighborhood map changed),
    and judge any listing that a user has no mark on yet.

    Returns how many listings moved.
    """
    moved = 0
    users = {searcher.user_id for searcher in searchers}
    for listing in await session.scalars(select(Listing)):
        area = areas.locate(listing)
        if area != listing.area:
            listing.area = area
            moved += 1
            judge(listing, searchers)
        elif not users.issubset(listing.marks):
            judge(listing, searchers)
    return moved
