"""Yad2 (yad2.co.il), Israel's largest classifieds site.

Yad2 sits behind Radware's bot manager, which turns plain HTTP clients away. So the search
page is opened in the real browser, and every page of results, the first one included, is
read from the JSON API that the site itself calls. The page's own first page (the Next.js data
embedded in it) mixes private and agency listings differently from the API's first page, so
it isn't enough by itself.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from playwright.async_api import Page

from roomfinder.normalize import (
    canonical_property_type,
    clean_multiline,
    clean_text,
    furniture_from_text,
    parking_from_text,
    parse_floor,
    parse_int,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, ListingDetails, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError, human_pause

log = logging.getLogger(__name__)

SITE = "https://www.yad2.co.il"
FEED_API = "https://gw.yad2.co.il/realestate-feed/rent/feed"
ITEM_API = "https://gw.yad2.co.il/realestate-item/{token}"
ITEM_URL = SITE + "/realestate/item/{token}"
MAX_PAGES = 15
ISRAEL = ZoneInfo("Asia/Jerusalem")

# Amenity flags of a listing (inProperty). A flag the advertiser didn't tick is left out.
AMENITY_FLAGS = {
    "includeElevator": "מעלית",
    "includeAirconditioner": "מיזוג",
    "includeBalcony": "מרפסת",
    "includeSecurityRoom": 'ממ"ד',
    "includeWarehouse": "מחסן",
    "includeBars": "סורגים",
    "includeBoiler": "דוד שמש",
    "isRenovated": "משופצת",
    "isHandicapped": "גישה לנכים",
    "isPetsAllowed": "חיות מחמד",
}

# Buckets in a feed page that hold listings; the others (trio, kingOfTheHar...) advertise agencies.
# The search page holds its regular listings in "yad2", the API splits them into private/agency
LISTING_BUCKETS = ("yad2", "private", "agency", "platinum", "booster")

CITIES: dict[str, dict[str, Any]] = {
    "ראשון לציון": {"region": 1, "region_slug": "center-and-sharon", "area": 9, "city": 8300},
}

PROPERTY_CODES: dict[PropertyType, tuple[str, ...]] = {
    PropertyType.APARTMENT: ("1",),
    PropertyType.GARDEN: ("3",),
    PropertyType.PENTHOUSE: ("6",),
    PropertyType.DUPLEX: ("7", "51"),
    PropertyType.HOUSE: ("5", "39", "32", "55"),
    PropertyType.UNIT: ("11",),
    PropertyType.STUDIO: ("4",),
    PropertyType.OTHER: ("49",),
}

CHALLENGE_MARKERS = ("radware", "shieldsquare", "perfdrive", "captcha", "bot manager")

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)

_FETCH_JSON = """async (url) => {
  const response = await fetch(url, {credentials: 'include', signal: AbortSignal.timeout(20000)});
  return {status: response.status, body: await response.text()};
}"""


def _city(criteria: SearchCriteria) -> dict[str, Any]:
    try:
        return CITIES[criteria.city]
    except KeyError:
        raise SourceError(f"No Yad2 area code for {criteria.city}") from None


def _format_rooms(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def search_params(criteria: SearchCriteria) -> dict[str, str]:
    city = _city(criteria)
    params = {
        "area": str(city["area"]),
        "city": str(city["city"]),
        "minRooms": _format_rooms(criteria.rooms_min),
        "maxRooms": _format_rooms(criteria.rooms_max),
    }
    if criteria.price_min:
        params["minPrice"] = str(criteria.price_min)
    if criteria.price_max:
        params["maxPrice"] = str(criteria.price_max)
    codes = [code for kind in criteria.property_types for code in PROPERTY_CODES.get(kind, ())]
    if codes:
        params["property"] = ",".join(codes)
    return params


def is_challenge(*texts: str) -> bool:
    joined = " ".join(texts).lower()
    return any(marker in joined for marker in CHALLENGE_MARKERS)


def parse_next_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA.search(html)
    if match is None:
        raise SourceError("The Yad2 page has no __NEXT_DATA__")
    return json.loads(match.group(1))


def _text(value: Any) -> str | None:
    return clean_text(value.get("text")) if isinstance(value, dict) else None


def parse_listing(item: dict[str, Any]) -> ListingData | None:
    token = item.get("token")
    if not token:
        return None
    address = item.get("address") or {}
    house = address.get("house") or {}
    coords = address.get("coords") or {}
    details = item.get("additionalDetails") or {}
    meta = item.get("metaData") or {}

    property_text = _text(details.get("property"))
    street = _text(address.get("street"))
    number = house.get("number")
    house_number = str(number) if number not in (None, "", 0) else None
    images = [url for url in meta.get("images") or [] if isinstance(url, str)]
    if not images and isinstance(meta.get("coverImage"), str):
        images = [meta["coverImage"]]
    features = [
        name
        for tag in item.get("tags") or []
        if isinstance(tag, dict) and (name := clean_text(tag.get("name")))
    ]
    ad_type = item.get("adType")

    return ListingData(
        source="yad2",
        source_id=str(token),
        url=ITEM_URL.format(token=token),
        title=", ".join(
            filter(None, [property_text, " ".join(filter(None, [street, house_number]))])
        )
        or None,
        price=parse_price(item.get("price")),
        rooms=parse_rooms(details.get("roomsCount")),
        sqm=parse_int(details.get("squareMeter")) or None,
        floor=parse_floor(house.get("floor")),
        property_type=canonical_property_type(property_text),
        city=_text(address.get("city")),
        neighborhood=_text(address.get("neighborhood")),
        street=street,
        house_number=house_number,
        latitude=coords.get("lat"),
        longitude=coords.get("lon"),
        is_agency=ad_type != "private" if ad_type else None,
        images=images,
        features=features,
        raw=item,
    )


def parse_feed(feed: dict[str, Any]) -> list[ListingData]:
    listings: list[ListingData] = []
    for bucket in LISTING_BUCKETS:
        for item in feed.get(bucket) or []:
            if isinstance(item, dict) and (listing := parse_listing(item)) is not None:
                listings.append(listing)
    return listings


def total_pages(feed: dict[str, Any]) -> int:
    return int((feed.get("pagination") or {}).get("totalPages") or 1)


def _local_datetime(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=ISRAEL)


def _entry_date(flags: dict[str, Any], details: dict[str, Any]) -> str | None:
    if flags.get("isImmediateEntrance"):
        return "מיידי"
    entrance = _local_datetime(details.get("entranceDate"))
    if entrance is None:
        return None
    flexible = " (גמיש)" if details.get("isEnterDateFlexible") else ""
    return f"{entrance:%d/%m/%Y}{flexible}"


def parse_item_details(item: dict[str, Any]) -> ListingDetails:
    """The listing's own data, from the item API."""
    flags = item.get("inProperty") or {}
    details = item.get("additionalDetails") or {}
    description = clean_multiline((item.get("metaData") or {}).get("description"))
    furniture_details = clean_text(item.get("furnitureInfo"))
    spaces = parse_int(details.get("parkingSpacesCount"))

    text_furnished = furniture_from_text(" ".join(filter(None, [furniture_details, description])))
    text_parking = parking_from_text(description)
    if isinstance(item.get("inProperty"), dict):
        # Unticked amenities are left out of inProperty, so "missing" means "no" - unless
        # the ad's own text says otherwise
        furnished = bool(flags.get("includeFurniture")) or bool(text_furnished)
        parking = bool(flags.get("includeParking")) or bool(spaces) or bool(text_parking)
    else:
        furnished = text_furnished
        parking = True if spaces else text_parking

    return ListingDetails(
        description=description,
        furnished=furnished,
        furniture_details=furniture_details,
        parking=parking,
        parking_spaces=spaces or None,
        entry_date=_entry_date(flags, details),
        posted_at=_local_datetime((item.get("dates") or {}).get("createdAt")),
        features=[label for flag, label in AMENITY_FLAGS.items() if flags.get(flag)],
    )


