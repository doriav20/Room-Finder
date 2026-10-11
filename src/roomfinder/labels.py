"""Hebrew labels and links for listing details, shared by the Telegram messages and the
dashboard."""

from collections.abc import Iterable
from urllib.parse import urlencode

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


def directions_url(listing: Listing, copies: Iterable[Listing] = ()) -> str | None:
    """Google Maps directions that start at the apartment, with the destination left to fill in.

    A full address reads best there; without one the start is the listing's map point (its own
    or a copy's from another site), and failing that its street or neighborhood."""
    street = " ".join(filter(None, [listing.street, listing.house_number]))
    point = next(
        (
            f"{candidate.latitude},{candidate.longitude}"
            for candidate in (listing, *copies)
            if candidate.latitude is not None and candidate.longitude is not None
        ),
        None,
    )
    if listing.street and listing.house_number:
        origin = ", ".join(filter(None, [street, listing.city]))
    elif point:
        origin = point
    elif place := street or listing.neighborhood or listing.area:
        origin = ", ".join(filter(None, [place, listing.city]))
    else:
        return None
    return "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "origin": origin})
