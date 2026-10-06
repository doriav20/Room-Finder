"""The dashboard: listings, sources and settings pages."""

import asyncio
import csv
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
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from roomfinder import areas
from roomfinder.config import NOVNC_PAGE, Settings
from roomfinder.db import utcnow
from roomfinder.labels import area_note, furniture_label, parking_label
from roomfinder.matching import listing_matches
from roomfinder.models import (
    Listing,
    ListingStatus,
    PriceChange,
    SourceRun,
    SourceState,
    TelegramChat,
)
from roomfinder.normalize import CITY_ALIASES, normalize_city
from roomfinder.pipeline import rematch_all
from roomfinder.runner import get_source_state
from roomfinder.schemas import PROPERTY_TYPE_LABELS, SearchCriteria
from roomfinder.search import MAX_QUERY_LENGTH, TextSearch
from roomfinder.settings_store import (
    Preferences,
    get_criteria,
    get_preferences,
    save_criteria,
    save_preferences,
)
from roomfinder.sources.base import SourceError
from roomfinder.web import formatting
from roomfinder.web.security import require_user

# Every page here needs a signed-in user (see web/accounts.py for signing in)
router = APIRouter(dependencies=[Depends(require_user)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PAGE_SIZE = 48
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


def static_url(path: str) -> str:
    # By path only: a full http:// address would be blocked on a page served over HTTPS
    # by a proxy in front (e.g. Tailscale), which talks plain HTTP to the dashboard
    return f"/static/{path}"


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
    preferences = await get_preferences(session)
    my_telegram = await session.scalar(
        select(func.count()).select_from(TelegramChat).where(TelegramChat.account_id == user.id)
    )
    header = {
        "failing": sum(1 for state in states if state.enabled and state.consecutive_failures),
        "paused": preferences.notifications_paused,
        "telegram_configured": request.app.state.telegram is not None,
        "my_telegram": my_telegram,
    }
    base = {"header": header, "current_user": user, "source_labels": _source_labels(request)}
    return templates.TemplateResponse(request, name, base | context)


def render_partial(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {"source_labels": _source_labels(request)} | context
    )


def _source_labels(request: Request) -> dict[str, str]:
    return {name: source.label for name, source in request.app.state.sources.items()}


def _sessionmaker(request: Request):
    return request.app.state.sessionmaker


def _known_source(request: Request, name: str) -> str:
    if name not in request.app.state.sources:
        raise HTTPException(404, "Unknown source")
    return name


# --- listings ------------------------------------------------------------------


@dataclass
class ListingFilters:
    tab: str = ""
    source: str = ""
    area: str = ""
    price_max: int | None = None
    # "yes" / "no" / "" - unlike the search criteria, these show known values only
    furnished: str = ""
    parking: str = ""
    # Free text in the listing's own words (see search.py)
    q: str = ""
    sort: str = "newest"
    all: bool = False

    @classmethod
    def from_request(cls, request: Request) -> "ListingFilters":
        params = request.query_params
        price_max = params.get("price_max", "")
        tab = params.get("tab", "")
        sort = params.get("sort", "newest")
        return cls(
            tab=tab if tab in dict(TABS) else "",
            source=params.get("source", ""),
            area=params.get("area", ""),
            price_max=int(price_max) if price_max.isdigit() else None,
            furnished=params.get("furnished", "")
            if params.get("furnished") in ("yes", "no")
            else "",
            parking=params.get("parking", "") if params.get("parking") in ("yes", "no") else "",
            q=params.get("q", "").strip()[:MAX_QUERY_LENGTH],
            sort=sort if sort in {"newest", "price_asc", "price_desc"} else "newest",
            all=params.get("all") == "1",
        )

    def query(self, **overrides: Any) -> str:
        values = {
            "tab": self.tab,
            "source": self.source,
            "area": self.area,
            "price_max": self.price_max,
            "furnished": self.furnished,
            "parking": self.parking,
            "q": self.q,
            "sort": self.sort if self.sort != "newest" else None,
            "all": "1" if self.all else None,
        } | overrides
        return urlencode({key: value for key, value in values.items() if value})

    @property
    def active_count(self) -> int:
        """How many of the filters under the search box are set (the phone layout folds them)"""
        values = (self.source, self.area, self.price_max, self.furnished, self.parking, self.all)
        return sum(1 for value in values if value) + (self.sort != "newest")

    @cached_property
    def search(self) -> TextSearch:
        return TextSearch.parse(self.q)


def _listings_query(filters: ListingFilters, tab: str) -> Select[tuple[Listing]]:
    query = select(Listing).where(Listing.duplicate_of_id.is_(None))
    if tab == "removed":
        query = query.where(Listing.removed_at.is_not(None))
    else:
        query = query.where(Listing.removed_at.is_(None))
    if tab in TAB_STATUS:
        query = query.where(Listing.status == TAB_STATUS[tab])
    else:
        query = query.where(Listing.status != ListingStatus.HIDDEN)
    # Listings the user acted on stay visible even if they no longer fit the search
    if not filters.all and tab not in ("favorite", "contacted", "hidden"):
        query = query.where(Listing.matches.is_(True))
    if filters.source:
        query = query.where(Listing.source == filters.source)
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


@dataclass
class Card:
    listing: Listing
    address: str
    duplicates: list[Listing] = field(default_factory=list)
    # The previous price, when it dropped recently
    price_drop: int | None = None
    area_note: str | None = None


async def _cards(session: AsyncSession, listings: list[Listing]) -> list[Card]:
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
        )
        for listing in listings
    ]


