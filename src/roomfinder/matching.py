"""Decides whether a listing fits the user's search criteria."""

from typing import Protocol

from roomfinder.normalize import contains_phrase, normalize_city, normalize_neighborhood
from roomfinder.schemas import SearchCriteria


class ListingLike(Protocol):
    """Shared by ListingData (freshly parsed) and the Listing model (stored)."""

    title: str | None
    description: str | None
    price: int | None
    rooms: float | None
    sqm: int | None
    property_type: str | None
    city: str | None
    neighborhood: str | None
    is_agency: bool | None
    furnished: bool | None
    parking: bool | None


def mismatch_reasons(listing: ListingLike, criteria: SearchCriteria) -> list[str]:
    """Why the listing doesn't fit. Unknown values never disqualify a listing."""
    reasons: list[str] = []

    if listing.city and normalize_city(listing.city) != normalize_city(criteria.city):
        reasons.append("city")

    if listing.rooms is not None and not criteria.rooms_min <= listing.rooms <= criteria.rooms_max:
        reasons.append("rooms")

    if listing.price is not None and (
        (criteria.price_min and listing.price < criteria.price_min)
        or (criteria.price_max and listing.price > criteria.price_max)
    ):
        reasons.append("price")

    if criteria.sqm_min and listing.sqm is not None and listing.sqm < criteria.sqm_min:
        reasons.append("sqm")

    if (
        listing.property_type
        and criteria.property_types
        and listing.property_type not in criteria.property_types
    ):
        reasons.append("property_type")

    if criteria.private_only and listing.is_agency:
        reasons.append("agency")

    if (criteria.furnished == "yes" and listing.furnished is False) or (
        criteria.furnished == "no" and listing.furnished is True
    ):
        reasons.append("furnished")

    if criteria.parking_required and listing.parking is False:
        reasons.append("parking")

    neighborhood = normalize_neighborhood(listing.neighborhood)
    if neighborhood:
        included = {normalize_neighborhood(n) for n in criteria.neighborhoods_include}
        excluded = {normalize_neighborhood(n) for n in criteria.neighborhoods_exclude}
        if (included and neighborhood not in included) or neighborhood in excluded:
            reasons.append("neighborhood")

    text = " ".join(filter(None, [listing.title, listing.description]))
    if any(contains_phrase(text, keyword) for keyword in criteria.exclude_keywords):
        reasons.append("keyword")

    return reasons


def listing_matches(listing: ListingLike, criteria: SearchCriteria) -> bool:
    return not mismatch_reasons(listing, criteria)
