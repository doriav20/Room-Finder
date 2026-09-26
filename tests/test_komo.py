from pathlib import Path

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import komo
from roomfinder.sources.komo import KomoSource, parse_page, search_params

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


async def test_fetch_follows_pages():
    page = FakePage([fixture("komo_page1.html"), fixture("komo_page2.html")])

    result = await KomoSource().fetch(page, SearchCriteria(), None)

    assert len(page.visited) == 2
    assert page.visited[1].endswith("&currPage=2")
    assert [listing.source_id for listing in result.listings] == [
        "1000001",
        "1000002",
        "1000003",
        "1000004",
    ]
    assert result.complete is True
