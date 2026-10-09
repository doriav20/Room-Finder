"""The dashboard: listings, sources and settings pages.

Each user sees the listings through their own search and marks (favorites, hidden, notes);
the sources and the checking schedule are shared, and only admins change them.
"""

import asyncio
import csv
import hashlib
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import time, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from roomfinder import areas
from roomfinder.config import NOVNC_PAGE, Settings
from roomfinder.db import utcnow
from roomfinder.labels import area_note, furniture_label, parking_label
from roomfinder.matching import listing_matches
from roomfinder.models import (
    Listing,
    ListingMark,
    ListingStatus,
    PriceChange,
    SourceRun,
    SourceState,
    TelegramChat,
    User,
)
from roomfinder.normalize import CITY_ALIASES, normalize_city
from roomfinder.pipeline import mark, rematch_user
from roomfinder.runner import fetch_plan, get_source_state
from roomfinder.schemas import PROPERTY_TYPE_LABELS, SearchCriteria
from roomfinder.search import MAX_QUERY_LENGTH, TextSearch
from roomfinder.searchers import load_searchers
from roomfinder.settings_store import (
    Preferences,
    Schedule,
    get_schedule,
    save_schedule,
    set_user_criteria,
    set_user_preferences,
    user_criteria,
    user_preferences,
)
from roomfinder.sources.base import SourceError
from roomfinder.web import formatting
from roomfinder.web.security import require_admin, require_user

