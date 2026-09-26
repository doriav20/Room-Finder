from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import homeless
from roomfinder.sources.homeless import HomelessSource, parse_details, parse_page

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = (FIXTURES / "homeless_page1.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "homeless_detail.html").read_text(encoding="utf-8")
LAST_PAGE = PAGE1.replace("הבא &gt;", "")


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

    monkeypatch.setattr(homeless, "human_pause", instant)


def test_parse_page_reads_table_rows():
    listings, has_next = parse_page(PAGE1)

    assert [listing.source_id for listing in listings] == ["700001", "700002"]
    assert has_next is True
    assert parse_page(LAST_PAGE)[1] is False


def test_listing_fields():
    first, second = parse_page(PAGE1)[0]

    assert first.url == "https://www.homeless.co.il/rent/viewad,700001.aspx"
    assert (first.price, first.rooms, first.floor) == (9500, 4.5, 3)
    assert (first.city, first.neighborhood, first.street, first.house_number) == (
        "ראשון לציון",
        "בנות חייל",
        "עליזה בגין",
        "8",
    )
    assert first.property_type == PropertyType.APARTMENT
    assert first.entry_date == "מיידי"
    assert first.posted_at == datetime(2026, 9, 26, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert first.images == ["https://uploads.homeless.co.il/rent/202609/300/nvFile1.Jpeg"]
    assert second.property_type == PropertyType.GARDEN
    assert (second.neighborhood, second.street, second.house_number) == (None, "הרצל", None)
    assert second.floor == 0
    assert second.images == []


def test_listing_page_details():
    details = parse_details(DETAIL)

    assert details.furnished is True
    assert details.furniture_details == "מטבח מודרני, פינת אוכל, מיטה זוגית וארונות קיר"
    assert details.description == "דירת 4 חדרים ענקית במרכז העיר. משופצת."
    assert (details.parking, details.parking_spaces) == (True, None)
    assert (details.sqm, details.entry_date) == (110, "מיידי")
    assert details.features == ["מעלית", "מיזוג", "מרפסת"]


def test_street_parking_is_not_parking():
    details = parse_details(DETAIL.replace("חניה: פרטית - מקורה", "חניה: ברחוב"))

    assert details.parking is False


def test_options_that_are_off():
    html = DETAIL.replace(
        'IconOption on"><span><img src="/Images/BoardImages/furniture',
        'IconOption off"><span><img src="/Images/BoardImages/furniture',
    )

    assert parse_details(html).furnished is False


def test_search_url_uses_the_city_name():
    assert HomelessSource().search_url(SearchCriteria()) == (
        "https://www.homeless.co.il/rent/city=%D7%A8%D7%90%D7%A9%D7%95%D7%9F%20%D7%9C%D7%A6%D7%99%D7%95%D7%9F"
    )


async def test_fetch_follows_pages_until_the_last_one():
    page = FakePage([PAGE1, LAST_PAGE])

    result = await HomelessSource().fetch(page, SearchCriteria(), None)

    assert page.visited[1] == HomelessSource().search_url(SearchCriteria()) + "/2"
    assert len(result.listings) == 2
    assert result.complete is True


async def test_fetch_stops_when_a_page_repeats_listings_already_seen():
    # Past the last page the site still shows "next" and repeats the first page
    page = FakePage([PAGE1, PAGE1, PAGE1])

    result = await HomelessSource().fetch(page, SearchCriteria(), None)

    assert len(page.visited) == 2
    assert len(result.listings) == 2
    assert result.complete is True


async def test_fetch_stops_at_the_page_limit():
    pages = [PAGE1.replace("700001", str(800000 + n)) for n in range(homeless.MAX_PAGES)]
    page = FakePage(pages)

    result = await HomelessSource().fetch(page, SearchCriteria(), None)

    assert len(page.visited) == homeless.MAX_PAGES
    assert result.complete is False
