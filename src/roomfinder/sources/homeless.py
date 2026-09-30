"""Homeless (homeless.co.il) - an old classifieds board with a table of listings per city.

The search filters go in the page's path, e.g. /rent/city=X$$inumber4=7$$inumber4_1=8 for
4-4.5 rooms, and the following pages add /2, /3...
"""

import logging
import re
from datetime import datetime
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from roomfinder.normalize import (
    canonical_property_type,
    clean_multiline,
    clean_text,
    parse_floor,
    parse_int,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, ListingDetails, SearchCriteria
from roomfinder.sources.base import FetchResult, Source, SourceError, human_pause

log = logging.getLogger(__name__)

SITE = "https://www.homeless.co.il"
AD_URL = SITE + "/rent/viewad,{id}.aspx"
MAX_PAGES = 5
ISRAEL = ZoneInfo("Asia/Jerusalem")

# Columns of a listing row in the results table
PHOTO, KIND, CITY, NEIGHBORHOOD, ADDRESS, ROOMS, FLOOR, PRICE, ENTRY, UPDATED = range(1, 11)

_STREET_AND_NUMBER = re.compile(r"^(?P<street>.*?)\s+(?P<number>\d+\s*[א-ת]?)$")

# Option labels on a listing page, written the way the other sites write them
FEATURE_LABELS = {"ממד": 'ממ"ד', "מזגן": "מיזוג"}

# The search form's room counts; a search names one by its position (from 1)
ROOM_OPTIONS = (1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 8, 9, 10)


def _room_option(rooms: float, *, round_up: bool) -> int:
    """The position of the site's room count nearest to rooms, rounding outwards."""
    if round_up:
        return next(
            (n for n, option in enumerate(ROOM_OPTIONS, 1) if option >= rooms), len(ROOM_OPTIONS)
        )
    return max((n for n, option in enumerate(ROOM_OPTIONS, 1) if option <= rooms), default=1)


def search_url(criteria: SearchCriteria) -> str:
    filters = [
        f"city={quote(criteria.city)}",
        f"inumber4={_room_option(criteria.rooms_min, round_up=False)}",
        f"inumber4_1={_room_option(criteria.rooms_max, round_up=True)}",
    ]
    if criteria.price_min:
        filters.append(f"flong3={criteria.price_min}")
    if criteria.price_max:
        filters.append(f"flong3_1={criteria.price_max}")
    return f"{SITE}/rent/" + "$$".join(filters)


def _cell(cells: list[Tag], index: int) -> str | None:
    return clean_text(cells[index].get_text(" ")) if index < len(cells) else None


def _posted_at(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text or "", "%d/%m/%Y").replace(tzinfo=ISRAEL)
    except ValueError:
        return None


def _parse_row(row: Tag) -> ListingData | None:
    match = re.fullmatch(r"ad_(\d+)", row.get("id", ""))
    cells = row.find_all("td", recursive=False)
    if match is None or len(cells) <= UPDATED:
        return None
    street, number = _cell(cells, ADDRESS), None
    if street and (parts := _STREET_AND_NUMBER.match(street)):
        street, number = parts.group("street"), parts.group("number").replace(" ", "")
    image = cells[PHOTO].find("img", src=True)
    kind = _cell(cells, KIND)
    return ListingData(
        source="homeless",
        source_id=match.group(1),
        url=AD_URL.format(id=match.group(1)),
        title=", ".join(filter(None, [kind, " ".join(filter(None, [street, number]))])) or None,
        price=parse_price(_cell(cells, PRICE)),
        rooms=parse_rooms(_cell(cells, ROOMS)),
        floor=parse_floor(_cell(cells, FLOOR)),
        property_type=canonical_property_type(kind),
        city=_cell(cells, CITY),
        neighborhood=_cell(cells, NEIGHBORHOOD),
        street=street,
        house_number=number,
        entry_date=_cell(cells, ENTRY),
        posted_at=_posted_at(_cell(cells, UPDATED)),
        images=[urljoin(SITE, image["src"])] if image else [],
    )


def parse_details(html: str) -> ListingDetails:
    """A listing page: property options are icons marked "on" or "off"."""
    soup = BeautifulSoup(html, "lxml")
    options: dict[str, tuple[bool | None, str | None]] = {}
    for option in soup.select("div.IconOption"):
        classes = option.get("class") or []
        label, _, value = (clean_text(option.get_text(" ")) or "").partition(":")
        state = True if "on" in classes else False if "off" in classes else None
        options[label.strip()] = (state, value.strip() or None)

    remarks = soup.select_one("div.remarks")
    text = clean_multiline(remarks.get_text("\n")) if remarks else None
    description, _, furniture_details = (text or "").partition("פירוט הריהוט:")

    parking_state, parking_kind = options.get("חניה", (None, None))
    parking = parking_state
    if parking_state and parking_kind and "ברחוב" in parking_kind:
        parking = False  # street parking isn't a parking of the apartment's own
    return ListingDetails(
        description=clean_multiline(description),
        furnished=options.get("ריהוט", (None, None))[0],
        furniture_details=clean_text(furniture_details),
        parking=parking,
        sqm=parse_int(options.get('מ"ר', (None, None))[1]),
        entry_date=options.get("כניסה", (None, None))[1],
        features=[
            FEATURE_LABELS.get(label, label)
            for label, (state, _) in options.items()
            if state and label not in ("ריהוט", "חניה")
        ],
    )


def parse_page(html: str) -> tuple[list[ListingData], bool]:
    """The listings on one result page, and whether there is a next page."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[id^=ad_]")
    listings = [listing for row in rows if (listing := _parse_row(row)) is not None]
    # On the last page "next" is still there, but disabled and without a link
    has_next = soup.find("a", class_="pagingtext", href=True, string=re.compile("הבא")) is not None
    return listings, has_next


class HomelessSource(Source):
    name = "homeless"
    label = "הומלס"
    default_interval_minutes = 30
    night_interval_minutes = 60
    has_details = True

    async def fetch_details(self, page: Page, source_id: str, url: str) -> ListingDetails | None:
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status == 404:
            return None
        html = await page.content()
        return parse_details(html) if "IconOption" in html else None

    def search_url(self, criteria: SearchCriteria) -> str:
        return search_url(criteria)

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        base_url = (custom_url or self.search_url(criteria)).rstrip("/")
        listings: dict[str, ListingData] = {}
        for number in range(1, MAX_PAGES + 1):
            if number > 1:
                await human_pause(2.0, 5.0)
            url = base_url if number == 1 else f"{base_url}/{number}"
            response = await page.goto(url, wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                raise SourceError(f"Homeless answered {response.status}")
            found, has_next = parse_page(await page.content())
            new = [listing for listing in found if listing.source_id not in listings]
            listings.update((listing.source_id, listing) for listing in new)
            # Past the last page the site repeats the first page
            if not has_next or not new:
                return FetchResult(list(listings.values()), complete=True)
        # Older pages hold old listings; don't treat what's beyond them as removed
        log.warning("Homeless has more than %d pages of results", MAX_PAGES)
        return FetchResult(list(listings.values()), complete=False)