# Every page here needs a signed-in user (see web/accounts.py for signing in)
router = APIRouter(dependencies=[Depends(require_user)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC_DIR = Path(__file__).parent / "static"

PAGE_SIZE = 48
# The map shows a tab's listings all at once, up to this many
MAP_LIMIT = 1000
PRICE_DROP_WINDOW = timedelta(days=14)
ROOM_OPTIONS = [value / 2 for value in range(2, 17)]
TABS = [
    ("new", "חדשות"),
    ("all", "הכול"),
    ("favorite", "מועדפים"),
    ("contacted", "יצרתי קשר"),
    ("hidden", "לא רלוונטי"),
    ("removed", "הוסרו"),
]
TAB_STATUS = {
    "new": ListingStatus.NEW,
    "favorite": ListingStatus.FAVORITE,
    "contacted": ListingStatus.CONTACTED,
    "hidden": ListingStatus.HIDDEN,
}
SETTABLE_STATUSES = {
    ListingStatus.SEEN,
    ListingStatus.FAVORITE,
    ListingStatus.CONTACTED,
    ListingStatus.HIDDEN,
}


_static_versions: dict[str, tuple[int, str]] = {}


def static_url(path: str) -> str:
    # By path only: a full http:// address would be blocked on a page served over HTTPS
    # by a proxy in front (e.g. Tailscale), which talks plain HTTP to the dashboard.
    # The version changes with the file's content, so browsers can keep it for good
    # without a round trip on every page (main.py marks versioned files immutable)
    file = STATIC_DIR / path
    modified = file.stat().st_mtime_ns
    cached = _static_versions.get(path)
    if cached is None or cached[0] != modified:
        cached = (modified, hashlib.sha256(file.read_bytes()).hexdigest()[:12])
        _static_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


def configure_templates(settings: Settings) -> None:
    templates.env.globals["static_url"] = static_url
    filters = templates.env.filters
    filters["price"] = formatting.price
    filters["rooms"] = formatting.rooms
    filters["floor"] = formatting.floor
    filters["property_label"] = formatting.property_label
    filters["run_status"] = formatting.run_status
    filters["ago"] = formatting.time_ago
    filters["local_datetime"] = lambda moment: formatting.local_datetime(moment, settings.timezone)
    filters["furniture_label"] = furniture_label
    filters["parking_label"] = parking_label
    # Pages that render cards outside a search (e.g. after a status change) pass no search
    filters["highlight"] = lambda text, search=None: (search or TextSearch()).highlight(text)


# --- shared --------------------------------------------------------------------


async def render_page(
    request: Request, session: AsyncSession, name: str, context: dict[str, Any]
) -> HTMLResponse:
    """Render a full page, with the header status that every page shows."""
    user = request.state.user
    states = list(await session.scalars(select(SourceState)))
    my_telegram = await session.scalar(
        select(func.count()).select_from(TelegramChat).where(TelegramChat.account_id == user.id)
    )
    header = {
        "failing": sum(1 for state in states if state.enabled and state.consecutive_failures),
        "paused": user_preferences(user).notifications_paused,
        "telegram_configured": request.app.state.telegram is not None,
        "my_telegram": my_telegram,
    }
    base = {"header": header, "current_user": user, "source_labels": _source_labels(request)}
    return templates.TemplateResponse(request, name, base | context)


def render_partial(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    base = {"source_labels": _source_labels(request), "current_user": request.state.user}
    return templates.TemplateResponse(request, name, base | context)


def _source_labels(request: Request) -> dict[str, str]:
    return {name: source.label for name, source in request.app.state.sources.items()}


def _sessionmaker(request: Request):
    return request.app.state.sessionmaker


def _user_id(request: Request) -> int:
    return request.state.user.id


async def _me(request: Request, session: AsyncSession) -> User:
    """The signed-in user, in this session (to change their settings)."""
    return await session.get(User, _user_id(request))


def _known_source(request: Request, name: str) -> str:
    if name not in request.app.state.sources:
        raise HTTPException(404, "Unknown source")
    return name


# --- listings ------------------------------------------------------------------


@dataclass
class ListingFilters:
    tab: str = ""
    # The sites to show; none means all of them
    sources: list[str] = field(default_factory=list)
    area: str = ""
    price_max: int | None = None
    # "yes" / "no" / "" - unlike the search criteria, these show known values only
    furnished: str = ""
    parking: str = ""
    # Free text in the listing's own words (see search.py)
    q: str = ""
    sort: str = "newest"
    all: bool = False
    # "map" shows the listings on a map instead of as cards
    view: str = ""

    @classmethod
    def from_request(cls, request: Request) -> "ListingFilters":
        params = request.query_params
        price_max = params.get("price_max", "")
        tab = params.get("tab", "")
        sort = params.get("sort", "newest")
        return cls(
            tab=tab if tab in dict(TABS) else "",
            sources=list(dict.fromkeys(params.getlist("source"))),
            area=params.get("area", ""),
            price_max=int(price_max) if price_max.isdigit() else None,
            furnished=params.get("furnished", "")
            if params.get("furnished") in ("yes", "no")
            else "",
            parking=params.get("parking", "") if params.get("parking") in ("yes", "no") else "",
            q=params.get("q", "").strip()[:MAX_QUERY_LENGTH],
            sort=sort if sort in {"newest", "price_asc", "price_desc"} else "newest",
            all=params.get("all") == "1",
            view="map" if params.get("view") == "map" else "",
        )

    def query(self, **overrides: Any) -> str:
        values = {
            "tab": self.tab,
            "source": self.sources,
            "area": self.area,
            "price_max": self.price_max,
            "furnished": self.furnished,
            "parking": self.parking,
            "q": self.q,
            "sort": self.sort if self.sort != "newest" else None,
            "all": "1" if self.all else None,
            "view": self.view,
        } | overrides
        return urlencode({key: value for key, value in values.items() if value}, doseq=True)

    @property
    def active_count(self) -> int:
        """How many of the filters under the search box are set (the phone layout folds them)"""
        values = (self.sources, self.area, self.price_max, self.furnished, self.parking, self.all)
        return sum(1 for value in values if value) + (self.sort != "newest")

    @cached_property
    def search(self) -> TextSearch:
        return TextSearch.parse(self.q)


def _with_my_mark(user_id: int) -> Select[tuple[Listing]]:
    """Listings, each with the user's own mark joined (no mark reads as: seen, doesn't fit)."""
    return select(Listing).outerjoin(
        ListingMark, and_(ListingMark.listing_id == Listing.id, ListingMark.user_id == user_id)
    )


def _listings_query(filters: ListingFilters, tab: str, user_id: int) -> Select[tuple[Listing]]:
    query = _with_my_mark(user_id).where(Listing.duplicate_of_id.is_(None))
    if tab == "removed":
        query = query.where(Listing.removed_at.is_not(None))
    else:
        query = query.where(Listing.removed_at.is_(None))
    my_status = func.coalesce(ListingMark.status, ListingStatus.SEEN)
    if tab in TAB_STATUS:
        query = query.where(my_status == TAB_STATUS[tab])
    else:
        query = query.where(my_status != ListingStatus.HIDDEN)
    # Listings the user acted on stay visible even if they no longer fit the search
    if not filters.all and tab not in ("favorite", "contacted", "hidden"):
        query = query.where(ListingMark.matches.is_(True))
    if filters.sources:
        # A listing is also on a site when one of its duplicates was found there
        copy = aliased(Listing)
        on_a_chosen_site = (
            select(copy.id)
            .where(
                copy.duplicate_of_id == Listing.id,
                copy.removed_at.is_(None),
                copy.source.in_(filters.sources),
            )
            .exists()
        )
        query = query.where(or_(Listing.source.in_(filters.sources), on_a_chosen_site))
    if filters.area:
        query = query.where(Listing.area == filters.area)
    if filters.price_max:
        query = query.where(or_(Listing.price.is_(None), Listing.price <= filters.price_max))
    if filters.furnished:
        query = query.where(Listing.furnished.is_(filters.furnished == "yes"))
    if filters.parking:
        query = query.where(Listing.parking.is_(filters.parking == "yes"))
    if filters.search:
        query = query.where(*filters.search.conditions())
    return query


def _ordered(query: Select[tuple[Listing]], sort: str) -> Select[tuple[Listing]]:
    if sort == "price_asc":
        return query.order_by(Listing.price.is_(None), Listing.price.asc(), Listing.id.desc())
    if sort == "price_desc":
        return query.order_by(Listing.price.is_(None), Listing.price.desc(), Listing.id.desc())
    return query.order_by(Listing.first_seen_at.desc(), Listing.id.desc())


def _status_of(listing: Listing, user_id: int) -> str:
    found = listing.marks.get(user_id)
    return found.status if found else ListingStatus.SEEN


@dataclass
class Card:
    listing: Listing
    address: str
    duplicates: list[Listing] = field(default_factory=list)
    # The previous price, when it dropped recently
    price_drop: int | None = None
    area_note: str | None = None
    # The signed-in user's own mark on the listing (none yet: seen, doesn't fit their search)
    mark: ListingMark | None = None

    @property
    def status(self) -> str:
        return self.mark.status if self.mark else ListingStatus.SEEN

    @property
    def matches(self) -> bool:
        return bool(self.mark and self.mark.matches)

    @property
    def notes(self) -> str | None:
        return self.mark.notes if self.mark else None


async def _cards(session: AsyncSession, listings: list[Listing], user_id: int) -> list[Card]:
    ids = [listing.id for listing in listings]
    duplicates: defaultdict[int, list[Listing]] = defaultdict(list)
    drops: dict[int, int] = {}
    if ids:
        for duplicate in await session.scalars(
            select(Listing).where(Listing.duplicate_of_id.in_(ids), Listing.removed_at.is_(None))
        ):
            duplicates[duplicate.duplicate_of_id].append(duplicate)
        for change in await session.scalars(
            select(PriceChange)
            .where(
                PriceChange.listing_id.in_(ids),
                PriceChange.seen_at >= utcnow() - PRICE_DROP_WINDOW,
            )
            .order_by(PriceChange.seen_at)
        ):
            if change.old_price and change.new_price and change.new_price < change.old_price:
                drops[change.listing_id] = drops.get(change.listing_id, change.old_price)
            else:
                drops.pop(change.listing_id, None)
    return [
        Card(
            listing,
            formatting.address(listing),
            duplicates[listing.id],
            drops.get(listing.id),
            area_note(listing),
            listing.marks.get(user_id),
        )
        for listing in listings
    ]


async def _page_of_cards(
    session: AsyncSession, filters: ListingFilters, tab: str, offset: int, user_id: int
) -> tuple[list[Card], int | None]:
    query = _ordered(_listings_query(filters, tab, user_id), filters.sort)
    listings = list(await session.scalars(query.offset(offset).limit(PAGE_SIZE + 1)))
    next_offset = offset + PAGE_SIZE if len(listings) > PAGE_SIZE else None
    return await _cards(session, listings[:PAGE_SIZE], user_id), next_offset


async def _count(session: AsyncSession, query: Select[Any]) -> int:
    return await session.scalar(select(func.count()).select_from(query.subquery())) or 0


def _criteria_summary(criteria: SearchCriteria) -> str:
    parts = [criteria.city, _rooms_summary(criteria)]
    if criteria.price_max:
        parts.append(f"עד {criteria.price_max:,} ₪")
    if criteria.areas:
        parts.append(_areas_summary(criteria))
    return " · ".join(parts)


def _rooms_summary(criteria: SearchCriteria) -> str:
    if criteria.rooms_min == criteria.rooms_max:
        return f"{formatting.rooms(criteria.rooms_min)} חדרים"
    return f"{formatting.rooms(criteria.rooms_min)}-{formatting.rooms(criteria.rooms_max)} חדרים"


def _areas_summary(criteria: SearchCriteria) -> str:
    if not criteria.areas:
        return "כל העיר"
    if len(criteria.areas) == 1:
        return f"שכונת {criteria.areas[0]}"
    return f"{len(criteria.areas)} שכונות"


async def _known_areas(session: AsyncSession) -> list[str]:
    rows = await session.scalars(
        select(Listing.area).where(Listing.area.is_not(None)).group_by(Listing.area)
    )
    return sorted(rows)


@router.get("/", response_class=HTMLResponse)
async def listings_page(request: Request) -> HTMLResponse:
    filters = ListingFilters.from_request(request)
    user_id = _user_id(request)
    async with _sessionmaker(request)() as session:
        counts = {
            key: await _count(session, _listings_query(filters, key, user_id)) for key, _ in TABS
        }
        tab = filters.tab or ("new" if counts["new"] else "all")
        # The map fetches its listings itself (listings_map below)
        cards, next_offset = (
            ([], None)
            if filters.view == "map"
            else await _page_of_cards(session, filters, tab, 0, user_id)
        )
        # Listings whose ad text wasn't read yet: a search sees only their title and address
        without_text = 0
        if filters.search:
            unsearched = _listings_query(replace(filters, q=""), tab, user_id)
            without_text = await _count(session, unsearched.where(Listing.description.is_(None)))
        context = {
            "active": "listings",
            "search": filters.search,
            "without_text": without_text,
            "tab": tab,
            "tabs": [(key, label, counts[key]) for key, label in TABS],
            "tab_query": lambda key: filters.query(tab=key),
            "filters": filters,
            "cards": cards,
            "next_offset": next_offset,
            "page_query": filters.query(tab=tab),
            "view_query": lambda view: filters.query(tab=tab, view=view),
            "areas": await _known_areas(session),
            "latest_id": await session.scalar(select(func.max(Listing.id))) or 0,
            "criteria_summary": _criteria_summary(user_criteria(request.state.user)),
        }
        return await render_page(request, session, "listings.html", context)


@router.get("/listings/cards", response_class=HTMLResponse)
async def more_cards(request: Request, offset: int = 0) -> HTMLResponse:
    filters = ListingFilters.from_request(request)
    tab = filters.tab or "all"
    async with _sessionmaker(request)() as session:
        cards, next_offset = await _page_of_cards(
            session, filters, tab, max(offset, 0), _user_id(request)
        )
    context = {
        "cards": cards,
        "next_offset": next_offset,
        "page_query": filters.query(tab=tab),
        "search": filters.search,
    }
    return render_partial(request, "partials/listing_cards.html", context)


def _map_point(listing: Listing, copies: list[Listing]) -> tuple[float, float, bool] | None:
    """Where to put a listing on the map: its own point, a copy's from another site, or
    (approximately) the middle of its neighborhood. The flag says it's approximate."""
    for candidate in (listing, *copies):
        if candidate.latitude is not None and candidate.longitude is not None:
            return candidate.latitude, candidate.longitude, False
    city_map = areas.city_map(listing.city)
    if listing.area and city_map:
        for area in city_map.areas:
            if area.name == listing.area:
                return *area.label_point, True
    return None


@router.get("/api/listings/map")
async def listings_map(request: Request) -> JSONResponse:
    """A tab's listings with where they are, for the map view of the listings page."""
    filters = ListingFilters.from_request(request)
    tab = filters.tab or "all"
    labels = _source_labels(request)
    user_id = _user_id(request)
    async with _sessionmaker(request)() as session:
        query = _ordered(_listings_query(filters, tab, user_id), filters.sort)
        listings = list(await session.scalars(query.limit(MAP_LIMIT)))
        copies: defaultdict[int, list[Listing]] = defaultdict(list)
        if listings:
            for copy in await session.scalars(
                select(Listing).where(
                    Listing.duplicate_of_id.in_([listing.id for listing in listings])
                )
            ):
                copies[copy.duplicate_of_id].append(copy)
    criteria = user_criteria(request.state.user)
    points = []
    without_location = 0
    for listing in listings:
        point = _map_point(listing, copies[listing.id])
        if point is None:
            without_location += 1
            continue
        lat, lng, approximate = point
        points.append(
            {
                "id": listing.id,
                "lat": lat,
                "lng": lng,
                "approximate": approximate,
                "price": listing.price,
                "rooms": listing.rooms,
                "sqm": listing.sqm,
                "address": formatting.address(listing),
                "area": listing.area,
                "image": listing.images[0] if listing.images else None,
                "url": listing.url,
                "status": _status_of(listing, user_id),
                "source": labels.get(listing.source, listing.source),
            }
        )
    city_map = areas.city_map(criteria.city)
    outlines = []
    bounds = None
    if city_map:
        west, south, east, north = city_map.bounds
        bounds = [[south, west], [north, east]]
        outlines = [
            {"name": area.name, "outline": [[lat, lon] for lon, lat in area.ring]}
            for area in city_map.areas
            if area.name in criteria.areas
        ]
    return JSONResponse(
        {
            "listings": points,
            "without_location": without_location,
            "areas": outlines,
            "bounds": bounds,
        }
    )


@router.post("/listings/{listing_id}/status", response_class=HTMLResponse)
async def set_listing_status(request: Request, listing_id: int) -> HTMLResponse:
    wanted = (await request.form()).get("status")
    if wanted not in SETTABLE_STATUSES:
        raise HTTPException(400, "Unknown status")
    user_id = _user_id(request)
    async with _sessionmaker(request)() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404)
        found = mark(listing, user_id)
        # Pressing the same button again undoes it
        found.status = ListingStatus.SEEN if found.status == wanted else wanted
        await session.commit()
        [card] = await _cards(session, [listing], user_id)
    return render_partial(request, "partials/listing_card.html", {"card": card})


@router.post("/listings/{listing_id}/notes")
async def set_listing_notes(request: Request, listing_id: int) -> Response:
    notes = str((await request.form()).get("notes", "")).strip()
    async with _sessionmaker(request)() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404)
        mark(listing, _user_id(request)).notes = notes or None
        await session.commit()
    return Response(status_code=204)


@router.post("/listings/mark-seen")
async def mark_new_listings_seen(request: Request) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await session.execute(
            update(ListingMark)
            .where(
                ListingMark.user_id == _user_id(request),
                ListingMark.status == ListingStatus.NEW,
            )
            .values(status=ListingStatus.SEEN)
        )
        await session.commit()
    return RedirectResponse("/?tab=all", status_code=303)


@router.get("/api/updates")
async def listing_updates(request: Request, since_id: int = 0) -> JSONResponse:
    """New listings that fit the user's search since the page loaded, for browser notifications."""
    labels = _source_labels(request)
    async with _sessionmaker(request)() as session:
        listings = await session.scalars(
            _with_my_mark(_user_id(request))
            .where(
                Listing.id > since_id,
                ListingMark.status == ListingStatus.NEW,
                ListingMark.matches.is_(True),
                Listing.duplicate_of_id.is_(None),
            )
            .order_by(Listing.id)
        )
        items = [
            {
                "id": listing.id,
                "url": listing.url,
                "summary": formatting.listing_summary(listing, labels.get(listing.source, "")),
            }
            for listing in listings
        ]
        latest_id = await session.scalar(select(func.max(Listing.id))) or 0
    return JSONResponse({"latest_id": max(latest_id, since_id), "listings": items})


def _spreadsheet_safe(*values: Any) -> list[Any]:
    """Ad texts come from strangers: a cell starting with = + - @ would run as an Excel formula."""
    return [
        f"'{value}"
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r")
        else value
        for value in values
    ]


@router.get("/export.csv")
async def export_csv(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    labels = _source_labels(request)
    user_id = _user_id(request)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "אתר", "סטטוס", "מתאימה", "מחיר", "חדרים", "מ״ר", "קומה", "כתובת", "שכונה (מפה)", "עיר",
            "פרטי/תיווך", "סוג נכס", "ריהוט", "חניה", "נמצאה", "נראתה לאחרונה", "הוסרה",
            "הערות", "תיאור", "קישור",
        ]
    )  # fmt: skip
    async with _sessionmaker(request)() as session:
        for listing in await session.scalars(select(Listing).order_by(Listing.first_seen_at)):
            found = listing.marks.get(user_id)
            writer.writerow(
                _spreadsheet_safe(
                    labels.get(listing.source, listing.source),
                    found.status if found else ListingStatus.SEEN,
                    "כן" if found and found.matches else "לא",
                    listing.price or "",
                    formatting.rooms(listing.rooms),
                    listing.sqm or "",
                    "" if listing.floor is None else listing.floor,
                    formatting.address(listing),
                    listing.area or "",
                    listing.city or "",
                    {True: "תיווך", False: "פרטי"}.get(listing.is_agency, ""),
                    formatting.property_label(listing.property_type),
                    furniture_label(listing) or "",
                    parking_label(listing) or "",
                    formatting.local_datetime(listing.first_seen_at, settings.timezone),
                    formatting.local_datetime(listing.last_seen_at, settings.timezone),
                    formatting.local_datetime(listing.removed_at, settings.timezone),
                    (found.notes if found else None) or "",
                    listing.description or "",
                    listing.url,
                )
            )
    # The BOM makes Excel read the Hebrew correctly
    return Response(
        "﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="roomfinder.csv"'},
    )


