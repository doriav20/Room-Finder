import httpx
import pytest

from conftest import HOME, bus_line
from roomfinder import transit
from roomfinder.matching import listing_matches, mismatch_reasons
from roomfinder.models import Listing
from roomfinder.schemas import SearchCriteria

# 0.001 of a degree of latitude is about 110 meters
NEAR = (HOME[0] + 0.001, HOME[1])
FAR = (HOME[0] + 0.01, HOME[1])

ROUTES = [
    {
        "id": 1,
        "operator_ref": 25,
        "route_mkt": "27017",
        "route_short_name": "17",
        "route_direction": "1",
        "route_alternative": "#",
        "agency_name": "אלקטרה אפיקים",
        "route_long_name": "ת. מרכזית אשדוד/רציפים-אשדוד<->דרך מנחם בגין-גן יבנה-1#",
    },
    {
        "id": 2,
        "operator_ref": 25,
        "route_mkt": "27017",
        "route_short_name": "17",
        "route_direction": "2",
        "route_alternative": "#",
        "agency_name": "אלקטרה אפיקים",
        "route_long_name": "דרך מנחם בגין-גן יבנה<->ת. מרכזית אשדוד-אשדוד-2#",
    },
    {
        "id": 3,
        "operator_ref": 3,
        "route_mkt": "10017",
        "route_short_name": "17",
        "route_direction": "1",
        "route_alternative": "#",
        "agency_name": "אגד",
        "route_long_name": "קניון הזהב-ראשון לציון<->ת. רכבת משה דיין-ראשון לציון-1#",
    },
]
# The stops of a ride of each route; the two directions share a stop
RIDE_STOPS = {
    101: [("1001", "הרצל/רוטשילד"), ("1002", "ת. מרכזית")],
    102: [("1002", "ת. מרכזית"), ("1003", "הרצל/ז'בוטינסקי")],
}


