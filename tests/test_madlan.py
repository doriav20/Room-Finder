import json
from datetime import UTC, datetime

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import madlan
from roomfinder.sources.base import BlockedError, SourceError
from roomfinder.sources.madlan import (
    MadlanSource,
    parse_poi,
    parse_ssr_context,
    search_results,
    search_url,
)

BULLETIN = {
    "__typename": "Bulletin",
    "id": "uamRPJPrKMD",
    "type": "bulletin",
    "dealType": "unitRent",
    "address": "השדה 6, ראשון לציון",
    "addressDetails": {
        "city": "ראשון לציון",
        "streetName": "השדה",
        "streetNumber": "6",
        "neighbourhood": "רמז",
    },
    "area": 100,
    "beds": 4,
    "floor": "3",
    "price": 6000,
    "firstTimeSeen": "2026-09-03T08:47:24.000Z",
    "poc": {"type": "private", "__typename": "BulletinPrivate"},
    "buildingClass": "flat",
    "locationPoint": {"lat": 31.9636744, "lng": 34.7946341},
    "images": [
        {"imageUrl": "bulletins/תמונה_9-b1a6.jpg", "isFloorplan": None},
        {"imageUrl": "bulletins/plan.jpg", "isFloorplan": True},
        {"imageUrl": "/bulletins/Screenshot_1.jpg", "isFloorplan": None},
    ],
}
AGENT_BULLETIN = BULLETIN | {
    "id": "XKjHvmv06I7",
    "address": "הרצל 10, ראשון לציון",
    "addressDetails": {"city": "ראשון לציון", "streetName": "הרצל", "streetNumber": None},
    "floor": "קרקע",
    "price": None,
    "poc": {"type": "agent", "__typename": "BulletinAgent"},
    "buildingClass": "penthouseapp",
    "images": [],
    "firstTimeSeen": None,
}
PROJECT = {"id": "some_project", "type": "project", "projectName": "פרויקט"}


def page_html(pois, total) -> str:
    context = {
        "initialPath": "/for-rent/x",
        "reduxInitialState": {
            "domainData": {
                "searchList": {
                    "data": {"searchPoiV2": {"total": total, "poi": pois}},
                    "meta": {"variables": {"offset": 0, "limit": 50}},
                }
            }
        },
    }
    raw = json.dumps(context, ensure_ascii=False).replace(
        '"limit": 50', '"limit": 50, "edited": undefined'
    )
    return f"<html><body><script>window.__SSR_HYDRATED_CONTEXT__={raw};</script></body></html>"


class FakePage:
    def __init__(self, pages: list[str]) -> None:
        self.pages = list(pages)
        self.visited: list[str] = []
        self._html = ""

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self._html = self.pages.pop(0)

    async def content(self):
        return self._html


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(madlan, "human_pause", instant)


def test_embedded_context_with_undefined_is_parsed():
    pois, total = search_results(parse_ssr_context(page_html([BULLETIN, PROJECT], 2)))

    assert total == 2
    assert [poi["id"] for poi in pois] == ["uamRPJPrKMD", "some_project"]


def test_missing_context_is_an_error():
    with pytest.raises(SourceError):
        parse_ssr_context("<html></html>")


def test_bulletin_fields():
    listing = parse_poi(BULLETIN)

    assert listing.url == "https://www.madlan.co.il/listings/uamRPJPrKMD"
    assert (listing.price, listing.rooms, listing.sqm, listing.floor) == (6000, 4.0, 100, 3)
    assert (listing.city, listing.neighborhood, listing.street, listing.house_number) == (
        "ראשון לציון",
        "רמז",
        "השדה",
        "6",
    )
    assert listing.property_type == PropertyType.APARTMENT
    assert listing.is_agency is False
    assert listing.images == [
        "https://images2.madlan.co.il/t:nonce:v=2/bulletins/%D7%AA%D7%9E%D7%95%D7%A0%D7%94_9-b1a6.jpg",
        "https://images2.madlan.co.il/t:nonce:v=2/bulletins/Screenshot_1.jpg",
    ]
    assert listing.posted_at == datetime(2026, 9, 3, 8, 47, 24, tzinfo=UTC)
    assert (listing.latitude, listing.longitude) == (31.9636744, 34.7946341)


def test_agent_bulletin_and_projects():
    listing = parse_poi(AGENT_BULLETIN)

    assert listing.is_agency is True
    assert listing.property_type == PropertyType.PENTHOUSE
    assert (listing.price, listing.floor, listing.house_number, listing.posted_at) == (
        None,
        0,
        None,
        None,
    )
    assert parse_poi(PROJECT) is None


def test_search_urls():
    rishon = "https://www.madlan.co.il/for-rent/%D7%A8%D7%90%D7%A9%D7%95%D7%9F-%D7%9C%D7%A6%D7%99%D7%95%D7%9F-%D7%99%D7%A9%D7%A8%D7%90%D7%9C"

    assert search_url(SearchCriteria()) == rishon + "/4-bd"
    assert search_url(SearchCriteria(), 2) == rishon + "/4-bd?page=2"
    assert search_url(SearchCriteria(rooms_min=4, rooms_max=4.5)) == rishon
    with pytest.raises(SourceError):
        search_url(SearchCriteria(city="אילת"))


async def test_fetch_reads_every_page():
    page = FakePage([page_html([BULLETIN, PROJECT], 60), page_html([AGENT_BULLETIN], 60)])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert page.visited == [search_url(SearchCriteria()), search_url(SearchCriteria(), 2)]
    assert [listing.source_id for listing in result.listings] == ["uamRPJPrKMD", "XKjHvmv06I7"]
    assert result.complete is True


async def test_bot_check_on_first_page_is_blocked():
    page = FakePage(['<html><div id="px-captcha"></div></html>'])

    with pytest.raises(BlockedError):
        await MadlanSource().fetch(page, SearchCriteria(), None)


async def test_bot_check_on_a_later_page_keeps_what_was_read():
    page = FakePage([page_html([BULLETIN], 60), '<div id="px-captcha"></div>'])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert [listing.source_id for listing in result.listings] == ["uamRPJPrKMD"]
    assert result.complete is False
