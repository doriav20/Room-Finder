"""Hebrew labels for listing details, shared by the Telegram messages and the dashboard."""

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
