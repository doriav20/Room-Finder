"""OnMap (onmap.co.il) - a map-based listings site with a JSON search API.

One API call returns every listing in the city for the requested room counts.
"""

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page

from roomfinder.normalize import clean_text, parse_floor, parse_int, parse_price, parse_rooms
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError

SITE = "https://www.onmap.co.il"
SEARCH_API = "https://phoenix.onmap.co.il/v1/properties/mixed_search"
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


class OnMapSource(Source):
    name = "onmap"
    label = "OnMap"
    default_interval_minutes = 30
    night_interval_minutes = 60

    def search_url(self, criteria: SearchCriteria) -> str:
        path = f"{SITE}/search/homes/rent/{_city_slug(criteria)}"
        return (
            f"{path}/rooms_{criteria.rooms_min:g}"
            if criteria.rooms_min == criteria.rooms_max
            else path
        )

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        response = await page.goto(custom_url or api_url(criteria), wait_until="domcontentloaded")
        if response is None:
            raise SourceError("OnMap gave no response")
        if response.status in (401, 403, 429):
            raise BlockedError(f"OnMap answered {response.status}")
        if response.status != 200:
            raise SourceError(f"OnMap answered {response.status}")
        try:
            body = json.loads(await response.text())
        except json.JSONDecodeError:
            raise BlockedError("OnMap returned a page instead of JSON") from None
        return FetchResult(parse_response(body), complete=True)
