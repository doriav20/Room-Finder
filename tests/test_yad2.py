import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from roomfinder.schemas import PropertyType, SearchCriteria
from roomfinder.sources import yad2
from roomfinder.sources.base import BlockedError, SourceError
from roomfinder.sources.yad2 import (
    Yad2Source,
    feed_api_params,
    is_challenge,
    parse_feed,
    parse_item_details,
    parse_next_data,
    search_params,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page_html(page_props) -> str:
    data = json.dumps({"props": {"pageProps": page_props}}, ensure_ascii=False)
    return (
        '<html><head><title>נדל"ן להשכרה</title></head><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{data}</script></body></html>'
    )


class FakePage:
    def __init__(self, html: str, api_responses=(), title='נדל"ן להשכרה בראשון לציון'):
        self.html = html
        self._title = title
        self.url = "https://www.yad2.co.il/realestate/rent/center-and-sharon"
        self.api_responses = list(api_responses)
        self.visited: list[str] = []
        self.api_urls: list[str] = []

    async def goto(self, url, **kwargs):
        self.visited.append(url)

    async def content(self):
        return self.html

    async def title(self):
        return self._title

    async def wait_for_timeout(self, milliseconds):
        pass

    async def evaluate(self, script, url):
        self.api_urls.append(url)
        return self.api_responses.pop(0)


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(yad2, "human_pause", instant)


def api_page2():
    return {"status": 200, "body": (FIXTURES / "yad2_api_page2.json").read_text(encoding="utf-8")}


def test_parse_feed_reads_listing_buckets_and_skips_agency_ads():
    listings = parse_feed(load("yad2_page_props.json")["feed"])

    assert [listing.source_id for listing in listings] == [
        "tok00001",
        "tok00002",
        "tok00003",
        "tok00003",
    ]


def test_listing_fields_are_mapped():
    first = parse_feed(load("yad2_page_props.json")["feed"])[0]

    assert first.url == "https://www.yad2.co.il/realestate/item/tok00001"
    assert first.title == "דירה, הרצל 53"
    assert (first.price, first.rooms, first.sqm, first.floor) == (5800, 4.0, 90, 2)
    assert first.property_type == PropertyType.APARTMENT
    assert (first.city, first.neighborhood, first.street, first.house_number) == (
        "ראשון לציון",
        'רמב"ם',
        "הרצל",
        "53",
    )
    assert (first.latitude, first.longitude) == (31.971567, 34.807158)
    assert first.is_agency is False
    assert first.images == [
        "https://img.yad2.co.il/Pic/1a.jpeg",
        "https://img.yad2.co.il/Pic/1b.jpeg",
    ]
    assert first.features == ["חניה", 'ממ"ד']


def test_missing_values_and_agency_listings():
    second, third = parse_feed(load("yad2_page_props.json")["feed"])[1:3]

    assert second.price is None  # 0 means the price wasn't given
    assert second.house_number is None
    assert second.floor == 0
    assert second.images == ["https://img.yad2.co.il/Pic/2a.jpeg"]
    assert second.property_type == PropertyType.GARDEN
    assert third.is_agency is True
    assert third.property_type == PropertyType.PENTHOUSE


def test_search_params_follow_the_criteria():
    criteria = SearchCriteria(
        rooms_min=4,
        rooms_max=4.5,
        price_max=7000,
        property_types=[PropertyType.APARTMENT, PropertyType.DUPLEX],
    )

    assert search_params(criteria) == {
        "area": "9",
        "city": "8300",
        "minRooms": "4",
        "maxRooms": "4.5",
        "maxPrice": "7000",
        "property": "1,7,51",
    }


def test_search_url_for_default_criteria():
    assert Yad2Source().search_url(SearchCriteria()) == (
        "https://www.yad2.co.il/realestate/rent/center-and-sharon"
        "?area=9&city=8300&minRooms=4&maxRooms=4&property=1%2C3%2C7%2C51%2C6"
    )


def test_unknown_city_is_an_error():
    with pytest.raises(SourceError):
        search_params(SearchCriteria(city="אילת"))


def test_feed_api_params_come_from_the_page_when_available():
    assert feed_api_params(load("yad2_page_props.json"), SearchCriteria()) == {
        "city": "8300",
        "area": "9",
        "region": "1",
        "minRooms": "4",
        "maxRooms": "4",
    }
    assert feed_api_params({}, SearchCriteria())["region"] == "1"


def test_is_challenge():
    assert is_challenge("Radware Bot Manager Captcha")
    assert is_challenge("", "https://validate.perfdrive.com/abc")
    assert not is_challenge('נדל"ן להשכרה', "https://www.yad2.co.il/realestate/rent")


def test_parse_next_data_requires_the_script():
    with pytest.raises(SourceError):
        parse_next_data("<html></html>")


async def test_fetch_reads_all_pages_and_removes_repeats():
    page = FakePage(page_html(load("yad2_page_props.json")), [api_page2()])

    result = await Yad2Source().fetch(page, SearchCriteria(), None)

    assert page.visited == [Yad2Source().search_url(SearchCriteria())]
    assert page.api_urls == [
        "https://gw.yad2.co.il/realestate-feed/rent/feed"
        "?city=8300&area=9&region=1&minRooms=4&maxRooms=4&page=2"
    ]
    assert sorted(listing.source_id for listing in result.listings) == [
        "tok00001",
        "tok00002",
        "tok00003",
        "tok00004",
    ]
    assert result.complete is True


async def test_fetch_uses_the_custom_url():
    props = copy.deepcopy(load("yad2_page_props.json"))
    props["feed"]["pagination"] = {"total": 3, "totalPages": 1}
    page = FakePage(page_html(props))

    await Yad2Source().fetch(page, SearchCriteria(), "https://www.yad2.co.il/realestate/rent?x=1")

    assert page.visited == ["https://www.yad2.co.il/realestate/rent?x=1"]
    assert page.api_urls == []


async def test_blocked_later_page_keeps_the_first_page():
    page = FakePage(page_html(load("yad2_page_props.json")), [{"status": 403, "body": "no"}])

    result = await Yad2Source().fetch(page, SearchCriteria(), None)

    assert result.complete is False
    assert {listing.source_id for listing in result.listings} == {
        "tok00001",
        "tok00002",
        "tok00003",
    }


async def test_bot_check_page_raises_blocked():
    page = FakePage("<html><body>Please wait</body></html>", title="Radware Bot Manager Captcha")

    with pytest.raises(BlockedError):
        await Yad2Source().fetch(page, SearchCriteria(), None)


def test_item_details():
    details = parse_item_details(load("yad2_item.json")["data"])

    assert details.furnished is True
    assert details.furniture_details.startswith("אפשרות לשכור את הדירה מרוהטת")
    assert (details.parking, details.parking_spaces) == (True, 2)
    assert details.description == "דירה מושלמת ומרווחת.\nיש חניון סגור."
    assert details.entry_date == "17/09/2026 (גמיש)"
    assert details.posted_at == datetime(2026, 9, 16, 15, 53, 12, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert details.features == ["מעלית", "מיזוג", "מרפסת", "משופצת"]


def test_unticked_amenities_mean_no():
    item = {"inProperty": {"includeElevator": True}, "metaData": {"description": "דירה יפה"}}

    details = parse_item_details(item)

    assert (details.furnished, details.parking, details.parking_spaces) == (False, False, None)


def test_the_ad_text_can_say_yes():
    item = {"inProperty": {}, "metaData": {"description": "הדירה מרוהטת, חניה בטאבו"}}

    details = parse_item_details(item)

    assert (details.furnished, details.parking) == (True, True)


def test_without_amenity_flags_nothing_is_assumed():
    details = parse_item_details({"metaData": {"description": "דירה יפה"}})

    assert (details.furnished, details.parking) == (None, None)


async def test_fetch_details_calls_the_item_api():
    item = {"status": 200, "body": (FIXTURES / "yad2_item.json").read_text(encoding="utf-8")}
    page = FakePage("", [item, {"status": 404, "body": ""}])

    details = await Yad2Source().fetch_details(page, "tok00001", "https://x")
    gone = await Yad2Source().fetch_details(page, "tok00002", "https://x")

    assert page.api_urls == [
        "https://gw.yad2.co.il/realestate-item/tok00001",
        "https://gw.yad2.co.il/realestate-item/tok00002",
    ]
    assert page.visited == []  # the tab is already on Yad2
    assert details.furnished is True
    assert gone is None


async def test_unexpected_page_is_a_source_error():
    page = FakePage("<html><body>תחזוקה</body></html>", title="יד2 - האתר בתחזוקה")

    with pytest.raises(SourceError):
        await Yad2Source().fetch(page, SearchCriteria(), None)
