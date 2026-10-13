"""Bus lines and their stops, and how long the walk from a listing to a line is.

The lines and stops come from Open Bus Stride (the Ministry of Transport's timetables, served
by Hasadna), read once when a user adds a line. A line here is an operator's catalog number:
both directions and every variation of the route, with all their stops.

The walk to a line is the quickest walk to one of its stops, on the streets (walking.py works
those out and keeps them on the listing). Until a walk is worked out, it is estimated from the
distance.
"""

import math
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx

from roomfinder import areas
from roomfinder.db import utcnow
from roomfinder.schemas import BusLine, BusStop

STRIDE_URL = "https://open-bus-stride-api.hasadna.org.il"
TIMEOUT_SECONDS = 20
# The timetable of a day is published late sometimes, and not every line runs every day
ROUTE_DAYS_BACK = 7
# Walks are worked out to the stops nearest to the listing as the crow flies, if any is near
NEAREST_STOPS = 3
MAX_STOP_METERS = 2000
# For the estimate: 5 km/h, on streets that are longer than the straight line
WALK_METERS_PER_SECOND = 5000 / 3600
DETOUR = 1.3

LINE_KEY = re.compile(r"^(\d+)-(\w+)$")
# "מוצא<->יעד-1#": the direction and the variation of the route come after the names
_ROUTE_SUFFIX = re.compile(r"-\w[#\w]*$")


class TransitError(Exception):
    """The bus lines couldn't be read."""


class Walkable(Protocol):
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class LineCandidate:
    """A line that goes by a number, for the user to tell apart from others with the number."""

    key: str
    number: str
    agency: str
    description: str


@dataclass(frozen=True)
class LineWalk:
    line: BusLine
    stop: BusStop
    minutes: int
    # Not on the streets yet: from the distance
    estimated: bool
    # No walk is shorter than the straight line, so an estimate still says this much for sure
    at_least: int


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": "RoomFinder"})


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> list[dict[str, Any]]:
    try:
        response = await client.get(STRIDE_URL + path, params=params)
        response.raise_for_status()
        rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TransitError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(rows, list):
        raise TransitError("Unexpected answer from the bus lines service")
    return rows


async def _routes(client: httpx.AsyncClient, **filters: Any) -> AsyncIterator[list[dict[str, Any]]]:
    """The routes of each of the last days that has any, from today back."""
    today = utcnow().date()
    for back in range(ROUTE_DAYS_BACK):
        day = (today - timedelta(days=back)).isoformat()
        rows = await _get(
            client, "/gtfs_routes/list", date_from=day, date_to=day, limit=1000, **filters
        )
        if rows:
            yield rows


async def _stops(client: httpx.AsyncClient, routes: list[dict[str, Any]]) -> dict[str, BusStop]:
    """The stops of these routes, as one ride of each route stops at them."""
    stops: dict[str, BusStop] = {}
    for route in routes:
        rides = await _get(client, "/gtfs_rides/list", gtfs_route_id=route["id"], limit=1)
        if not rides:
            continue
        for row in await _get(
            client, "/gtfs_ride_stops/list", gtfs_ride_ids=rides[0]["id"], limit=1000
        ):
            lat, lon = row.get("gtfs_stop__lat"), row.get("gtfs_stop__lon")
            if lat is None or lon is None or row.get("gtfs_stop__code") is None:
                continue
            code = str(row["gtfs_stop__code"])
            stops[code] = BusStop(
                code=code, name=row.get("gtfs_stop__name") or "", lat=lat, lon=lon
            )
    return stops


def _key(route: dict[str, Any]) -> str:
    return f"{route['operator_ref']}-{route['route_mkt']}"


def _description(routes: list[dict[str, Any]]) -> str:
    """Where the line goes, from its main route: 'מוצא ⇄ יעד'."""
    main = min(
        routes, key=lambda route: (route["route_alternative"] != "#", route["route_direction"])
    )
    name = _ROUTE_SUFFIX.sub("", main["route_long_name"] or "")
    return " ⇄ ".join(part.strip() for part in name.split("<->"))