@pytest.fixture
def stride(monkeypatch):
    """A stand-in for Open Bus Stride, whose timetable of today isn't out yet."""
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = request.url.params
        if request.url.path == "/gtfs_routes/list":
            if len([r for r in requests if r.url.path == request.url.path]) == 1:
                return httpx.Response(200, json=[])
            rows = [
                route
                for route in ROUTES
                if params.get("route_short_name") in (None, route["route_short_name"])
                and params.get("route_mkt") in (None, route["route_mkt"])
                and params.get("operator_refs") in (None, str(route["operator_ref"]))
            ]
            return httpx.Response(200, json=rows)
        if request.url.path == "/gtfs_rides/list":
            return httpx.Response(200, json=[{"id": 100 + int(params["gtfs_route_id"])}])
        stops = RIDE_STOPS[int(params["gtfs_ride_ids"])]
        rows = [
            {
                "gtfs_stop__code": int(code),
                "gtfs_stop__name": name,
                "gtfs_stop__lat": 31.8 + index / 1000,
                "gtfs_stop__lon": 34.65,
            }
            for index, (code, name) in enumerate(stops)
        ]
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(
        transit, "new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return requests


async def test_lines_with_a_number_are_told_apart_by_operator_and_route(stride):
    lines = await transit.find_lines("17")

    assert [(line.key, line.agency) for line in lines] == [
        ("25-27017", "אלקטרה אפיקים"),
        ("3-10017", "אגד"),
    ]
    assert lines[0].description == "ת. מרכזית אשדוד/רציפים-אשדוד ⇄ דרך מנחם בגין-גן יבנה"
    # Today had no timetable yet, so the day before was read
    days = [request.url.params["date_from"] for request in stride]
    assert len(days) == 2 and days[0] > days[1]


async def test_a_line_has_the_stops_of_both_directions_once(stride):
    line = await transit.fetch_line("25-27017")

    assert (line.number, line.agency) == ("17", "אלקטרה אפיקים")
    assert [(stop.code, stop.name) for stop in line.stops] == [
        ("1001", "הרצל/רוטשילד"),
        ("1002", "ת. מרכזית"),
        ("1003", "הרצל/ז'בוטינסקי"),
    ]
    assert line.fetched_at is not None


async def test_a_day_whose_rides_have_no_stops_yet_is_passed_over(stride, monkeypatch):
    # In the morning the day's routes and rides are out, but not yet where the rides stop
    day_of_routes: list[str] = []
    real_get = transit._get

    async def get(client, path, **params):
        rows = await real_get(client, path, **params)
        if path == "/gtfs_routes/list" and rows:
            day_of_routes.append(params["date_from"])
        if path == "/gtfs_ride_stops/list" and len(day_of_routes) == 1:
            return []
        return rows

    monkeypatch.setattr(transit, "_get", get)

    line = await transit.fetch_line("25-27017")

    assert len(line.stops) == 3
    assert len(day_of_routes) == 2 and day_of_routes[0] > day_of_routes[1]


async def test_unknown_lines_and_a_service_that_is_down_are_errors(stride):
    with pytest.raises(transit.TransitError):
        await transit.fetch_line("25-99999")
    with pytest.raises(transit.TransitError):
        await transit.fetch_line("not a line")


async def test_without_the_service_lines_cannot_be_read():
    # conftest keeps every test offline
    with pytest.raises(transit.TransitError):
        await transit.find_lines("17")


def make_listing(**overrides) -> Listing:
    fields = {
        "source": "yad2",
        "source_id": "1",
        "url": "https://example.com/1",
        "city": "ראשון לציון",
        "rooms": 4,
        "price": 6000,
        "property_type": "apartment",
        "latitude": HOME[0],
        "longitude": HOME[1],
    } | overrides
    return Listing(**fields)


def test_walks_are_worked_out_only_to_the_nearest_stops():
    points = [(HOME[0] + step / 1000, HOME[1]) for step in (5, 1, 3, 2, 30)]
    line = bus_line("17", *points)

    stops = transit.candidate_stops(line, *HOME)

    assert [stop.code for stop in stops] == ["172", "174", "173"]
    assert transit.candidate_stops(bus_line("5", (32.5, 35.0)), *HOME) == []


def test_a_walk_is_estimated_from_the_distance_until_it_is_worked_out():
    line = bus_line("17", NEAR, FAR)
    listing = make_listing()

    estimate = transit.walk_to(line, listing)
    listing.walks = {"at": list(HOME), "stops": {"171": 420, "172": 300}}
    walked = transit.walk_to(line, listing)

    # 110 meters in a straight line, a little longer on the streets
    assert (estimate.stop.code, estimate.minutes, estimate.estimated) == ("171", 2, True)
    assert estimate.at_least == 1
    # On the streets the other stop turned out quicker
    assert (walked.stop.code, walked.minutes, walked.estimated) == ("172", 5, False)


def test_walks_from_where_the_listing_used_to_be_are_not_used():
    line = bus_line("17", NEAR)
    listing = make_listing(walks={"at": [31.9, 34.7], "stops": {"171": 60}})

    assert transit.walk_to(line, listing).estimated


def test_a_listing_without_a_point_has_no_walks_but_a_copy_may_lend_its_point():
    line = bus_line("17", NEAR)
    unplaced = make_listing(latitude=None, longitude=None)

    assert transit.walk_to(line, unplaced) is None
    assert transit.walks_from([line], unplaced) == []
    assert [walk.minutes for walk in transit.walks_from([line], unplaced, [make_listing()])] == [2]


def test_listings_far_from_the_bus_lines_do_not_fit():
    near, far = bus_line("17", NEAR), bus_line("25", FAR)
    listing = make_listing()

    def search(rule: str, *lines) -> SearchCriteria:
        return SearchCriteria(bus_lines=list(lines), bus_rule=rule, bus_max_minutes=5)

    assert listing_matches(listing, search("any", near, far))
    assert mismatch_reasons(listing, search("all", near, far)) == ["bus"]
    assert mismatch_reasons(listing, search("any", far)) == ["bus"]
    assert listing_matches(listing, search("show", far))
    # Unknown never rules out: no point on the map
    assert listing_matches(make_listing(latitude=None, longitude=None), search("all", far))


def test_an_estimated_walk_rules_out_only_when_the_straight_line_is_too_long():
    # 390 meters away: 5 minutes in a straight line, an estimate of 6 on the streets
    line = bus_line("17", (HOME[0] + 0.0035, HOME[1]))
    search = SearchCriteria(bus_lines=[line], bus_max_minutes=5)
    listing = make_listing()

    assert transit.walk_to(line, listing).minutes == 6
    assert listing_matches(listing, search)

    listing.walks = {"at": list(HOME), "stops": {"171": 500}}
    assert mismatch_reasons(listing, search) == ["bus"]
    listing.walks = {"at": list(HOME), "stops": {"171": 280}}
    assert listing_matches(listing, search)


def test_lines_of_several_searches_are_listed_once():
    first, second = bus_line("17", NEAR), bus_line("25", FAR)
    searches = [SearchCriteria(bus_lines=[first]), SearchCriteria(bus_lines=[first, second])]

    assert [line.number for line in transit.lines_of(searches)] == ["17", "25"]
