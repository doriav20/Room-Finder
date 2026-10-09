from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class PropertyType(StrEnum):
    APARTMENT = "apartment"
    GARDEN = "garden"
    DUPLEX = "duplex"
    PENTHOUSE = "penthouse"
    HOUSE = "house"
    UNIT = "unit"
    STUDIO = "studio"
    OTHER = "other"


PROPERTY_TYPE_LABELS: dict[PropertyType, str] = {
    PropertyType.APARTMENT: "דירה",
    PropertyType.GARDEN: "דירת גן",
    PropertyType.DUPLEX: "דופלקס",
    PropertyType.PENTHOUSE: "גג/פנטהאוז",
    PropertyType.HOUSE: "בית פרטי/קוטג'",
    PropertyType.UNIT: "יחידת דיור",
    PropertyType.STUDIO: "סטודיו/לופט",
    PropertyType.OTHER: "אחר",
}


class ListingData(BaseModel):
    """A listing as parsed from a source, before it is stored."""

    source: str
    source_id: str
    url: str
    title: str | None = None
    description: str | None = None
    price: int | None = None
    rooms: float | None = None
    sqm: int | None = None
    floor: int | None = None
    total_floors: int | None = None
    property_type: PropertyType | None = None
    city: str | None = None
    neighborhood: str | None = None
    street: str | None = None
    house_number: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_agency: bool | None = None
    entry_date: str | None = None
    images: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    furnished: bool | None = None
    furniture_details: str | None = None
    parking: bool | None = None
    parking_spaces: int | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] | None = None


class ListingDetails(BaseModel):
    """What a listing's own page adds to the search results."""

    description: str | None = None
    furnished: bool | None = None
    furniture_details: str | None = None
    parking: bool | None = None
    parking_spaces: int | None = None
    sqm: int | None = None
    entry_date: str | None = None
    posted_at: datetime | None = None
    features: list[str] = Field(default_factory=list)


class SearchCriteria(BaseModel):
    """What the user is looking for; editable from the dashboard."""

    city: str = "ראשון לציון"
    rooms_min: float = 4
    rooms_max: float = 4
    price_min: int | None = None
    price_max: int | None = None
    sqm_min: int | None = None
    property_types: list[PropertyType] = Field(
        default_factory=lambda: [
            PropertyType.APARTMENT,
            PropertyType.GARDEN,
            PropertyType.DUPLEX,
            PropertyType.PENTHOUSE,
        ]
    )
    private_only: bool = False
    furnished: Literal["any", "yes", "no"] = "any"
    parking_required: bool = False
    # Official neighborhoods from the city's map (see areas.py); empty means the whole city
    areas: list[str] = Field(default_factory=list)
    # Whether a listing that can't be placed in a neighborhood still fits
    unknown_area_ok: bool = True
    # Matched as whole words, so "שותף" would not hit "חצר משותפת"
    exclude_keywords: list[str] = Field(
        default_factory=lambda: ["סאבלט", "שותף לדירה", "שותפה לדירה"]
    )

    def fetch_fields(self) -> dict[str, Any]:
        """Fields that change which listings the sources return (the rest are local filters)."""
        return self.model_dump(
            mode="json",
            include={
                "city",
                "rooms_min",
                "rooms_max",
                "price_min",
                "price_max",
                "property_types",
                "private_only",
            },
        )
