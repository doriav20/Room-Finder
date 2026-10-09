"""Decides whether a listing fits the user's search criteria."""

from typing import Protocol

from roomfinder import areas
from roomfinder.normalize import contains_phrase, normalize_city
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
    latitude: float | None
    longitude: float | None
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

    # Neighborhoods only mean something for the city whose map they come from
    if criteria.areas and areas.city_map(criteria.city) is not None:
        # A stored listing keeps its neighborhood (pipeline.py), which saves placing it again
        # for every user's search
        area = listing.area if hasattr(listing, "area") else areas.locate(listing)
        if (area is None and not criteria.unknown_area_ok) or (
            area is not None and area not in criteria.areas
        ):
            reasons.append("area")

    text = " ".join(filter(None, [listing.title, listing.description]))
    if any(contains_phrase(text, keyword) for keyword in criteria.exclude_keywords):
        reasons.append("keyword")

    return reasons


def listing_matches(listing: ListingLike, criteria: SearchCriteria) -> bool:
    return not mismatch_reasons(listing, criteria)