def feed_api_params(page_props: dict[str, Any], criteria: SearchCriteria) -> dict[str, str]:
    """The API parameters the page itself used (fall back to our own when missing)."""
    key = page_props.get("feedQueryKey")
    if isinstance(key, list) and len(key) > 1 and isinstance(key[1], dict):
        params = {name: str(value) for name, value in key[1].items() if value is not None}
    else:
        params = search_params(criteria) | {"region": str(_city(criteria)["region"])}
    params.pop("page", None)
    return params


class Yad2Source(Source):
    name = "yad2"
    label = "יד2"
    default_interval_minutes = 10
    night_interval_minutes = 30
    has_details = True

    def search_url(self, criteria: SearchCriteria) -> str:
        slug = _city(criteria)["region_slug"]
        return f"{SITE}/realestate/rent/{slug}?{urlencode(search_params(criteria))}"

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        await page.goto(custom_url or self.search_url(criteria), wait_until="domcontentloaded")
        html = await self._results_page_html(page)
        page_props = parse_next_data(html).get("props", {}).get("pageProps", {})
        feed = page_props.get("feed")
        if not isinstance(feed, dict):
            raise SourceError("The Yad2 page has no feed data")

        listings = parse_feed(feed)
        pages = total_pages(feed)
        params = feed_api_params(page_props, criteria)
        complete = pages <= MAX_PAGES
        if not complete:
            log.warning("Yad2 has %d pages of results, reading only %d", pages, MAX_PAGES)
        for number in range(1, min(pages, MAX_PAGES) + 1):
            await human_pause(1.5, 4.0)
            try:
                listings.extend(parse_feed(await self._api_page(page, params, number)))
            except (BlockedError, SourceError) as exc:
                # Keep what we have: the newest listings are on the first pages
                log.warning("Yad2 stopped at page %d of %d: %s", number, pages, exc)
                complete = False
                break

        unique = {listing.source_id: listing for listing in listings}
        return FetchResult(list(unique.values()), complete=complete)

    async def _results_page_html(self, page: Page) -> str:
        # Radware may show a challenge first; give one that solves itself a moment
        for _ in range(3):
            html = await page.content()
            title = await page.title()
            if "__NEXT_DATA__" in html:
                return html
            if not is_challenge(title, page.url, html[:5000]):
                raise SourceError(f"Unexpected Yad2 page: {title!r}")
            await page.wait_for_timeout(5000)
        raise BlockedError(f"Yad2 shows a bot check: {await page.title()!r}")

    async def fetch_details(self, page: Page, source_id: str, url: str) -> ListingDetails | None:
        # The API is called from a Yad2 page, like the site does; normally the tab is still
        # on the search results
        if not page.url.startswith(SITE):
            await page.goto(f"{SITE}/realestate/rent", wait_until="domcontentloaded")
        item = await self._api_json(page, ITEM_API.format(token=source_id), allow_missing=True)
        return parse_item_details(item) if item is not None else None

    async def _api_page(self, page: Page, params: dict[str, str], number: int) -> dict[str, Any]:
        url = f"{FEED_API}?{urlencode(params | {'page': str(number)})}"
        return await self._api_json(page, url)

    async def _api_json(
        self, page: Page, url: str, *, allow_missing: bool = False
    ) -> dict[str, Any] | None:
        result = await page.evaluate(_FETCH_JSON, url)
        status = result["status"]
        if status in (401, 403, 429):
            raise BlockedError(f"Yad2 API answered {status}")
        if status == 404 and allow_missing:
            return None
        if status != 200:
            raise SourceError(f"Yad2 API answered {status}")
        try:
            body = json.loads(result["body"])
        except json.JSONDecodeError:
            raise BlockedError("Yad2 API returned a page instead of JSON") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise SourceError("Yad2 API response has no data")
        return data
