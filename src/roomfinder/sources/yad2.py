"""Yad2 (yad2.co.il), Israel's largest classifieds site.

Yad2 sits behind Radware's bot manager, which turns plain HTTP clients away. So the search
page is opened in the real browser, the first page of results is read from the Next.js data
embedded in it, and the following pages come from the JSON API that the site itself calls.
"""

import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page

from roomfinder.normalize import (
    canonical_property_type,
    clean_text,
    parse_floor,
    parse_int,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError, human_pause

log = logging.getLogger(__name__)

SITE = "https://www.yad2.co.il"
FEED_API = "https://gw.yad2.co.il/realestate-feed/rent/feed"
ITEM_URL = SITE + "/realestate/item/{token}"
MAX_PAGES = 15

# Buckets in a feed page that hold listings; the others (trio, kingOfTheHar...) advertise agencies
LISTING_BUCKETS = ("private", "agency", "platinum", "booster")

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
        for number in range(2, min(pages, MAX_PAGES) + 1):
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

    async def _api_page(self, page: Page, params: dict[str, str], number: int) -> dict[str, Any]:
        url = f"{FEED_API}?{urlencode(params | {'page': str(number)})}"
        result = await page.evaluate(_FETCH_JSON, url)
        status = result["status"]
        if status in (401, 403, 429):
            raise BlockedError(f"Yad2 API answered {status}")
        if status != 200:
            raise SourceError(f"Yad2 API answered {status}")
        try:
            body = json.loads(result["body"])
        except json.JSONDecodeError:
            raise BlockedError("Yad2 API returned a page instead of JSON") from None
        feed = body.get("data") if isinstance(body, dict) else None
        if not isinstance(feed, dict):
            raise SourceError("Yad2 API response has no data")
        return feed
