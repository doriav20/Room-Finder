"""Hebrew display helpers, used as Jinja filters and in text summaries."""

from datetime import datetime
from zoneinfo import ZoneInfo

from roomfinder.db import utcnow
from roomfinder.models import Listing, RunStatus
from roomfinder.schemas import PROPERTY_TYPE_LABELS, PropertyType

RUN_STATUS_LABELS = {
    RunStatus.RUNNING: "⏳ רץ",
    RunStatus.OK: "✅ הצליח",
    RunStatus.BLOCKED: "🛑 נחסם",
    RunStatus.ERROR: "⚠️ שגיאה",
}


def price(value: int | None) -> str:
    return f"{value:,} ₪" if value else "מחיר לא צוין"


def rooms(value: float | None) -> str:
    return f"{value:g}" if value else "?"


def floor(value: int | None) -> str:
    if value is None:
        return ""
    return "קומת קרקע" if value == 0 else f"קומה {value}"


def property_label(value: str | None) -> str:
    if value in PropertyType:
        return PROPERTY_TYPE_LABELS[PropertyType(value)]
    return value or ""


def run_status(value: str) -> str:
    return RUN_STATUS_LABELS.get(RunStatus(value), value) if value in RunStatus else value


def time_ago(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return "—"
    seconds = max(0.0, ((now or utcnow()) - moment).total_seconds())
    minutes = int(seconds // 60)
    hours = minutes // 60
    days = hours // 24
    if minutes < 1:
        return "הרגע"
    if minutes < 60:
        return "לפני דקה" if minutes == 1 else f"לפני {minutes} דקות"
    if hours < 24:
        return {1: "לפני שעה", 2: "לפני שעתיים"}.get(hours, f"לפני {hours} שעות")
    return {1: "לפני יום", 2: "לפני יומיים"}.get(days, f"לפני {days} ימים")


def local_datetime(moment: datetime | None, timezone: str) -> str:
    if moment is None:
        return ""
    return moment.astimezone(ZoneInfo(timezone)).strftime("%d/%m/%Y %H:%M")


def address(listing: Listing) -> str:
    street = " ".join(filter(None, [listing.street, listing.house_number]))
    return ", ".join(filter(None, [street, listing.neighborhood]))


def listing_summary(listing: Listing, source_label: str) -> str:
    parts = [price(listing.price), f"{rooms(listing.rooms)} חד׳", address(listing), source_label]
    return " · ".join(part for part in parts if part)
