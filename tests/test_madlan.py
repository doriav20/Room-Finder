import json
from datetime import UTC, datetime

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import madlan
from roomfinder.sources.base import BlockedError, SourceError
from roomfinder.sources.madlan import (
    MadlanSource,
    page_variables,
    parse_poi,
    parse_ssr_context,
    search_list,
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
    "description": "דירה מרווחת, ללא ריהוט.\nחניה בטאבו ומרפסת שמש",
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


VARIABLES = {
    "dealType": "unitRent",
    "roomsRange": [4, 4],
    "limit": 50,
    "offset": 0,
    "cursor": {"seenProjects": None, "bulletinsOffset": 0},
    "listingEdited": None,
}


def page_html(pois, total) -> str:
    context = {
        "initialPath": "/for-rent/x",
        "reduxInitialState": {
            "domainData": {
                "searchList": {
                    "data": {
                        "searchPoiV2": {
                            "total": total,
                            "cursor": {"bulletinsOffset": 49, "seenProjects": ["p1"]},
                            "poi": pois,
                        }
                    },
                    "meta": {"variables": VARIABLES},
                }
            }
        },
    }
    raw = json.dumps(context, ensure_ascii=False).replace(
        '"limit": 50', '"limit": 50, "edited": undefined'
    )
    return f"<html><body><script>window.__SSR_HYDRATED_CONTEXT__={raw};</script></body></html>"


def api_page(pois, total, bulletins_offset=99):
    result = {"total": total, "cursor": {"bulletinsOffset": bulletins_offset}, "poi": pois}
    return {"status": 200, "body": json.dumps({"data": {"searchPoiV2": result}})}


class FakePage:
    def __init__(self, pages: list[str], api_responses=()) -> None:
        self.pages = list(pages)
        self.api_responses = list(api_responses)
        self.visited: list[str] = []
        self.api_calls: list[dict] = []
        self._html = ""

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self._html = self.pages.pop(0)

    async def content(self):
        return self._html

    async def evaluate(self, script, arg):
        self.api_calls.append(arg)
        return self.api_responses.pop(0)


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(madlan, "human_pause", instant)


def test_embedded_context_with_undefined_is_parsed():
    result, variables = search_list(parse_ssr_context(page_html([BULLETIN, PROJECT], 2)))
    pois, total = search_results(result)

    assert total == 2
    assert [poi["id"] for poi in pois] == ["uamRPJPrKMD", "some_project"]
    assert variables["roomsRange"] == [4, 4]


def test_missing_context_is_an_error():
    with pytest.raises(SourceError):
        parse_ssr_context("<html></html>")


def test_bulletin_fields():
    listing = parse_poi(BULLETIN)

    assert listing.url == "https://www.madlan.co.il/listings/uamRPJPrKMD"
    assert (listing.price, listing.rooms, listing.sqm, listing.floor) == (6000, 4.0, 100, 3)
    assert listing.description == "דירה מרווחת, ללא ריהוט.\nחניה בטאבו ומרפסת שמש"
    assert (listing.furnished, listing.parking) == (False, True)
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


RISHON = "https://www.madlan.co.il/for-rent/%D7%A8%D7%90%D7%A9%D7%95%D7%9F-%D7%9C%D7%A6%D7%99%D7%95%D7%9F-%D7%99%D7%A9%D7%A8%D7%90%D7%9C"


def test_search_url_has_the_filters():
    types = "flat,gardenApartment,duplex,penthouse,attic"
    assert search_url(SearchCriteria()) == f"{RISHON}?filters=__4-4___{types}{'_' * 13}"

    every_type = SearchCriteria(
        rooms_min=4, rooms_max=4.5, price_max=7000, property_types=list(PropertyType)
    )
    assert search_url(every_type) == f"{RISHON}?filters=_-7000_4-4.5{'_' * 16}"
    assert "_3000-_" in search_url(SearchCriteria(price_min=3000))


def test_types_without_a_filter_on_the_site_read_every_type():
    criteria = SearchCriteria(property_types=[PropertyType.APARTMENT, PropertyType.STUDIO])

    assert search_url(criteria) == f"{RISHON}?filters=__4-4{'_' * 16}"


def test_unknown_city():
    with pytest.raises(SourceError):
        search_url(SearchCriteria(city="אילת"))


def test_page_variables_follow_the_previous_page():
    variables = page_variables(
        VARIABLES, {"cursor": {"bulletinsOffset": 49, "seenProjects": ["p1"]}}, 2
    )

    assert variables["offset"] == 50
    assert variables["cursor"] == {"seenProjects": ["p1"], "bulletinsOffset": 49}
    assert variables["roomsRange"] == [4, 4]
    assert "listingEdited" not in variables  # not a variable of the query


async def test_fetch_reads_the_following_pages_from_the_api():
    page = FakePage(
        [page_html([BULLETIN, PROJECT], 120)],
        [api_page([AGENT_BULLETIN], 120, 99), api_page([BULLETIN | {"id": "third"}], 120, 149)],
    )

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert page.visited == [search_url(SearchCriteria())]
    assert [call["variables"]["offset"] for call in page.api_calls] == [50, 100]
    assert [call["variables"]["cursor"]["bulletinsOffset"] for call in page.api_calls] == [49, 99]
    assert "searchPoiV2(" in page.api_calls[0]["query"]
    assert [listing.source_id for listing in result.listings] == [
        "uamRPJPrKMD",
        "XKjHvmv06I7",
        "third",
    ]
    assert result.complete is True


async def test_fetch_uses_the_custom_url():
    page = FakePage([page_html([BULLETIN], 1)])

    await MadlanSource().fetch(page, SearchCriteria(), "https://www.madlan.co.il/for-rent/x")

    assert page.visited == ["https://www.madlan.co.il/for-rent/x"]
    assert page.api_calls == []


async def test_bot_check_on_first_page_is_blocked():
    page = FakePage(['<html><div id="px-captcha"></div></html>'])

    with pytest.raises(BlockedError):
        await MadlanSource().fetch(page, SearchCriteria(), None)


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 403, "body": '<html><div id="px-captcha"></div></html>'},
        {"status": 500, "body": "oops"},
        {"status": 200, "body": '{"errors": [{"message": "bad variable"}], "data": null}'},
    ],
)
async def test_a_failing_api_keeps_the_first_page(answer):
    page = FakePage([page_html([BULLETIN], 60)], [answer])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert [listing.source_id for listing in result.listings] == ["uamRPJPrKMD"]
    assert result.complete is False


async def test_too_many_pages_is_incomplete(monkeypatch):
    monkeypatch.setattr(madlan, "MAX_PAGES", 1)
    page = FakePage([page_html([BULLETIN], 60)])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert page.api_calls == []
    assert result.complete is False


async def test_a_first_page_without_the_ads_text_is_read_again_from_the_api():
    bare = {key: value for key, value in BULLETIN.items() if key != "description"}
    page = FakePage([page_html([bare], 1)], [api_page([BULLETIN], 1)])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert page.api_calls[0]["variables"]["offset"] == 0
    assert "listingEdited" not in page.api_calls[0]["variables"]
    assert result.listings[0].description == BULLETIN["description"]


async def test_a_failing_api_keeps_the_first_page_without_the_text():
    bare = {key: value for key, value in BULLETIN.items() if key != "description"}
    page = FakePage([page_html([bare], 1)], [{"status": 500, "body": "oops"}])

    result = await MadlanSource().fetch(page, SearchCriteria(), None)

    assert [listing.description for listing in result.listings] == [None]
    assert result.complete is True