# --- sources -------------------------------------------------------------------


@dataclass
class SourceRow:
    name: str
    label: str
    state: SourceState
    running: bool
    # The site's search page with the user's own search as its filters (or the custom URL)
    search_url: str | None
    default_interval: int
    active: int
    matching: int


async def _source_row(request: Request, session: AsyncSession, name: str) -> SourceRow:
    source = request.app.state.sources[_known_source(request, name)]
    state = await get_source_state(session, name)
    listings = select(Listing).where(Listing.source == name, Listing.removed_at.is_(None))
    try:
        search_url = state.custom_url or source.search_url(user_criteria(request.state.user))
    except SourceError:
        search_url = None
    mine = _with_my_mark(_user_id(request)).where(
        Listing.source == name,
        Listing.removed_at.is_(None),
        Listing.duplicate_of_id.is_(None),
        ListingMark.matches.is_(True),
    )
    return SourceRow(
        name=name,
        label=source.label,
        state=state,
        running=request.app.state.runner.is_running(name),
        search_url=search_url,
        default_interval=source.default_interval_minutes,
        active=await _count(session, listings),
        matching=await _count(session, mine),
    )


async def _render_row(request: Request, name: str) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        row = await _source_row(request, session, name)
        await session.commit()
    return render_partial(request, "partials/source_row.html", {"row": row})


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request) -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        rows = [await _source_row(request, session, name) for name in request.app.state.sources]
        await session.commit()
        runs = list(
            await session.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(25))
        )
        searchers = await load_searchers(session)
        context = {
            "active": "sources",
            "rows": rows,
            "runs": runs,
            "browser_version": await request.app.state.browser.version(),
            "novnc_page": NOVNC_PAGE,
            # What the sites are read with: everyone's searches joined (searchers.py)
            "combined": [
                f"{search.city} · {_rooms_summary(search)} · {_price_range(search)}"
                for search in fetch_plan(searchers, None)
            ],
            "searching_users": sum(1 for searcher in searchers if searcher.active),
        }
        return await render_page(request, session, "sources.html", context)


