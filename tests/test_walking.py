import httpx
import pytest

from conftest import HOME, ROUTING_URL, bus_line
from roomfinder import transit, walking
from roomfinder.db import utcnow
from roomfinder.models import Listing

NEAR = (HOME[0] + 0.001, HOME[1])
NEARBY = (HOME[0] + 0.002, HOME[1])


@pytest.fixture
def routing(monkeypatch):
    """A stand-in for the routing server: the walk to the first stop asked about takes 100
    seconds, to the second 200, and so on."""
    requests: list[httpx.Request] = []
    state = {"down": False}

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if state["down"]:
            return httpx.Response(503)
        sources = request.url.params["sources"].split(";")
        stops = request.url.params["destinations"].split(";")
        row = [100.4 * (index + 1) for index in range(len(stops))]
        return httpx.Response(200, json={"code": "Ok", "durations": [row for _ in sources]})

    monkeypatch.setattr(
        transit, "new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    return requests, state


async def add_listing(sessionmaker, source_id: str, point=HOME, **overrides) -> int:
    now = utcnow()
    async with sessionmaker() as session:
        listing = Listing(
            source="yad2",
            source_id=source_id,
            url=f"https://example.com/{source_id}",
            latitude=point and point[0],
            longitude=point and point[1],
            first_seen_at=now,
            last_seen_at=now,
            images=[],
            features=[],
            **overrides,
        )
        session.add(listing)
        await session.commit()
        return listing.id


async def test_walks_are_asked_from_the_listings_to_the_stops(routing):
    requests, _ = routing
    line = bus_line("17", NEAR, NEARBY)

    async with transit.new_client() as client:
        table = await walking.walk_table(client, ROUTING_URL, [HOME, NEAR], line.stops)

    assert table == [{"171": 100, "172": 201}] * 2
    # Longitude first, the listings first
    assert str(requests[0].url).startswith(
        "http://routing.test/table/v1/foot/34.800000,31.960000;34.800000,31.961000"
        ";34.800000,31.961000;34.800000,31.962000"
    )
    params = requests[0].url.params
    assert (params["sources"], params["destinations"]) == ("0;1", "2;3")


async def test_a_stop_without_a_way_to_it_is_remembered_as_such(monkeypatch):
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "Ok", "durations": [[None]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        assert await walking.walk_table(
            client, ROUTING_URL, [HOME], bus_line("17", NEAR).stops
        ) == [{"171": None}]


async def test_walks_are_stored_on_the_listings_that_lack_them(routing, sessionmaker):
    requests, _ = routing
    line = bus_line("17", NEAR)
    placed = await add_listing(sessionmaker, "a")
    unplaced = await add_listing(sessionmaker, "b", point=None)

    async with sessionmaker() as session:
        assert await walking.pending(session, [line]) == [placed]
    filled = await walking.fill_walks(sessionmaker, [placed, unplaced], [line], ROUTING_URL)

    assert filled == [placed]
    async with sessionmaker() as session:
        listing = await session.get(Listing, placed)
        assert listing.walks == {"at": list(HOME), "stops": {"171": 100}}
        assert transit.walk_to(line, listing).minutes == 2
        assert await walking.pending(session, [line]) == []
    # Without a routing server nothing is worked out
    assert await walking.fill_walks(sessionmaker, [placed], [bus_line("9", NEARBY)], "") == []
    # Nothing is asked again, and another line adds to what is known
    assert await walking.fill_walks(sessionmaker, [placed], [line], ROUTING_URL) == []
    assert await walking.fill_walks(
        sessionmaker, [placed], [line, bus_line("25", NEARBY)], ROUTING_URL
    )
    assert len(requests) == 2
    async with sessionmaker() as session:
        assert (await session.get(Listing, placed)).walks["stops"] == {"171": 100, "251": 100}


async def test_listings_are_asked_about_in_groups_that_fit_a_request(
    routing, sessionmaker, monkeypatch
):
    requests, _ = routing
    monkeypatch.setattr(walking, "GROUP_LISTINGS", 2)
    # Each listing has a stop of its own next to it, and they share none
    points = [(HOME[0] + index / 10, HOME[1]) for index in range(5)]
    line = bus_line("17", *((lat + 0.001, lon) for lat, lon in points))
    ids = [await add_listing(sessionmaker, str(index), point) for index, point in enumerate(points)]

    assert await walking.fill_walks(sessionmaker, ids, [line], ROUTING_URL) == ids

    assert [request.url.params["sources"] for request in requests] == ["0;1", "0;1", "0"]
    async with sessionmaker() as session:
        walks = [(await session.get(Listing, listing_id)).walks["stops"] for listing_id in ids]
    # Each listing keeps the walks to the stops near it only
    assert walks == [{"171": 100}, {"172": 201}, {"173": 100}, {"174": 201}, {"175": 100}]


async def test_a_routing_server_that_is_down_leaves_estimates_and_is_left_alone(
    routing, sessionmaker, monkeypatch
):
    requests, state = routing
    state["down"] = True
    line = bus_line("17", NEAR)
    monkeypatch.setattr(walking, "GROUP_LISTINGS", 1)
    ids = [await add_listing(sessionmaker, str(index)) for index in range(5)]

    assert await walking.fill_walks(sessionmaker, ids, [line], ROUTING_URL) == []

    assert len(requests) == walking.MAX_FAILURES
    async with sessionmaker() as session:
        listing = await session.get(Listing, ids[0])
        assert listing.walks is None
        assert transit.walk_to(line, listing).estimated
