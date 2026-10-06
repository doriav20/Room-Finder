"""Madlan (madlan.co.il) - listings with neighborhood data.

Madlan is guarded by PerimeterX, which blocks a browser that loads a second page soon after
the first. So the search page is loaded once, its first page of results is read from the data
embedded in it, and the following pages come from the GraphQL API that the page itself calls,
with the page's own search variables. The ads' text comes with the results, so listing pages
are never loaded. The source runs rarely.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from playwright.async_api import Page

from roomfinder.normalize import (
    clean_multiline,
    clean_text,
    furniture_from_text,
    parking_from_text,
    parse_floor,
    parse_int,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, FetchResult, Source, SourceError, human_pause

log = logging.getLogger(__name__)

SITE = "https://www.madlan.co.il"
LISTING_URL = SITE + "/listings/{id}"
IMAGE_URL = "https://images2.madlan.co.il/t:nonce:v=2/{path}"
MAX_PAGES = 10

# The site's property type filter, for the types it has
FILTER_TYPES: dict[PropertyType, tuple[str, ...]] = {
    PropertyType.APARTMENT: ("flat",),
    PropertyType.GARDEN: ("gardenApartment",),
    PropertyType.DUPLEX: ("duplex",),
    PropertyType.PENTHOUSE: ("penthouse", "attic"),
    PropertyType.HOUSE: ("villa", "cottage", "dualCottage"),
}
# The filters= parameter joins 19 values with "_"; these are the ones used here
FILTER_FIELDS, PRICE, ROOMS, TYPES = 19, 1, 2, 5

# The variables of the site's own search query, with their types. The page's variables are
# sent back as they are, with the next page's offset
QUERY_VARIABLES = {
    "dealType": "String",
    "userContext": "JSONObject",
    "abtests": "JSONObject",
    "noFee": "Boolean",
    "adPlacement": "AdPlacement",
    "priceRange": "[Int]",
    "ppmRange": "[Int]",
    "monthlyTaxRange": "[Int]",
    "roomsRange": "[Float]",
    "bathsRange": "[Float]",
    "buildingClass": "[String]",
    "amenities": "inputAmenitiesFilter",
    "secureRoomType": "[SecureRoomType]",
    "generalCondition": "[GeneralCondition]",
    "sellerType": "[SellerType]",
    "floorRange": "[Int]",
    "areaRange": "[Int]",
    "tileRanges": "[TileRange]",
    "tileRangesExcl": "[TileRange]",
    "sort": "[SortField]",
    "limit": "Int",
    "offset": "Int",
    "cursor": "inputCursor",
    "poiTypes": "[PoiType]",
    "locationDocId": "String",
    "searchContext": "SearchContext",
    "underPriceEstimation": "Boolean",
    "discountedProjects": "Boolean",
    "priceDrop": "Boolean",
    "isCommercialRealEstate": "Boolean",
    "commercialAmenities": "inputCommercialAmenitiesFilter",
    "qualityClass": "[String]",
    "numberOfEmployeesRange": "[Float]",
    "creationDaysRange": "Int",
    "onlyImmediate": "Boolean",
}
# What is read of a search result: the fields parse_poi() uses
SEARCH_FIELDS = """
    total
    cursor { bulletinsOffset projectsOffset seenProjects }
    poi {
      id
      type
      firstTimeSeen
      locationPoint { lat lng }
      addressDetails { city neighbourhood streetName streetNumber }
      ... on Bulletin {
        address
        price
        beds
        description
        area
        floor
        buildingClass
        images { imageUrl isFloorplan }
        poc { type }
      }
    }
"""
SEARCH_QUERY = (
    "query searchPoi("
    + ", ".join(f"${name}: {kind}" for name, kind in QUERY_VARIABLES.items())
    + ") {\n  searchPoiV2("
    + ", ".join(f"{name}: ${name}" for name in QUERY_VARIABLES)
    + ") {"
    + SEARCH_FIELDS
    + "  }\n}"
)

# Runs in the search page, with the headers the site's own client adds
_FETCH_API = """async ({query, variables}) => {
  const endpoint = (window.__LOCALIZE_SSR_CONFIG__ || {}).apiEndpoint || '/api2';
  const response = await fetch(endpoint, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
      'X-Original-Url': location.pathname + location.search,
      'X-Original-Hostname': location.hostname,
      'X-Source': 'web',
    },
    body: JSON.stringify({operationName: 'searchPoi', variables, query}),
    signal: AbortSignal.timeout(20000),
  });
  return {status: response.status, body: await response.text()};
}"""

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


def search_url(criteria: SearchCriteria) -> str:
    """The search page, with the criteria as the site's own filters."""
    filters = [""] * FILTER_FIELDS
    if criteria.price_min or criteria.price_max:
        filters[PRICE] = f"{criteria.price_min or ''}-{criteria.price_max or ''}"
    filters[ROOMS] = f"{criteria.rooms_min:g}-{criteria.rooms_max:g}"
    # A type the site has no filter for (e.g. a studio) may be filed under any of them, so
    # then every type is read
    kinds = set(criteria.property_types)
    if kinds and kinds <= FILTER_TYPES.keys() and kinds != FILTER_TYPES.keys():
        filters[TYPES] = ",".join(
            name for kind in FILTER_TYPES if kind in kinds for name in FILTER_TYPES[kind]
        )
    query = urlencode({"filters": "_".join(filters)}, safe=",")
    return f"{SITE}/for-rent/{quote(_city_slug(criteria))}?{query}"


