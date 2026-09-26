import pytest

from roomfinder.normalize import (
    address_key,
    canonical_property_type,
    contains_phrase,
    normalize_city,
    normalize_house_number,
    normalize_street,
    parse_floor,
    parse_price,
    parse_rooms,
)
from roomfinder.schemas import PropertyType


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("6,200 ₪", 6200),
        ("₪ 6200", 6200),
        (6200, 6200),
        ('5,800 ש"ח', 5800),
        ("לא צוין מחיר", None),
        (None, None),
        (0, None),
        (True, None),
    ],
)
def test_parse_price(value, expected):
    assert parse_price(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("4", 4.0), ("4.5 חדרים", 4.5), ("4,5", 4.5), (4, 4.0), ("", None), (None, None), ("0", None)],
)
def test_parse_rooms(value, expected):
    assert parse_rooms(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("קרקע", 0), ("קומה 3", 3), (5, 5), ("מרתף", -1), (None, None), ("", None)],
)
def test_parse_floor(value, expected):
    assert parse_floor(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('ראשל"צ', "ראשון לציון"),
        ("ראשל״צ", "ראשון לציון"),
        ("  ראשון   לציון ", "ראשון לציון"),
        ("Rishon LeZion", "ראשון לציון"),
        ("חולון", "חולון"),
        (None, None),
    ],
)
def test_normalize_city(value, expected):
    assert normalize_city(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("רחוב הרצל", "הרצל"),
        ("רח' הרצל", "הרצל"),
        ("רח׳ הרצל", "הרצל"),
        ("שד' רוטשילד", "רוטשילד"),
        ("ז'בוטינסקי", "ז בוטינסקי"),
        ("ז׳בוטינסקי", "ז בוטינסקי"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_street(value, expected):
    assert normalize_street(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12", "12"), (12, "12"), ("12א", "12א"), ("12 א", "12א"), ("12אב", "12"), ("0", None)],
)
def test_normalize_house_number(value, expected):
    assert normalize_house_number(value) == expected


def test_address_key_ignores_street_prefixes_and_types():
    assert address_key("רחוב הרצל", "12") == address_key("רח' הרצל", 12) == "הרצל|12"
    assert address_key("הרצל", None) is None
    assert address_key(None, "12") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("דירה", PropertyType.APARTMENT),
        ("דירת גן", PropertyType.GARDEN),
        ("גג/פנטהאוז", PropertyType.PENTHOUSE),
        ("מיני פנטהאוז", PropertyType.PENTHOUSE),
        ("דופלקס", PropertyType.DUPLEX),
        ("בית פרטי/קוטג'", PropertyType.HOUSE),
        ("יחידת דיור", PropertyType.UNIT),
        ("סטודיו/לופט", PropertyType.STUDIO),
        ("חניה", PropertyType.OTHER),
        (None, None),
    ],
)
def test_canonical_property_type(value, expected):
    assert canonical_property_type(value) == expected


@pytest.mark.parametrize(
    ("text", "phrase", "expected"),
    [
        ("מחפש שותף לדירה", "שותף לדירה", True),
        ("סאבלט לחודשיים", "סאבלט", True),
        ("חצר משותפת", "שותף", False),
        ("דירה עם גג משותף", "שותף", False),
        (None, "סאבלט", False),
        ("סאבלט", "", False),
    ],
)
def test_contains_phrase_matches_whole_words(text, phrase, expected):
    assert contains_phrase(text, phrase) is expected
