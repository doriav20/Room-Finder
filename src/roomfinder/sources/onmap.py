"""OnMap (onmap.co.il) - a map-based listings site with a JSON search API.

One API call returns every listing in the city for the requested room counts, and another
one a listing's own details, such as the ad's text.
"""

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from playwright.async_api import Page

from roomfinder.normalize import (
    clean_multiline,
    clean_text,
    parse_floor,
    parse_int,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, ListingDetails, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError

SITE = "https://www.onmap.co.il"
SEARCH_API = "https://phoenix.onmap.co.il/v1/properties/mixed_search"
DETAILS_API = "https://phoenix.onmap.co.il/v1/properties/{id}"
DETAILS_URL = SITE + "/home-details/{slug}"

CITY_SLUGS = {"ראשון לציון": "rishon-letsiyon"}

PROPERTY_TYPES = {
    "apartment": PropertyType.APARTMENT,
    "penthouse": PropertyType.PENTHOUSE,
    "minipenthouse": PropertyType.PENTHOUSE,
    "rooftopapartment": PropertyType.PENTHOUSE,
    "duplex": PropertyType.DUPLEX,
    "triplex": PropertyType.DUPLEX,
    "gardenapartment": PropertyType.GARDEN,
    "housingunit": PropertyType.UNIT,
    "studio": PropertyType.STUDIO,
    "loft": PropertyType.STUDIO,
    "cottage": PropertyType.HOUSE,
    "house": PropertyType.HOUSE,
    "villa": PropertyType.HOUSE,
    "semidetached": PropertyType.HOUSE,
}

# The site's property types, as its search page URL names them
TYPE_SLUGS: dict[PropertyType, tuple[str, ...]] = {
    PropertyType.APARTMENT: ("apartment",),
    PropertyType.GARDEN: ("garden_apartment",),
    PropertyType.DUPLEX: ("duplex", "triplex"),
    PropertyType.PENTHOUSE: ("penthouse", "mini_penthouse", "rooftop_apartment"),
    PropertyType.HOUSE: ("cottage", "villa"),
    PropertyType.UNIT: ("housing_unit",),
    PropertyType.STUDIO: ("studio", "loft"),
    PropertyType.OTHER: ("room",),
}
# The search page's highest room choice, which stands for "6 or more"
MAX_ROOMS_CHOICE = 6

# Amenities in additional_info that are worth showing as tags (parking has its own field)
FEATURES = {"elevator": "מעלית", "balcony": "מרפסת", "storage": "מחסן"}


def _city_slug(criteria: SearchCriteria) -> str:
    try:
        return CITY_SLUGS[criteria.city]
    except KeyError:
        raise SourceError(f"No OnMap city name for {criteria.city}") from None


def _room_values(criteria: SearchCriteria) -> list[str]:
    values, rooms = [], criteria.rooms_min
    while rooms <= criteria.rooms_max:
        values.append(f"{rooms:g}")
        rooms += 0.5
    return values


def search_url(criteria: SearchCriteria) -> str:
    """The site's search page, e.g. .../rishon-letsiyon/price_0-8000/rooms_4/types_apartment"""
    url = f"{SITE}/search/homes/rent/{_city_slug(criteria)}"
    if criteria.price_min or criteria.price_max:
        url += f"/price_{criteria.price_min or 0}"
        if criteria.price_max:
            url += f"-{criteria.price_max}"
    # The page offers whole room counts, and 4 includes 3.5 and 4.5
    low = max(1, min(int(criteria.rooms_min), MAX_ROOMS_CHOICE))
    high = max(low, min(int(criteria.rooms_max), MAX_ROOMS_CHOICE))
    url += "/rooms_" + ",".join(str(rooms) for rooms in range(low, high + 1))
    if set(criteria.property_types) != set(PropertyType):
        slugs = [slug for kind in criteria.property_types for slug in TYPE_SLUGS[kind]]
        if slugs:
            url += "/types_" + ",".join(slugs)
    return url


def api_url(criteria: SearchCriteria) -> str:
    params = [
        ("option", "rent"),
        ("section", "residence"),
        ("city", _city_slug(criteria)),
        ("is_mobile", "false"),
    ] + [("rooms[]", value) for value in _room_values(criteria)]
    return f"{SEARCH_API}?{urlencode(params)}"


def _property_type(value: Any) -> PropertyType | None:
    if not value:
        return None
    key = str(value).lower().replace("_", "").replace("-", "")
    return PROPERTY_TYPES.get(key, PropertyType.OTHER)


def _posted_at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _has(value: Any) -> bool:
    if isinstance(value, dict):
        return any(item not in (None, False, "none", "") for item in value.values())
    return value not in (None, False, "none", "", 0)


def parse_item(item: dict[str, Any]) -> ListingData | None:
    listing_id = item.get("id")
    if not listing_id or item.get("search_option") not in (None, "rent"):
        return None
    address = (item.get("address") or {}).get("he") or {}
    location = (item.get("address") or {}).get("location") or {}
    info = item.get("additional_info") or {}
    floor = info.get("floor") or {}
    city = clean_text(address.get("city_name"))
    neighborhood = clean_text(address.get("neighborhood"))
    slug = item.get("slug")
    images = [
        url
        for image in item.get("images") or []
        if isinstance(image, dict) and (url := image.get("gallery") or image.get("full"))
    ]
    street = clean_text(address.get("street_name"))
    number = clean_text(address.get("house_number"))
    return ListingData(
        source="onmap",
        source_id=str(listing_id),
        url=DETAILS_URL.format(slug=slug or listing_id),
        title=" ".join(filter(None, [street, number])) or None,
        price=parse_price(item.get("price")),
        rooms=parse_rooms(info.get("rooms")),
        sqm=parse_int((info.get("area") or {}).get("base")) or None,
        floor=parse_floor(floor.get("on_the")),
        total_floors=parse_int(floor.get("out_of")),
        property_type=_property_type(item.get("property_type")),
        city=city,
        # OnMap repeats the city name when it doesn't know the neighborhood
        neighborhood=neighborhood if neighborhood != city else None,
        street=street,
        house_number=number,
        latitude=location.get("lat"),
        longitude=location.get("lon"),
        images=images,
        features=[label for key, label in FEATURES.items() if _has(info.get(key))],
        # Only a parking that is there counts; an empty field may just be unfilled
        parking=True if _has(info.get("parking")) else None,
        posted_at=_posted_at(item.get("created_at")),
    )


def parse_response(body: dict[str, Any]) -> list[ListingData]:
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise SourceError("OnMap response has no data list")
    return [listing for item in items if isinstance(item, dict) and (listing := parse_item(item))]


def parse_details(body: Any) -> ListingDetails | None:
    """A listing's own details (None when it was taken down)."""
    if not isinstance(body, dict) or body.get("is_deleted") or body.get("is_active") is False:
        return None
    return ListingDetails(description=clean_multiline(body.get("description")))


async def _get_json(page: Page, url: str) -> Any:
    """GET one of the API's addresses; None when there is no such listing."""
    response = await page.goto(url, wait_until="domcontentloaded")
    if response is None:
        raise SourceError("OnMap gave no response")
    if response.status in (401, 403, 429):
        raise BlockedError(f"OnMap answered {response.status}")
    if response.status == 404:
        return None
    if response.status != 200:
        raise SourceError(f"OnMap answered {response.status}")
    try:
        return json.loads(await response.text())
    except json.JSONDecodeError:
        raise BlockedError("OnMap returned a page instead of JSON") from None


class OnMapSource(Source):
    name = "onmap"
    label = "OnMap"
    default_interval_minutes = 30
    night_interval_minutes = 60
    has_details = True

    def search_url(self, criteria: SearchCriteria) -> str:
        return search_url(criteria)

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        body = await _get_json(page, custom_url or api_url(criteria))
        if body is None:
            raise SourceError("OnMap answered 404")
        return FetchResult(parse_response(body), complete=True)

    async def fetch_details(self, page: Page, source_id: str, url: str) -> ListingDetails | None:
        return parse_details(await _get_json(page, DETAILS_API.format(id=quote(source_id))))
