"""Komo (komo.co.il) - a classifieds board with plain server-rendered result pages."""

import re
from urllib.parse import urlencode, urljoin

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

SITE = "https://www.komo.co.il"
SEARCH_PATH = "/code/nadlan/apartments-for-rent.asp"
DETAILS_URL = SITE + "/code/nadlan/details/?modaaNum={id}"
MAX_PAGES = 10

# Amenity items on a listing page (by CSS class), besides furniture and parking
AMENITIES = {
    "maalit": "מעלית",
    "mizug": "מיזוג",
    "mamad": 'ממ"ד',
    "soragim": "סורגים",
    "mahsan": "מחסן",
    "gisha": "גישה לנכים",
    "pets": "חיות מחמד",
    "boiler": "דוד שמש",
    "renovated": "משופצת",
    "longRent": "לטווח ארוך",
}

_ROOMS_AND_SIZE = re.compile(
    r"^(?P<kind>.*?)\s*(?P<rooms>\d+(?:\.\d+)?)\s*חדרים(?:\s*\((?P<sqm>\d+)\s*מ\"ר\))?"
)
_FLOOR = re.compile(r"קומה:\s*(?P<floor>-?\d+|קרקע)(?:\s*מתוך\s*(?P<total>\d+))?")
_STREET_AND_NUMBER = re.compile(r"^(?P<street>.*?)\s+(?P<number>\d+\s*[א-ת]?)$")


def search_params(criteria: SearchCriteria) -> dict[str, str]:
    params = {"nehes": "1", "cityName": criteria.city}
    params["fromRooms"] = f"{criteria.rooms_min:g}"
    params["toRooms"] = f"{criteria.rooms_max:g}"
    if criteria.price_min:
        params["fromPrice"] = str(criteria.price_min)
    if criteria.price_max:
        params["toPrice"] = str(criteria.price_max)
    if criteria.private_only:
        params["privateOnly"] = "1"
    return params


def _split_title(title: str) -> tuple[str | None, str | None, str | None, str | None]:
    """ "עיר, שכונה, רחוב 5" -> city, neighborhood, street, house number."""
    parts = [part.strip() for part in title.split(",") if part.strip()]
    if not parts:
        return None, None, None, None
    city, neighborhood, street = parts[0], None, None
    if len(parts) >= 3:
        neighborhood, street = parts[1], ", ".join(parts[2:])
    elif len(parts) == 2:
        if re.search(r"\d", parts[1]):
            street = parts[1]
        else:
            neighborhood = parts[1]
    number = None
    if street and (match := _STREET_AND_NUMBER.match(street)):
        street, number = match.group("street"), match.group("number").replace(" ", "")
    return city, neighborhood, street, number


def _parse_box(box: Tag) -> ListingData | None:
    link = box.find("a", href=re.compile(r"modaaNum=\d+"))
    if link is None:
        return None
    listing_id = re.search(r"modaaNum=(\d+)", link["href"]).group(1)
    title = clean_text(box.select_one(".title").get_text(" ")) if box.select_one(".title") else ""
    city, neighborhood, street, number = _split_title(title or "")

    description = (
        clean_text(box.select_one(".description").get_text(" "))
        if box.select_one(".description")
        else ""
    )
    rooms = sqm = kind = None
    if match := _ROOMS_AND_SIZE.search(description or ""):
        kind, rooms, sqm = match.group("kind"), match.group("rooms"), match.group("sqm")
    floor = total_floors = None
    if match := _FLOOR.search(description or ""):
        floor, total_floors = match.group("floor"), match.group("total")

    image = box.select_one(".image__wrapper img[src]")
    price = box.select_one(".price")
    return ListingData(
        source="komo",
        source_id=listing_id,
        url=DETAILS_URL.format(id=listing_id),
        title=title or None,
        price=parse_price(price.get_text()) if price else None,
        rooms=parse_rooms(rooms),
        sqm=parse_int(sqm),
        floor=parse_floor(floor),
        total_floors=parse_int(total_floors),
        property_type=canonical_property_type(kind),
        city=city,
        neighborhood=neighborhood,
        street=street,
        house_number=number,
        images=[urljoin(SITE, image["src"])] if image else [],
    )


def parse_details(html: str) -> ListingDetails:
    """A listing page: every amenity is listed, and the ones the ad has are marked "add"."""
    soup = BeautifulSoup(html, "lxml")
    section = soup.find(id="ancorThirdInfo")
    items = section.select("li") if section else []
    included = {name for li in items if "add" in (li.get("class") or []) for name in li["class"]}
    pairs = {
        clean_text(title.get_text(" ")): clean_text(content.get_text(" "))
        for element in soup.select(".secondInfoElement")
        if (title := element.select_one(".secondInfoElementTitle"))
        and (content := element.select_one(".secondInfoElementContent"))
    }
    description_element = soup.select_one("#teurWrap")
    description = (
        clean_multiline(description_element.get_text("\n")) if description_element else None
    )

    furnished = furniture_details = None
    if furniture_item := next((li for li in items if "riut" in (li.get("class") or [])), None):
        furnished = "add" in furniture_item["class"]
        label = clean_text(furniture_item.get_text(" "))
        if furnished and label and label != "ריהוט":
            furniture_details = label  # e.g. "ריהוט חלקי"

    spaces = parse_int(pairs.get("חניות"))
    return ListingDetails(
        description=description,
        furnished=furnished,
        furniture_details=furniture_details,
        parking=spaces > 0 if spaces is not None else None,
        parking_spaces=spaces or None,
        sqm=parse_int(pairs.get('מ"ר בנוי')),
        features=[label for name, label in AMENITIES.items() if name in included],
    )


def parse_page(html: str) -> tuple[list[ListingData], bool]:
    """The listings on one result page, and whether there is a next page."""
    soup = BeautifulSoup(html, "lxml")
    boxes = soup.select("div.View_Ad_Details[id^=modaa]")
    listings = [listing for box in boxes if (listing := _parse_box(box)) is not None]
    has_next = (
        soup.find("a", href=re.compile(r"currPage=\d+"), string=re.compile("»|הבא")) is not None
    )
    return listings, has_next


class KomoSource(Source):
    name = "komo"
    label = "קומו"
    default_interval_minutes = 30
    night_interval_minutes = 60
    has_details = True

    async def fetch_details(self, page: Page, source_id: str, url: str) -> ListingDetails | None:
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status == 404:
            return None
        html = await page.content()
        return parse_details(html) if "ancorThirdInfo" in html else None

    def search_url(self, criteria: SearchCriteria) -> str:
        return f"{SITE}{SEARCH_PATH}?{urlencode(search_params(criteria))}"

    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        base_url = custom_url or self.search_url(criteria)
        listings: dict[str, ListingData] = {}
        for number in range(1, MAX_PAGES + 1):
            if number > 1:
                await human_pause(2.0, 5.0)
            separator = "&" if "?" in base_url else "?"
            url = base_url if number == 1 else f"{base_url}{separator}currPage={number}"
            response = await page.goto(url, wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                raise SourceError(f"Komo answered {response.status}")
            found, has_next = parse_page(await page.content())
            new = [listing for listing in found if listing.source_id not in listings]
            listings.update((listing.source_id, listing) for listing in new)
            if not has_next or not new:
                return FetchResult(list(listings.values()), complete=True)
        return FetchResult(list(listings.values()), complete=False)