async def _page_of_cards(
    session: AsyncSession, filters: ListingFilters, tab: str, offset: int
) -> tuple[list[Card], int | None]:
    query = _ordered(_listings_query(filters, tab), filters.sort)
    listings = list(await session.scalars(query.offset(offset).limit(PAGE_SIZE + 1)))
    next_offset = offset + PAGE_SIZE if len(listings) > PAGE_SIZE else None
    return await _cards(session, listings[:PAGE_SIZE]), next_offset


async def _count(session: AsyncSession, query: Select[Any]) -> int:
    return await session.scalar(select(func.count()).select_from(query.subquery())) or 0


def _criteria_summary(criteria: SearchCriteria) -> str:
    if criteria.rooms_min == criteria.rooms_max:
        room_text = f"{formatting.rooms(criteria.rooms_min)} חדרים"
    else:
        room_text = (
            f"{formatting.rooms(criteria.rooms_min)}-{formatting.rooms(criteria.rooms_max)} חדרים"
        )
    parts = [criteria.city, room_text]
    if criteria.price_max:
        parts.append(f"עד {criteria.price_max:,} ₪")
    if criteria.areas:
        parts.append(_areas_summary(criteria))
    return " · ".join(parts)


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
    async with _sessionmaker(request)() as session:
        counts = {key: await _count(session, _listings_query(filters, key)) for key, _ in TABS}
        tab = filters.tab or ("new" if counts["new"] else "all")
        cards, next_offset = await _page_of_cards(session, filters, tab, 0)
        # Listings whose ad text wasn't read yet: a search sees only their title and address
        without_text = 0
        if filters.search:
            unsearched = _listings_query(replace(filters, q=""), tab)
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
            "areas": await _known_areas(session),
            "latest_id": await session.scalar(select(func.max(Listing.id))) or 0,
            "criteria_summary": _criteria_summary(await get_criteria(session)),
        }
        return await render_page(request, session, "listings.html", context)


@router.get("/listings/cards", response_class=HTMLResponse)
async def more_cards(request: Request, offset: int = 0) -> HTMLResponse:
    filters = ListingFilters.from_request(request)
    tab = filters.tab or "all"
    async with _sessionmaker(request)() as session:
        cards, next_offset = await _page_of_cards(session, filters, tab, max(offset, 0))
    context = {
        "cards": cards,
        "next_offset": next_offset,
        "page_query": filters.query(tab=tab),
        "search": filters.search,
    }
    return render_partial(request, "partials/listing_cards.html", context)


@router.post("/listings/{listing_id}/status", response_class=HTMLResponse)
async def set_listing_status(request: Request, listing_id: int) -> HTMLResponse:
    wanted = (await request.form()).get("status")
    if wanted not in SETTABLE_STATUSES:
        raise HTTPException(400, "Unknown status")
    async with _sessionmaker(request)() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404)
        # Pressing the same button again undoes it
        listing.status = ListingStatus.SEEN if listing.status == wanted else wanted
        await session.commit()
        [card] = await _cards(session, [listing])
    return render_partial(request, "partials/listing_card.html", {"card": card})


@router.post("/listings/{listing_id}/notes")
async def set_listing_notes(request: Request, listing_id: int) -> Response:
    notes = str((await request.form()).get("notes", "")).strip()
    async with _sessionmaker(request)() as session:
        listing = await session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(404)
        listing.notes = notes or None
        await session.commit()
    return Response(status_code=204)


@router.post("/listings/mark-seen")
async def mark_new_listings_seen(request: Request) -> RedirectResponse:
    async with _sessionmaker(request)() as session:
        await session.execute(
            update(Listing)
            .where(Listing.status == ListingStatus.NEW)
            .values(status=ListingStatus.SEEN)
        )
        await session.commit()
    return RedirectResponse("/?tab=all", status_code=303)


