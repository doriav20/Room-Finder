"""Madlan (madlan.co.il) - listings with neighborhood data.

Madlan is guarded by PerimeterX, which blocks browsers that load pages quickly. The results
are therefore read from the data embedded in the server-rendered search page, with long
pauses between pages, and the source runs rarely.
"""

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from playwright.async_api import Page

from roomfinder.normalize import clean_text, parse_floor, parse_int, parse_price, parse_rooms
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError, human_pause

SITE = "https://www.madlan.co.il"
LISTING_URL = SITE + "/listings/{id}"
IMAGE_URL = "https://images2.madlan.co.il/t:nonce:v=2/{path}"
PAGE_SIZE = 50
MAX_PAGES = 6

CITY_SLUGS = {"ראשון לציון": "ראשון-לציון-ישראל"}

BUILDING_CLASSES = {
    "flat": PropertyType.APARTMENT,
    "apartment": PropertyType.APARTMENT,
    "penthouseapp": PropertyType.PENTHOUSE,
    "penthouse": PropertyType.PENTHOUSE,
    "rooftop": PropertyType.PENTHOUSE,
    "roofflat": PropertyType.PENTHOUSE,
    "minipenthouse": PropertyType.PENTHOUSE,
    "gardenapartment": PropertyType.GARDEN,
    "gardenflat": PropertyType.GARDEN,
    "duplex": PropertyType.DUPLEX,
    "triplex": PropertyType.DUPLEX,
    "residentialunit": PropertyType.UNIT,
    "studio": PropertyType.STUDIO,
    "loft": PropertyType.STUDIO,
    "cottage": PropertyType.HOUSE,
    "twofamilydwelling": PropertyType.HOUSE,
    "privatehouse": PropertyType.HOUSE,
    "villa": PropertyType.HOUSE,
    "basement": PropertyType.OTHER,
}

_SSR_CONTEXT = re.compile(r"window\.__SSR_HYDRATED_CONTEXT__\s*=\s*(.*?)</script>", re.S)
# The embedded object is JavaScript, not JSON: it may contain `undefined`
_UNDEFINED = re.compile(r"([:\[,]\s*)undefined(?=\s*[,}\]])")


def _city_slug(criteria: SearchCriteria) -> str:
    try:
        return CITY_SLUGS[criteria.city]
    except KeyError:
        raise SourceError(f"No Madlan location name for {criteria.city}") from None


def search_url(criteria: SearchCriteria, page_number: int = 1) -> str:
    url = f"{SITE}/for-rent/{quote(_city_slug(criteria))}"
    # Madlan has pages for a single whole number of rooms; otherwise read all rooms
    if criteria.rooms_min == criteria.rooms_max and float(criteria.rooms_min).is_integer():
        url += f"/{int(criteria.rooms_min)}-bd"
    return url if page_number == 1 else f"{url}?page={page_number}"


def is_bot_check(html: str) -> bool:
    return "px-captcha" in html or "_pxCaptcha" in html


def parse_ssr_context(html: str) -> dict[str, Any]:
    match = _SSR_CONTEXT.search(html)
    if match is None:
        raise SourceError("The Madlan page has no search data")
    return json.loads(_UNDEFINED.sub(r"\1null", match.group(1).strip().rstrip(";")))


def search_results(context: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    try:
        result = context["reduxInitialState"]["domainData"]["searchList"]["data"]["searchPoiV2"]
    except (KeyError, TypeError):
        raise SourceError("Madlan search data has an unexpected shape") from None
    return list(result.get("poi") or []), int(result.get("total") or 0)


def _posted_at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_poi(poi: dict[str, Any]) -> ListingData | None:
    # Projects are new buildings by developers, not apartments for rent
    if poi.get("type") != "bulletin" or not poi.get("id"):
        return None
    details = poi.get("addressDetails") or {}
    point = poi.get("locationPoint") or {}
    contact_type = (poi.get("poc") or {}).get("type")
    building_class = str(poi.get("buildingClass") or "").lower()
    images = [
        IMAGE_URL.format(path=quote(image["imageUrl"].lstrip("/")))
        for image in poi.get("images") or []
        if isinstance(image, dict) and image.get("imageUrl") and not image.get("isFloorplan")
    ]
    street = clean_text(details.get("streetName"))
    number = clean_text(details.get("streetNumber"))
    return ListingData(
        source="madlan",
        source_id=str(poi["id"]),
        url=LISTING_URL.format(id=poi["id"]),
        title=clean_text(poi.get("address")),
        price=parse_price(poi.get("price")),
        rooms=parse_rooms(poi.get("beds")),
        sqm=parse_int(poi.get("area")) or None,
        floor=parse_floor(poi.get("floor")),
        property_type=BUILDING_CLASSES.get(building_class),
        city=clean_text(details.get("city")),
        neighborhood=clean_text(details.get("neighbourhood")),
        street=street,
        house_number=number,
        latitude=point.get("lat"),
        longitude=point.get("lng"),
        is_agency={"private": False, "agent": True}.get(contact_type),
        images=images,
        posted_at=_posted_at(poi.get("firstTimeSeen")),
    )


class MadlanSource(Source):
    name = "madlan"
    label = "מדלן"
    default_interval_minutes = 60
    night_interval_minutes = 120

    def search_url(self, criteria: SearchCriteria) -> str:
        return search_url(criteria)

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        listings: dict[str, ListingData] = {}
        page_number, pages = 1, 1
        while page_number <= min(pages, MAX_PAGES):
            if page_number > 1:
                # PerimeterX flags quick successive page loads
                await human_pause(45.0, 75.0)
            if custom_url:
                separator = "&" if "?" in custom_url else "?"
                url = (
                    custom_url if page_number == 1 else f"{custom_url}{separator}page={page_number}"
                )
            else:
                url = search_url(criteria, page_number)
            await page.goto(url, wait_until="domcontentloaded")
            html = await page.content()
            if is_bot_check(html):
                if page_number == 1:
                    raise BlockedError("Madlan shows a bot check (press and hold)")
                return FetchResult(list(listings.values()), complete=False)
            pois, total = search_results(parse_ssr_context(html))
            listings.update(
                (listing.source_id, listing) for poi in pois if (listing := parse_poi(poi))
            )
            pages = max(1, -(-total // PAGE_SIZE))
            page_number += 1
        return FetchResult(list(listings.values()), complete=pages <= MAX_PAGES)
