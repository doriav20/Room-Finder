"""Homeless (homeless.co.il) - an old classifieds board with a table of listings per city.

The site's own filters only work through its search form, so all of the city's rentals
are read (newest first) and the room count is filtered locally.
"""

import re
from datetime import datetime
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from roomfinder.normalize import (
    canonical_property_type,
    clean_text,
    parse_floor,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import ListingData, SearchCriteria
from roomfinder.sources.base import FetchResult, Source, SourceError, human_pause

SITE = "https://www.homeless.co.il"
AD_URL = SITE + "/rent/viewad,{id}.aspx"
MAX_PAGES = 5
ISRAEL = ZoneInfo("Asia/Jerusalem")

# Columns of a listing row in the results table
PHOTO, KIND, CITY, NEIGHBORHOOD, ADDRESS, ROOMS, FLOOR, PRICE, ENTRY, UPDATED = range(1, 11)

_STREET_AND_NUMBER = re.compile(r"^(?P<street>.*?)\s+(?P<number>\d+\s*[א-ת]?)$")


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


def parse_page(html: str) -> tuple[list[ListingData], bool]:
    """The listings on one result page, and whether there is a next page."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[id^=ad_]")
    listings = [listing for row in rows if (listing := _parse_row(row)) is not None]
    has_next = soup.find("a", class_="pagingtext", string=re.compile("הבא")) is not None
    return listings, has_next


class HomelessSource(Source):
    name = "homeless"
    label = "הומלס"
    default_interval_minutes = 30
    night_interval_minutes = 60

    def search_url(self, criteria: SearchCriteria) -> str:
        return f"{SITE}/rent/city={quote(criteria.city)}"

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
            # Past the last page the site keeps offering "next" and repeats the first page
            if not has_next or not new:
                return FetchResult(list(listings.values()), complete=True)
        # Older pages hold old listings; don't treat what's beyond them as removed
        return FetchResult(list(listings.values()), complete=False)
