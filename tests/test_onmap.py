import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources.base import BlockedError, SourceError
from roomfinder.sources.onmap import OnMapSource, api_url, parse_response, search_url

BODY = (Path(__file__).parent / "fixtures" / "onmap_search.json").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self):
        return self._text


class FakePage:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.visited: list[str] = []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        return self.response


def test_parse_response_skips_short_term_and_broken_items():
    listings = parse_response(json.loads(BODY))

    assert [listing.source_id for listing in listings] == ["HkDBhvJNMx", "Abc123"]


def test_listing_fields():
    first, second = parse_response(json.loads(BODY))

    assert first.url == (
        "https://www.onmap.co.il/home-details/apartment-for-rent-rishon-lezion-rishon-lezion-hkdbhvjnmx"
    )
    assert (first.price, first.rooms, first.sqm, first.floor, first.total_floors) == (
        7000,
        4.0,
        107,
        1,
        5,
    )
    assert (first.city, first.neighborhood, first.street, first.house_number) == (
        "ראשון לציון",
        None,
        'זלמן שז"ר',
        "29",
    )
    assert first.property_type == PropertyType.APARTMENT
    assert first.parking is True
    assert first.features == []
    assert first.images == ["https://onmap.pro/images/1.gallery.webp"]
    assert first.posted_at == datetime(2026, 7, 11, 7, 9, 51, 218000, tzinfo=UTC)
    assert (second.price, second.sqm, second.floor, second.house_number) == (None, None, 0, None)
    assert second.neighborhood == "נווה הדרים"
    assert second.property_type == PropertyType.PENTHOUSE
    assert second.parking is None
    assert second.features == []
    assert second.posted_at is None


def test_api_url_lists_every_half_room_in_range():
    url = api_url(SearchCriteria(rooms_min=4, rooms_max=5))

    assert url.startswith("https://phoenix.onmap.co.il/v1/properties/mixed_search?option=rent")
    assert "city=rishon-letsiyon" in url
    assert url.endswith("rooms%5B%5D=4&rooms%5B%5D=4.5&rooms%5B%5D=5")


def test_search_url_for_default_criteria():
    assert search_url(SearchCriteria()) == (
        "https://www.onmap.co.il/search/homes/rent/rishon-letsiyon/rooms_4"
        "/types_apartment,garden_apartment,duplex,triplex,penthouse,mini_penthouse,rooftop_apartment"
    )


def test_search_url_with_prices_rooms_and_every_type():
    criteria = SearchCriteria(
        rooms_min=3.5, rooms_max=5, price_max=8000, property_types=list(PropertyType)
    )

    assert search_url(criteria) == (
        "https://www.onmap.co.il/search/homes/rent/rishon-letsiyon/price_0-8000/rooms_3,4,5"
    )
    assert search_url(SearchCriteria(price_min=3000)).split("/")[7] == "price_3000"
    assert "/rooms_5,6/" in search_url(SearchCriteria(rooms_min=5, rooms_max=9))


def test_unknown_city():
    with pytest.raises(SourceError):
        api_url(SearchCriteria(city="אילת"))


async def test_fetch_reads_the_api():
    page = FakePage(FakeResponse(200, BODY))

    result = await OnMapSource().fetch(page, SearchCriteria(), None)

    assert page.visited == [api_url(SearchCriteria())]
    assert len(result.listings) == 2
    assert result.complete is True


async def test_blocked_or_html_answers():
    with pytest.raises(BlockedError):
        await OnMapSource().fetch(FakePage(FakeResponse(403, "")), SearchCriteria(), None)
    with pytest.raises(BlockedError):
        await OnMapSource().fetch(FakePage(FakeResponse(200, "<html>")), SearchCriteria(), None)
    with pytest.raises(SourceError):
        await OnMapSource().fetch(FakePage(FakeResponse(500, "")), SearchCriteria(), None)


DETAILS = (Path(__file__).parent / "fixtures" / "onmap_details.json").read_text(encoding="utf-8")


async def test_fetch_details_reads_the_ad_text():
    page = FakePage(FakeResponse(200, DETAILS))

    details = await OnMapSource().fetch_details(page, "HJBym0y1-e", "https://www.onmap.co.il/x")

    assert page.visited == ["https://phoenix.onmap.co.il/v1/properties/HJBym0y1-e"]
    assert details.description == (
        "דירה גדולה ומרווחת בשכונה ירוקה, דוד שמש חדש, מזגנים חדשים.\nחניה פרטית ומרכז מסחרי קטן."
    )


async def test_fetch_details_of_a_listing_that_is_gone():
    gone = json.dumps(json.loads(DETAILS) | {"is_active": False})
    source = OnMapSource()

    assert await source.fetch_details(FakePage(FakeResponse(404, "")), "x", "") is None
    assert await source.fetch_details(FakePage(FakeResponse(200, gone)), "x", "") is None
    with pytest.raises(BlockedError):
        await source.fetch_details(FakePage(FakeResponse(429, "")), "x", "")
