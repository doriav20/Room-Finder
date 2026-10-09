"""Who is searching for what, and the one search the sites are read with for all of them.

Every user has their own search. The sites aren't searched once per user: per city, all the
searches are joined into one that returns everything any of them would (combined_searches),
each site is read once with it, and every listing is then judged against each user's own
search. A listing that fits several users is fetched and stored once.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder.matching import ListingLike, listing_matches
from roomfinder.models import User
from roomfinder.normalize import normalize_city
from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.settings_store import Preferences, user_criteria, user_preferences

# The sites are read without what each user filters by locally (the rest have neutral defaults)
NO_LOCAL_FILTERS: dict[str, Any] = {"exclude_keywords": []}


@dataclass(frozen=True)
class Searcher:
    user_id: int
    active: bool
    is_admin: bool
    criteria: SearchCriteria
    preferences: Preferences


async def load_searchers(session: AsyncSession) -> list[Searcher]:
    """Every account, disabled ones too (their marks are kept up to date for when they return)."""
    users = await session.scalars(select(User).order_by(User.id))
    return [
        Searcher(
            user_id=user.id,
            active=user.is_active,
            is_admin=user.is_admin,
            criteria=user_criteria(user),
            preferences=user_preferences(user),
        )
        for user in users
    ]


def combined_searches(searches: Iterable[SearchCriteria]) -> list[SearchCriteria]:
    """One search per city, which returns everything that any of these searches would.

    Ranges are joined into one, overlapping or not: 4-5 rooms and 5-5.5 rooms are read as
    4-5.5, and a search without a price limit makes the combined search have none. A wider
    search costs a few more results; a second search costs another page load, and repeated
    page loads are what trip the sites' bot checks. Only what the sites filter by is
    combined: neighborhoods, furniture, parking and the rest stay each user's own.
    """
    by_city: dict[str, list[SearchCriteria]] = {}
    for search in searches:
        by_city.setdefault(normalize_city(search.city) or search.city, []).append(search)
    return [_combine(group) for _, group in sorted(by_city.items())]


def _combine(group: list[SearchCriteria]) -> SearchCriteria:
    lowest_prices = [search.price_min for search in group]
    highest_prices = [search.price_max for search in group]
    # A search without property types takes any type
    any_type = any(not search.property_types for search in group)
    kinds = {kind for search in group for kind in search.property_types}
    return SearchCriteria(
        city=group[0].city,
        rooms_min=min(search.rooms_min for search in group),
        rooms_max=max(search.rooms_max for search in group),
        price_min=None if None in lowest_prices else min(lowest_prices),
        price_max=None if None in highest_prices else max(highest_prices),
        property_types=[] if any_type else [kind for kind in PropertyType if kind in kinds],
        private_only=all(search.private_only for search in group),
        **NO_LOCAL_FILTERS,
    )


class FetchScope(BaseModel):
    """What a source is read with: an admin's custom URL, or the combined searches."""

    custom_url: str | None = None
    searches: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def of(cls, searches: Sequence[SearchCriteria], custom_url: str | None) -> "FetchScope":
        if custom_url:
            return cls(custom_url=custom_url)
        return cls(searches=[search.fetch_fields() for search in searches])

    def reaches(self, listing: ListingLike) -> bool:
        """Whether reading the source this way returns the listing, as far as can be told."""
        if self.custom_url:
            return True
        # Read without the local filters, as the sites are (combined_searches)
        searches = (
            SearchCriteria.model_validate(fields | NO_LOCAL_FILTERS) for fields in self.searches
        )
        return any(listing_matches(listing, search) for search in searches)


def in_earlier_reach(
    previous: FetchScope | None, current: FetchScope, listing: ListingLike
) -> bool:
    """Whether the source's earlier reading would have returned this listing too.

    If it would have, a listing that wasn't there before is new on the market. If only the
    current reading reaches it (the first one, another custom URL, or a search that grew,
    say when a user widened theirs), it was most likely on the market already, and is
    stored quietly instead of notifying about everything that was up all along.
    """
    if previous is None or previous.custom_url != current.custom_url:
        return False
    return previous == current or previous.reaches(listing)