@router.get("/api/updates")
async def listing_updates(request: Request, since_id: int = 0) -> JSONResponse:
    """New matching listings since the page loaded, for the browser notifications."""
    labels = _source_labels(request)
    async with _sessionmaker(request)() as session:
        listings = await session.scalars(
            select(Listing)
            .where(
                Listing.id > since_id,
                Listing.status == ListingStatus.NEW,
                Listing.matches.is_(True),
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
            writer.writerow(
                _spreadsheet_safe(
                    labels.get(listing.source, listing.source),
                    listing.status,
                    "כן" if listing.matches else "לא",
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
                    listing.notes or "",
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
    # The site's search page with the criteria as its own filters (or the custom URL)
    search_url: str | None
    default_interval: int
    active: int
    matching: int


async def _source_row(request: Request, session: AsyncSession, name: str) -> SourceRow:
    source = request.app.state.sources[_known_source(request, name)]
    state = await get_source_state(session, name)
    listings = select(Listing).where(Listing.source == name, Listing.removed_at.is_(None))
    try:
        search_url = state.custom_url or source.search_url(await get_criteria(session))
    except SourceError:
        search_url = None
    return SourceRow(
        name=name,
        label=source.label,
        state=state,
        running=request.app.state.runner.is_running(name),
        search_url=search_url,
        default_interval=source.default_interval_minutes,
        active=await _count(session, listings),
        matching=await _count(
            session,
            listings.where(Listing.matches.is_(True), Listing.duplicate_of_id.is_(None)),
        ),
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
        context = {
            "active": "sources",
            "rows": rows,
            "runs": runs,
            "browser_version": await request.app.state.browser.version(),
            "novnc_page": NOVNC_PAGE,
        }
        return await render_page(request, session, "sources.html", context)


@router.get("/sources/{name}/row", response_class=HTMLResponse)
async def source_row(request: Request, name: str) -> HTMLResponse:
    return await _render_row(request, name)


@router.post("/sources/{name}/run", response_class=HTMLResponse)
async def run_source_now(request: Request, name: str) -> HTMLResponse:
    _known_source(request, name)
    task = asyncio.create_task(request.app.state.runner.run(name))
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
    await asyncio.sleep(0)  # let the run start, so the row shows it as running
    return await _render_row(request, name)


@router.post("/sources/{name}/toggle", response_class=HTMLResponse)
async def toggle_source(request: Request, name: str) -> HTMLResponse:
    _known_source(request, name)
    async with _sessionmaker(request)() as session:
        state = await get_source_state(session, name)
        state.enabled = not state.enabled
        await session.commit()
    return await _render_row(request, name)


@router.post("/sources/{name}/settings", response_class=HTMLResponse)
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


async def _area_counts(session: AsyncSession, criteria: SearchCriteria) -> tuple[Counter[str], int]:
    """Current listings that fit everything but the neighborhoods, per neighborhood, and how
    many of them can't be placed in one."""
    anywhere = criteria.model_copy(update={"areas": []})
    counts: Counter[str] = Counter()
    unknown = 0
    query = select(Listing).where(
        Listing.removed_at.is_(None),
        Listing.duplicate_of_id.is_(None),
        Listing.status != ListingStatus.HIDDEN,
    )
    for listing in await session.scalars(query):
        if not listing_matches(listing, anywhere):
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
    async with _sessionmaker(request)() as session:
        criteria = await get_criteria(session)
        city_map = areas.city_map(criteria.city)
        counts, unknown = await _area_counts(session, criteria)
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
        criteria = await get_criteria(session)
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
        await save_criteria(session, criteria)
        await rematch_all(session, criteria)
        await session.commit()
    return RedirectResponse("/areas?saved=1", status_code=303)


# --- settings ------------------------------------------------------------------

SAVED_MESSAGES = {
    "criteria": "החיפוש נשמר. ההתאמה של כל המודעות חושבה מחדש.",
    "preferences": "ההגדרות נשמרו.",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = "") -> HTMLResponse:
    async with _sessionmaker(request)() as session:
        criteria = await get_criteria(session)
        context = {
            "active": "settings",
            "saved": SAVED_MESSAGES.get(saved),
            "criteria": criteria,
            "areas_summary": _areas_summary(criteria),
            "has_area_map": areas.city_map(criteria.city) is not None,
            "preferences": await get_preferences(session),
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
    async with _sessionmaker(request)() as session:
        current = await get_criteria(session)
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
    async with _sessionmaker(request)() as session:
        await save_criteria(session, criteria)
        await rematch_all(session, criteria)
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
        night_start=_time(form.get("night_start"), defaults.night_start),
        night_end=_time(form.get("night_end"), defaults.night_end),
    )
    async with _sessionmaker(request)() as session:
        await save_preferences(session, preferences)
        await session.commit()
    return RedirectResponse("/settings?saved=preferences", status_code=303)
