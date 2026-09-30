from pathlib import Path

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import komo
from roomfinder.sources.komo import (
    KomoSource,
    parse_details,
    parse_page,
    price_range,
    search_params,
    split_price,
    with_price_range,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakePage:
    def __init__(self, pages: list[str]) -> None:
        self.pages = list(pages)
        self.visited: list[str] = []
        self._html = ""

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self._html = self.pages.pop(0)
        return None

    async def content(self):
        return self._html


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(komo, "human_pause", instant)


def test_parse_page_reads_regular_and_promoted_listings():
    listings, has_next = parse_page(fixture("komo_page1.html"))

    assert [listing.source_id for listing in listings] == ["1000001", "1000002", "1000003"]
    assert has_next is True


def test_listing_fields():
    promoted, regular, garden = parse_page(fixture("komo_page1.html"))[0]

    assert regular.url == "https://www.komo.co.il/code/nadlan/details/?modaaNum=1000002"
    assert (regular.price, regular.rooms, regular.sqm) == (6200, 4.0, 120)
    assert (regular.floor, regular.total_floors) == (3, 9)
    assert (regular.city, regular.neighborhood, regular.street, regular.house_number) == (
        "ראשון לציון",
        "מישור הנוף",
        "יהושע גיבשטיין",
        "5",
    )
    assert regular.property_type == PropertyType.APARTMENT
    assert regular.images == [
        "https://www.komo.co.il/api/modaot/tmunot/showPic/list/?picNum=22&luachNum=2&picSize=1"
    ]
    assert promoted.property_type == PropertyType.APARTMENT  # "דירות"
    assert (promoted.neighborhood, promoted.street, promoted.house_number) == (
        "ראשונים",
        "פנחסוביץ",
        None,
    )
    assert garden.price is None
    assert (garden.rooms, garden.sqm, garden.floor) == (4.5, None, 0)
    assert garden.property_type == PropertyType.GARDEN
    assert (garden.neighborhood, garden.street, garden.house_number) == (None, "הרצל", "12א")
    assert garden.images == []


def test_last_page_has_no_next():
    listings, has_next = parse_page(fixture("komo_page2.html"))

    assert [listing.property_type for listing in listings] == [PropertyType.PENTHOUSE]
    assert has_next is False


def test_listing_page_details():
    details = parse_details(fixture("komo_detail.html"))

    assert (details.furnished, details.furniture_details) == (True, "ריהוט חלקי")
    assert (details.parking, details.parking_spaces) == (True, 1)
    assert details.sqm == 125
    assert details.description == "דירה גדולה ומרווחת, שרותים כפולים,\nחניה פרטית עם שער חשמלי"
    assert details.features == ["מעלית", "מיזוג", "מחסן"]


def test_unmarked_furniture_means_unfurnished():
    html = fixture("komo_detail.html").replace('class="riut add"', 'class="riut"')

    assert parse_details(html).furnished is False


async def test_fetch_details_skips_pages_without_the_details_section():
    page = FakePage([fixture("komo_detail.html"), "<html>המודעה הוסרה</html>"])

    found = await KomoSource().fetch_details(page, "1", "https://www.komo.co.il/x")
    gone = await KomoSource().fetch_details(page, "2", "https://www.komo.co.il/y")

    assert found.furnished is True
    assert gone is None


def test_search_params():
    criteria = SearchCriteria(rooms_min=4, rooms_max=4.5, price_max=7000, private_only=True)

    assert search_params(criteria) == {
        "nehes": "1",
        "cityName": "ראשון לציון",
        "fromRooms": "4",
        "toRooms": "4.5",
        "toPrice": "7000",
        "privateOnly": "1",
    }


async def test_a_search_with_more_pages_is_split_by_price():
    # Only the first page is open without signing in, so each half is read from its first page
    page = FakePage(
        [fixture("komo_page1.html"), fixture("komo_page2.html"), fixture("komo_page2.html")]
    )

    result = await KomoSource().fetch(page, SearchCriteria(), None)

    base = KomoSource().search_url(SearchCriteria())
    assert page.visited == [base, base + "&toPrice=6200", base + "&fromPrice=6201"]
    assert [listing.source_id for listing in result.listings] == [
        "1000001",
        "1000002",
        "1000003",
        "1000004",
    ]
    assert result.complete is True


async def test_a_single_page_search_is_read_once():
    page = FakePage([fixture("komo_page2.html")])

    result = await KomoSource().fetch(page, SearchCriteria(), None)

    assert len(page.visited) == 1
    assert result.complete is True


async def test_a_split_keeps_the_price_range_of_the_search():
    page = FakePage([fixture("komo_page1.html"), fixture("komo_page2.html")] + [""] * 2)
    custom = "https://www.komo.co.il/code/nadlan/apartments-for-rent.asp?nehes=1&fromPrice=5000&toPrice=9000"

    await KomoSource().fetch(page, SearchCriteria(), custom)

    assert page.visited[1:] == [
        "https://www.komo.co.il/code/nadlan/apartments-for-rent.asp?nehes=1&fromPrice=5000&toPrice=6200",
        "https://www.komo.co.il/code/nadlan/apartments-for-rent.asp?nehes=1&fromPrice=6201&toPrice=9000",
    ]


async def test_more_than_a_page_at_one_price_is_incomplete():
    page = FakePage([fixture("komo_page1.html")])
    criteria = SearchCriteria(price_min=6200, price_max=6200)

    result = await KomoSource().fetch(page, criteria, None)

    assert len(page.visited) == 1
    assert result.complete is False


async def test_running_out_of_searches_is_incomplete(monkeypatch):
    monkeypatch.setattr(komo, "MAX_SEARCHES", 1)
    page = FakePage([fixture("komo_page1.html")])

    result = await KomoSource().fetch(page, SearchCriteria(), None)

    assert len(page.visited) == 1
    assert result.complete is False


def test_price_range_of_a_url():
    assert price_range("https://x/?nehes=1&fromPrice=3000&toPrice=8000") == (3000, 8000)
    assert price_range("https://x/?nehes=1&toPrice=8000") == (None, 8000)
    assert price_range("https://x/?nehes=1") == (None, None)


def test_with_price_range_replaces_the_range_and_the_page():
    url = "https://x/a.asp?nehes=1&fromPrice=3000&currPage=2&toPrice=9000"

    assert (
        with_price_range(url, 3000, 6000) == "https://x/a.asp?nehes=1&fromPrice=3000&toPrice=6000"
    )
    assert with_price_range(url, None, 6000) == "https://x/a.asp?nehes=1&toPrice=6000"


def test_split_price():
    listings = parse_page(fixture("komo_page1.html"))[0]  # 11900, 6200 and no price

    assert split_price(listings, None, None) == 6200
    assert split_price(listings, 7000, None) == 11900
    # Nothing seen inside the range: split it in the middle
    assert split_price(listings, 7000, 9000) == 8000
    # The split is always inside the range, so both halves are narrower
    assert split_price(listings, 6200, 6300) == 6200
    assert split_price(listings, 5000, 6200) == 6199
    assert split_price(listings, 6200, 6200) is None
    assert split_price(listings, 12000, None) is None