def _by_line(routes: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    lines: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        if route.get("operator_ref") is not None and route.get("route_mkt"):
            lines.setdefault(_key(route), []).append(route)
    return lines


async def find_lines(number: str) -> list[LineCandidate]:
    """The lines with this number, anywhere in the country."""
    async with new_client() as http:
        routes = await anext(_routes(http, route_short_name=number), [])
    return [
        LineCandidate(key, number, group[0]["agency_name"] or "", _description(group))
        for key, group in _by_line(
            route for route in routes if route["route_short_name"] == number
        ).items()
    ]


async def fetch_line(key: str) -> BusLine:
    """A line with the stops of all its routes."""
    found = LINE_KEY.match(key)
    if found is None:
        raise TransitError("Unknown line")
    operator, catalog = found.groups()
    routes: list[dict[str, Any]] | None = None
    stops: dict[str, BusStop] = {}
    async with new_client() as http:
        # The routes and rides of a day are out before the stops of its rides (for hours, in
        # the morning), so a day whose rides have no stops yet is passed over
        async for day in _routes(http, operator_refs=operator, route_mkt=catalog):
            routes = _by_line(day).get(key)
            stops = await _stops(http, routes) if routes else {}
            if stops:
                break
    if not routes:
        raise TransitError("The line wasn't found")
    if not stops:
        raise TransitError("The line has no stops")
    return BusLine(
        key=key,
        number=routes[0]["route_short_name"],
        agency=routes[0]["agency_name"] or "",
        description=_description(routes),
        stops=list(stops.values()),
        fetched_at=utcnow(),
    )


def lines_of(searches: Iterable[Any]) -> list[BusLine]:
    """Every line that any of these searches has, once."""
    lines: dict[str, BusLine] = {}
    for search in searches:
        for line in search.bus_lines:
            lines.setdefault(line.key, line)
    return list(lines.values())


def air_meters(lat: float, lon: float, stop: BusStop) -> float:
    scale = areas.METERS_PER_DEGREE_LONGITUDE_AT_EQUATOR * math.cos(math.radians(lat))
    return math.hypot((stop.lon - lon) * scale, (stop.lat - lat) * areas.METERS_PER_DEGREE_LATITUDE)


def candidate_stops(line: BusLine, lat: float, lon: float) -> list[BusStop]:
    """The stops of the line that a walk from the point is worth working out to."""
    near = sorted(
        (meters, index)
        for index, stop in enumerate(line.stops)
        if (meters := air_meters(lat, lon, stop)) <= MAX_STOP_METERS
    )
    return [line.stops[index] for _, index in near[:NEAREST_STOPS]]


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def known_walks(listing: Walkable) -> dict[str, int | None]:
    """The walks worked out from where the listing is now, in seconds by the stop's code.
    None is a stop that can't be walked to."""
    walks = getattr(listing, "walks", None)
    if not walks or walks.get("at") != [listing.latitude, listing.longitude]:
        return {}
    return walks.get("stops") or {}


def walk_to(line: BusLine, listing: Walkable) -> LineWalk | None:
    """The quickest walk from the listing to a stop of the line; None when it can't be told
    (the listing has no point on the map)."""
    lat, lon = listing.latitude, listing.longitude
    if lat is None or lon is None or not line.stops:
        return None
    known = known_walks(listing)
    # Far from every stop: the nearest one, to say how far
    stops = candidate_stops(line, lat, lon) or [
        min(line.stops, key=lambda stop: air_meters(lat, lon, stop))
    ]
    walks = []
    for stop in stops:
        direct = air_meters(lat, lon, stop) / WALK_METERS_PER_SECOND
        seconds = known.get(stop.code)
        if seconds is None:
            walks.append((direct * DETOUR, True, direct, stop))
        else:
            walks.append((seconds, False, seconds, stop))
    seconds, estimated, at_least, stop = min(walks, key=lambda walk: walk[0])
    return LineWalk(line, stop, _minutes(seconds), estimated, _minutes(at_least))


def walks_from(
    lines: Iterable[BusLine], listing: Walkable, copies: Iterable[Walkable] = ()
) -> list[LineWalk]:
    """The walk to each line, from the listing's map point or a copy's from another site."""
    placed = next(
        (
            candidate
            for candidate in (listing, *copies)
            if candidate.latitude is not None and candidate.longitude is not None
        ),
        None,
    )
    if placed is None:
        return []
    return [walk for line in lines if (walk := walk_to(line, placed)) is not None]
