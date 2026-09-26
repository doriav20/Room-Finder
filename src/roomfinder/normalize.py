"""Parsing and normalization of the messy values that come from listing sites."""

import re
from typing import Any

from roomfinder.schemas import PropertyType

_QUOTES = str.maketrans({"״": '"', "׳": "'", "“": '"', "”": '"', "‘": "'", "’": "'", "`": "'"})
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_STREET_PREFIX = re.compile(r"^(?:רחוב|רח'|רח\.|שדרות|שד'|שד\.)\s*")
_STREET_PUNCTUATION = re.compile(r"[\"'\-.,()]")
_HOUSE_NUMBER = re.compile(r"(\d+)\s*([א-ת](?![א-ת]))?")

CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "ראשון לציון": (
        "ראשון לציון",
        "ראשון-לציון",
        'ראשל"צ',
        "ראשלצ",
        "ראשון",
        "rishon lezion",
        "rishon le zion",
        "rishon letsiyon",
    ),
}

# Checked in order: the more specific names first ("דירת גן" before "דירה")
_PROPERTY_TYPE_KEYWORDS: tuple[tuple[str, PropertyType], ...] = (
    ("מיני פנטהאוז", PropertyType.PENTHOUSE),
    ("פנטהאוז", PropertyType.PENTHOUSE),
    ("דירת גג", PropertyType.PENTHOUSE),
    ("גג", PropertyType.PENTHOUSE),
    ("דירת גן", PropertyType.GARDEN),
    ("דופלקס", PropertyType.DUPLEX),
    ("טריפלקס", PropertyType.DUPLEX),
    ("יחידת דיור", PropertyType.UNIT),
    ("סטודיו", PropertyType.STUDIO),
    ("לופט", PropertyType.STUDIO),
    ("קוטג", PropertyType.HOUSE),
    ("בית פרטי", PropertyType.HOUSE),
    ("דו משפחתי", PropertyType.HOUSE),
    ("דו-משפחתי", PropertyType.HOUSE),
    ("וילה", PropertyType.HOUSE),
    ("משק", PropertyType.HOUSE),
    ("דירה", PropertyType.APARTMENT),
    ("דירות", PropertyType.APARTMENT),
)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", str(value).translate(_QUOTES)).strip()
    return text or None


def clean_multiline(value: Any) -> str | None:
    """Like clean_text, but keeps the line breaks of an ad's text."""
    if value is None:
        return None
    lines = [clean_text(line) for line in str(value).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def _first_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _NUMBER.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def parse_price(value: Any) -> int | None:
    number = _first_number(value)
    return int(number) if number and number > 0 else None


def parse_int(value: Any) -> int | None:
    number = _first_number(value)
    return int(number) if number is not None else None


def parse_rooms(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.replace(",", ".")
    number = _first_number(value)
    return number if number and 0 < number < 20 else None


def parse_floor(value: Any) -> int | None:
    text = clean_text(value)
    if text is None:
        return None
    if "קרקע" in text:
        return 0
    if "מרתף" in text:
        return -1
    match = re.search(r"-?\d+", text)
    return int(match.group()) if match else None


def normalize_city(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    for canonical, aliases in CITY_ALIASES.items():
        if lowered in aliases:
            return canonical
    return text


def normalize_neighborhood(value: Any) -> str | None:
    text = clean_text(value)
    return _WHITESPACE.sub(" ", _STREET_PUNCTUATION.sub(" ", text)).strip() if text else None


def normalize_street(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    text = _STREET_PREFIX.sub("", text)
    text = _WHITESPACE.sub(" ", _STREET_PUNCTUATION.sub(" ", text)).strip()
    return text or None


def normalize_house_number(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    match = _HOUSE_NUMBER.search(text)
    if not match or match.group(1) == "0":
        return None
    return match.group(1) + (match.group(2) or "")


def address_key(street: Any, house_number: Any) -> str | None:
    """A key for spotting the same building across sites: "street|number"."""
    street_name = normalize_street(street)
    number = normalize_house_number(house_number)
    if not street_name or not number:
        return None
    return f"{street_name}|{number}"


# Phrases in an ad's own text, used when the site has no structured field. Negative phrases
# are checked first, since "לא מרוהטת" contains "מרוהטת".
_NO_FURNITURE = ("ללא ריהוט", "בלי ריהוט", "לא מרוהטת", "לא מרוהט", "ללא רהיטים")
_FURNITURE = ("מרוהטת", "מרוהט", "ריהוט מלא", "ריהוט חלקי", "כולל ריהוט", "עם ריהוט")
_NO_PARKING = (
    "ללא חניה",
    "ללא חנייה",
    "אין חניה",
    "אין חנייה",
    "בלי חניה",
    "בלי חנייה",
    "חניה ברחוב",
    "חנייה ברחוב",
)
_PARKING = (
    "חניה פרטית",
    "חנייה פרטית",
    "חניה בטאבו",
    "חנייה בטאבו",
    "חניה צמודה",
    "חנייה צמודה",
    "חניה מקורה",
    "חנייה מקורה",
    "חניה תת קרקעית",
    "חנייה תת קרקעית",
    "שתי חניות",
    "2 חניות",
)


def _phrase_flag(text: Any, negative: tuple[str, ...], positive: tuple[str, ...]) -> bool | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    if any(phrase in cleaned for phrase in negative):
        return False
    if any(phrase in cleaned for phrase in positive):
        return True
    return None


def furniture_from_text(text: Any) -> bool | None:
    return _phrase_flag(text, _NO_FURNITURE, _FURNITURE)


def parking_from_text(text: Any) -> bool | None:
    return _phrase_flag(text, _NO_PARKING, _PARKING)


def contains_phrase(text: str | None, phrase: str) -> bool:
    """Whole-word match: "שותף" is found in "מחפש שותף" but not in "משותף"."""
    phrase = clean_text(phrase) or ""
    if not text or not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", clean_text(text) or "") is not None


def canonical_property_type(value: Any) -> PropertyType | None:
    text = clean_text(value)
    if text is None:
        return None
    for keyword, property_type in _PROPERTY_TYPE_KEYWORDS:
        if keyword in text:
            return property_type
    return PropertyType.OTHER
