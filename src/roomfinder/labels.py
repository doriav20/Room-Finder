"""Hebrew labels for listing details, shared by the Telegram messages and the dashboard."""

from roomfinder import areas
from roomfinder.models import Listing


def furniture_label(listing: Listing) -> str | None:
    if listing.furnished is None:
        return None
    if not listing.furnished:
        return "לא מרוהטת"
    return "מרוהטת חלקית" if "חלקי" in (listing.furniture_details or "") else "מרוהטת"


def parking_label(listing: Listing) -> str | None:
    if listing.parking is None:
        return None
    if not listing.parking:
        return "אין חניה"
    spaces = listing.parking_spaces or 1
    return f"{spaces} חניות" if spaces > 1 else "חניה"


def area_note(listing: Listing) -> str | None:
    """The official neighborhood, when the site's own neighborhood name doesn't already say it
    (a listing placed by its map point, or one the site files under a broader name)."""
    if not listing.area:
        return None
    city_map = areas.city_map(listing.city)
    named = city_map.named(listing.neighborhood) if city_map else None
    return None if named is not None and named.name == listing.area else listing.area
