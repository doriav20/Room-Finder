"""Walks on the streets from listings to bus stops, in seconds.

Worked out by a routing server for walkers (OSRM, on OpenStreetMap's streets) that runs next
to the app (the "routing" service in compose.yaml), and kept on the listing (Listing.walks).
The public OSRM servers are for demonstrations and block whoever asks about more than a few
listings. While the server isn't there, or is still preparing its map, the walks stay
estimates (transit.walk_to) until a later try.
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from roomfinder import transit
from roomfinder.models import Listing
from roomfinder.schemas import BusLine, BusStop

log = logging.getLogger(__name__)

# One request: up to this many listings, and no more points (listings and stops) than an
# OSRM server takes in a table by default
GROUP_LISTINGS = 20
MAX_POINTS = 100
# The server is down or refusing; the rest can wait for the next time
MAX_FAILURES = 2

Point = tuple[float, float]


class WalkingError(Exception):
    """The routing server didn't work out the walks."""


@dataclass(frozen=True)
class _Wanted:
    listing_id: int
    # (latitude, longitude)
    point: Point
    stops: list[BusStop]


async def walk_table(
    client: httpx.AsyncClient, routing_url: str, points: Sequence[Point], stops: Sequence[BusStop]
) -> list[dict[str, int | None]]:
    """The walk from each (latitude, longitude) point to each stop, by the stop's code; None
    where there's no way."""
    places = [*((lon, lat) for lat, lon in points), *((stop.lon, stop.lat) for stop in stops)]
    path = ";".join(f"{x:.6f},{y:.6f}" for x, y in places)
    params = {
        "sources": ";".join(str(index) for index in range(len(points))),
        "destinations": ";".join(str(len(points) + index) for index in range(len(stops))),
        "annotations": "duration",
    }
    try:
        url = f"{routing_url.rstrip('/')}/table/v1/foot/{path}"
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "Ok":
            raise WalkingError(str(data.get("code")))
        rows = data["durations"]
    except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
        raise WalkingError(f"{type(exc).__name__}: {str(exc).splitlines()[0]}") from exc
    if len(rows) != len(points) or any(len(row) != len(stops) for row in rows):
        raise WalkingError("Unexpected answer from the routing server")
    return [
        {
            stop.code: None if seconds is None else round(seconds)
            for stop, seconds in zip(stops, row, strict=True)
        }
        for row in rows
    ]


def missing_stops(listing: Listing, lines: Sequence[BusLine]) -> list[BusStop]:
    """The stops near the listing that the walk to wasn't worked out yet."""
    if listing.latitude is None or listing.longitude is None:
        return []
    known = transit.known_walks(listing)
    stops: dict[str, BusStop] = {}
    for line in lines:
        for stop in transit.candidate_stops(line, listing.latitude, listing.longitude):
            if stop.code not in known:
                stops.setdefault(stop.code, stop)
    return list(stops.values())


async def pending(
    session: AsyncSession, lines: Sequence[BusLine], source: str | None = None
) -> list[int]:
    """The listings still up (of one source, or of all) that lack walks to these lines,
    newest first."""
    if not lines:
        return []
    query = select(Listing).where(
        Listing.removed_at.is_(None),
        Listing.latitude.is_not(None),
        Listing.longitude.is_not(None),
    )
    if source is not None:
        query = query.where(Listing.source == source)
    listings = await session.scalars(query.order_by(Listing.first_seen_at.desc()))
    return [listing.id for listing in listings if missing_stops(listing, lines)]


def _groups(wanted: Sequence[_Wanted]) -> Iterator[tuple[list[_Wanted], list[BusStop]]]:
    """The listings in groups that fit a request each, with the stops of each group."""
    group: list[_Wanted] = []
    stops: dict[str, BusStop] = {}
    for item in wanted:
        added = {stop.code: stop for stop in item.stops if stop.code not in stops}
        full = len(group) >= GROUP_LISTINGS or len(group) + 1 + len(stops | added) > MAX_POINTS
        if group and full:
            yield group, list(stops.values())
            group, stops = [], {}
            added = {stop.code: stop for stop in item.stops}
        group.append(item)
        stops |= added
    if group:
        yield group, list(stops.values())


async def fill_walks(
    sessionmaker: async_sessionmaker[AsyncSession],
    listing_ids: Sequence[int],
    lines: Sequence[BusLine],
    routing_url: str,
) -> list[int]:
    """Work out and store the walks these listings lack. Returns the listings that got some.

    routing_url is the OSRM server to ask; without one nothing is worked out."""
    filled: list[int] = []
    if not lines or not listing_ids or not routing_url:
        return filled
    wanted: list[_Wanted] = []
    async with sessionmaker() as session:
        for listing_id in listing_ids:
            listing = await session.get(Listing, listing_id)
            if listing is not None and (stops := missing_stops(listing, lines)):
                wanted.append(_Wanted(listing.id, (listing.latitude, listing.longitude), stops))
    failures = 0
    # No session is held while the server is asked: it can take a while
    async with transit.new_client() as http:
        for group, stops in _groups(wanted):
            try:
                table = await walk_table(http, routing_url, [item.point for item in group], stops)
            except WalkingError as exc:
                failures += 1
                log.warning("Working out the walks from %d listings failed: %s", len(group), exc)
                if failures >= MAX_FAILURES:
                    break
                continue
            async with sessionmaker() as session:
                for item, seconds in zip(group, table, strict=True):
                    listing = await session.get(Listing, item.listing_id)
                    # The listing may have moved, or got other walks, in the meantime
                    if listing is None or (listing.latitude, listing.longitude) != item.point:
                        continue
                    own = {stop.code: seconds[stop.code] for stop in item.stops}
                    listing.walks = {
                        "at": list(item.point),
                        "stops": transit.known_walks(listing) | own,
                    }
                    filled.append(item.listing_id)
                await session.commit()
    if wanted:
        log.info(
            "Worked out the walks to bus stops from %d of %d listings", len(filled), len(wanted)
        )
    return filled