def is_bot_check(html: str) -> bool:
    return "px-captcha" in html or "_pxCaptcha" in html


def parse_ssr_context(html: str) -> dict[str, Any]:
    match = _SSR_CONTEXT.search(html)
    if match is None:
        raise SourceError("The Madlan page has no search data")
    return json.loads(_UNDEFINED.sub(r"\1null", match.group(1).strip().rstrip(";")))


def search_list(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The page's first search result (searchPoiV2) and the variables it was searched with."""
    try:
        found = context["reduxInitialState"]["domainData"]["searchList"]
        result = found["data"]["searchPoiV2"]
    except (KeyError, TypeError):
        raise SourceError("Madlan search data has an unexpected shape") from None
    return result, (found.get("meta") or {}).get("variables") or {}


def search_results(result: Any) -> tuple[list[dict[str, Any]], int]:
    """The listings in a search result, and how many there are in all."""
    if not isinstance(result, dict):
        raise SourceError("Madlan search result has an unexpected shape")
    return list(result.get("poi") or []), int(result.get("total") or 0)


def query_variables(variables: dict[str, Any]) -> dict[str, Any]:
    """The page's search variables that the query takes, as they are (its first page)."""
    return {name: value for name, value in variables.items() if name in QUERY_VARIABLES}


def has_descriptions(result: dict[str, Any]) -> bool:
    """Whether the results carry the ads' text (even when an ad has none)."""
    pois, _ = search_results(result)
    bulletins = [poi for poi in pois if poi.get("type") == "bulletin"]
    return not bulletins or any("description" in poi for poi in bulletins)


def page_variables(
    variables: dict[str, Any], previous: dict[str, Any], number: int
) -> dict[str, Any]:
    """The search variables for page `number`, following the page that returned `previous`."""
    cursor = previous.get("cursor") or {}
    limit = int(variables.get("limit") or 50)
    return query_variables(variables) | {
        "offset": (number - 1) * limit,
        "cursor": {
            "seenProjects": cursor.get("seenProjects"),
            "bulletinsOffset": cursor.get("bulletinsOffset"),
        },
    }


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
    # The ad's text comes with the search results; listing pages are never loaded (see above)
    description = clean_multiline(poi.get("description"))
    return ListingData(
        source="madlan",
        source_id=str(poi["id"]),
        url=LISTING_URL.format(id=poi["id"]),
        title=clean_text(poi.get("address")),
        description=description,
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
        furnished=furniture_from_text(description),
        parking=parking_from_text(description),
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
        await page.goto(custom_url or search_url(criteria), wait_until="domcontentloaded")
        html = await page.content()
        if is_bot_check(html):
            raise BlockedError("Madlan shows a bot check (press and hold)")
        result, variables = search_list(parse_ssr_context(html))
        if not has_descriptions(result):
            # The page's own data may leave out the ads' text; the API returns the same page
            # with it, like the following pages
            await human_pause(2.0, 5.0)
            try:
                result = await self._api_page(page, query_variables(variables))
            except (BlockedError, SourceError) as exc:
                log.warning("Madlan's first page is without the ads' text: %s", exc)
        pois, total = search_results(result)
        listings: dict[str, ListingData] = {}
        listings.update((listing.source_id, listing) for poi in pois if (listing := parse_poi(poi)))

        pages = max(1, -(-total // int(variables.get("limit") or 50)))
        complete = pages <= MAX_PAGES
        if not complete:
            log.warning("Madlan has %d pages of results, reading only %d", pages, MAX_PAGES)
        for number in range(2, min(pages, MAX_PAGES) + 1):
            await human_pause(2.0, 5.0)
            try:
                result = await self._api_page(page, page_variables(variables, result, number))
                pois, _ = search_results(result)
            except (BlockedError, SourceError) as exc:
                # Keep what we have: the first pages hold most of the listings
                log.warning("Madlan stopped at page %d of %d: %s", number, pages, exc)
                complete = False
                break
            listings.update(
                (listing.source_id, listing) for poi in pois if (listing := parse_poi(poi))
            )
        return FetchResult(list(listings.values()), complete=complete)

    async def _api_page(self, page: Page, variables: dict[str, Any]) -> Any:
        response = await page.evaluate(_FETCH_API, {"query": SEARCH_QUERY, "variables": variables})
        status, body = response["status"], response["body"]
        if status in (401, 403, 429) or is_bot_check(body):
            raise BlockedError(f"Madlan API answered {status}")
        if status != 200:
            raise SourceError(f"Madlan API answered {status}")
        try:
            answer = json.loads(body)
            result = (answer.get("data") or {}).get("searchPoiV2")
        except (json.JSONDecodeError, AttributeError):
            raise SourceError("Madlan API returned something other than JSON") from None
        if result is None:
            errors = answer.get("errors") or [{}]
            raise SourceError(f"Madlan API returned no results: {errors[0].get('message')}")
        return result