def _price_range(criteria: SearchCriteria) -> str:
    if criteria.price_min and criteria.price_max:
        return f"{criteria.price_min:,}-{criteria.price_max:,} ₪"
    if criteria.price_max:
        return f"עד {criteria.price_max:,} ₪"
    if criteria.price_min:
        return f"מ-{criteria.price_min:,} ₪"
    return "כל המחירים"


@router.get("/sources/{name}/row", response_class=HTMLResponse)
async def source_row(request: Request, name: str) -> HTMLResponse:
    return await _render_row(request, name)


# The sources are shared by everyone, so only admins run them, turn them off or change them


@router.post(
    "/sources/{name}/run", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def run_source_now(request: Request, name: str) -> HTMLResponse:
    _known_source(request, name)
    task = asyncio.create_task(request.app.state.runner.run(name))
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
    await asyncio.sleep(0)  # let the run start, so the row shows it as running
    return await _render_row(request, name)


@router.post(
    "/sources/{name}/toggle", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def toggle_source(request: Request, name: str) -> HTMLResponse:
    _known_source(request, name)
    async with _sessionmaker(request)() as session:
        state = await get_source_state(session, name)
        state.enabled = not state.enabled
        await session.commit()
    return await _render_row(request, name)


@router.post(
    "/sources/{name}/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def source_settings(request: Request, name: str) -> HTMLResponse:
    _known_source(request, name)
    form = await request.form()
    interval = str(form.get("interval_minutes", "")).strip()
    custom_url = str(form.get("custom_url", "")).strip()
    if interval and not (interval.isdigit() and 5 <= int(interval) <= 240):
        raise HTTPException(400, "Interval must be 5-240 minutes")
    if custom_url and not custom_url.startswith(("https://", "http://")):
        raise HTTPException(400, "The custom URL must be a web address")
    async with _sessionmaker(request)() as session:
        state = await get_source_state(session, name)
        state.interval_minutes = int(interval) if interval else None
        state.custom_url = custom_url or None
        await session.commit()
    return await _render_row(request, name)


@router.post("/browser/restart")
async def restart_browser(request: Request) -> RedirectResponse:
    await request.app.state.browser.restart()
    return RedirectResponse("/sources", status_code=303)


# --- neighborhoods -------------------------------------------------------------


async def _area_counts(
    session: AsyncSession, criteria: SearchCriteria, user_id: int
) -> tuple[Counter[str], int]:
    """Current listings that fit everything in the user's search but the neighborhoods, per
    neighborhood, and how many of them can't be placed in one."""
    anywhere = criteria.model_copy(update={"areas": []})
    counts: Counter[str] = Counter()
    unknown = 0
    query = select(Listing).where(
        Listing.removed_at.is_(None),
        Listing.duplicate_of_id.is_(None),
    )
    for listing in await session.scalars(query):
        hidden = _status_of(listing, user_id) == ListingStatus.HIDDEN
        if hidden or not listing_matches(listing, anywhere):
            continue
        if listing.area:
            counts[listing.area] += 1
        else:
            unknown += 1
    return counts, unknown


def _map_data(city_map: areas.CityMap, counts: Counter[str]) -> dict[str, Any]:
    west, south, east, north = city_map.bounds
    return {
        "bounds": [[south, west], [north, east]],
        "areas": [
            {
                "name": area.name,
                "outline": [[lat, lon] for lon, lat in area.ring],
                "label": list(area.label_point),
                "count": counts[area.name],
            }
            for area in city_map.areas
        ],
    }


@router.get("/areas", response_class=HTMLResponse)
async def areas_page(request: Request, saved: str = "") -> HTMLResponse:
    criteria = user_criteria(request.state.user)
    async with _sessionmaker(request)() as session:
        city_map = areas.city_map(criteria.city)
        counts, unknown = await _area_counts(session, criteria, _user_id(request))
        context = {
            "active": "areas",
            "saved": saved == "1",
            "criteria": criteria,
            "city_map": city_map,
            "counts": counts,
            "unknown_count": unknown,
            "map_data": _map_data(city_map, counts) if city_map else None,
        }
        return await render_page(request, session, "areas.html", context)


@router.post("/areas")
async def save_areas(request: Request) -> RedirectResponse:
    form = await request.form()
    chosen = set(form.getlist("areas"))
    async with _sessionmaker(request)() as session:
        me = await _me(request, session)
        criteria = user_criteria(me)
        city_map = areas.city_map(criteria.city)
        if city_map is None or not chosen <= city_map.names:
            raise HTTPException(400, "Unknown neighborhood")
        criteria = criteria.model_copy(
            update={
                # In the map's order, so the saved list reads the same every time
                "areas": [area.name for area in city_map.areas if area.name in chosen],
                "unknown_area_ok": form.get("unknown_area_ok") == "1",
            }
        )
        set_user_criteria(me, criteria)
        await rematch_user(session, me.id, criteria)
        await session.commit()
    return RedirectResponse("/areas?saved=1", status_code=303)


# --- settings ------------------------------------------------------------------

SAVED_MESSAGES = {
    "criteria": "החיפוש שלך נשמר. ההתאמה של המודעות לחיפוש שלך חושבה מחדש.",
    "preferences": "ההגדרות שלך נשמרו.",
    "schedule": "שעות הבדיקה נשמרו.",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = "") -> HTMLResponse:
    user = request.state.user
    criteria = user_criteria(user)
    async with _sessionmaker(request)() as session:
        context = {
            "active": "settings",
            "saved": SAVED_MESSAGES.get(saved),
            "criteria": criteria,
            "areas_summary": _areas_summary(criteria),
            "has_area_map": areas.city_map(criteria.city) is not None,
            "preferences": user_preferences(user),
            "schedule": await get_schedule(session),
            "cities": list(CITY_ALIASES),
            "room_options": ROOM_OPTIONS,
            "property_types": [(kind.value, label) for kind, label in PROPERTY_TYPE_LABELS.items()],
        }
        return await render_page(request, session, "settings.html", context)


def _optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


@router.post("/settings/criteria")
async def save_search_criteria(request: Request) -> RedirectResponse:
    form = await request.form()
    rooms = sorted([float(form.get("rooms_min", 4)), float(form.get("rooms_max", 4))])
    city = str(form.get("city", ""))
    # Only the cities every site can be searched in; the sites are read for everyone at once
    if city not in CITY_ALIASES:
        raise HTTPException(400, "Unknown city")
    async with _sessionmaker(request)() as session:
        me = await _me(request, session)
        current = user_criteria(me)
        # The neighborhoods come from the map page, and belong to the city they were chosen in
        same_city = normalize_city(city) == normalize_city(current.city)
        try:
            criteria = SearchCriteria(
                city=city,
                rooms_min=rooms[0],
                rooms_max=rooms[1],
                price_min=_optional_int(form.get("price_min")),
                price_max=_optional_int(form.get("price_max")),
                sqm_min=_optional_int(form.get("sqm_min")),
                property_types=form.getlist("property_types"),
                private_only=form.get("private_only") == "1",
                furnished=str(form.get("furnished") or "any"),
                parking_required=form.get("parking_required") == "1",
                areas=current.areas if same_city else [],
                unknown_area_ok=current.unknown_area_ok,
                exclude_keywords=[
                    line.strip()
                    for line in str(form.get("exclude_keywords", "")).splitlines()
                    if line.strip()
                ],
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        set_user_criteria(me, criteria)
        await rematch_user(session, me.id, criteria)
        await session.commit()
    return RedirectResponse("/settings?saved=criteria", status_code=303)


def _time(value: Any, default: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return default


@router.post("/settings/preferences")
async def save_notification_preferences(request: Request) -> RedirectResponse:
    form = await request.form()
    defaults = Preferences()
    preferences = Preferences(
        notifications_paused=form.get("notifications_paused") == "1",
        notify_price_drops=form.get("notify_price_drops") == "1",
        quiet_hours_start=_time(form.get("quiet_hours_start"), defaults.quiet_hours_start),
        quiet_hours_end=_time(form.get("quiet_hours_end"), defaults.quiet_hours_end),
    )
    async with _sessionmaker(request)() as session:
        set_user_preferences(await _me(request, session), preferences)
        await session.commit()
    return RedirectResponse("/settings?saved=preferences", status_code=303)


@router.post("/settings/schedule", dependencies=[Depends(require_admin)])
async def save_checking_schedule(request: Request) -> RedirectResponse:
    form = await request.form()
    defaults = Schedule()
    schedule = Schedule(
        night_start=_time(form.get("night_start"), defaults.night_start),
        night_end=_time(form.get("night_end"), defaults.night_end),
    )
    async with _sessionmaker(request)() as session:
        await save_schedule(session, schedule)
        await session.commit()
    return RedirectResponse("/settings?saved=schedule", status_code=303)
