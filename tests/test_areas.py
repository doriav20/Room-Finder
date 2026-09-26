"""Placing listings in the official neighborhoods of their city."""

import math
from types import SimpleNamespace

import pytest

from roomfinder import areas

RISHON = "ראשון לציון"


def spot(**fields) -> SimpleNamespace:
    defaults = {"city": RISHON, "neighborhood": None, "latitude": None, "longitude": None}
    return SimpleNamespace(**(defaults | fields))


@pytest.fixture(scope="module")
def rishon() -> areas.CityMap:
    return areas.city_map(RISHON)


def named(city_map: areas.CityMap, name: str) -> areas.Area:
    return next(area for area in city_map.areas if area.name == name)


def test_rishon_lezion_has_a_map_of_its_neighborhoods(rishon):
    assert len(rishon.areas) == 40
    assert {"רמז", "נווה ים", 'רמב"ם', "קריית חתני פרס נובל"} <= rishon.names
    assert areas.city_map('ראשל"צ') is rishon
    assert areas.city_map("חולון") is None
    assert areas.city_map(None) is None


def test_each_neighborhood_is_found_by_its_own_name_and_label_point(rishon):
    for area in rishon.areas:
        assert rishon.named(area.name) is area
        assert rishon.at(*area.label_point) is area


@pytest.mark.parametrize(
    ("site_name", "official"),
    [
        ('רמב"ם', 'רמב"ם'),
        ("רמבם", 'רמב"ם'),
        ("רמב''ם", 'רמב"ם'),
        ("אברמוביץ", "אברמוביץ'"),
        ("קרית כרמים", "קריית כרמים"),
        ("נוה הדרים", "נווה הדרים"),
        ("נאות שיקמה", "נאות שקמה"),
        ("בנות חיל", "בנות חייל"),
        ("הרקפות", "רקפות"),
        ("הראשונים", "ראשונים"),
        ("שכונת נווה ים", "נווה ים"),
        ("רמז, מערב ותיק", "רמז"),
        ("רביבים, גני ראשון", "רביבים"),
        ("נווה חוף-פואבלו", "נווה חוף"),
        ("מישור הנוף, ראשון לציון", "מישור הנוף"),
        ("אשלים", "נאות אשלים"),
    ],
)
def test_each_sites_spelling_finds_the_official_neighborhood(rishon, site_name, official):
    assert rishon.named(site_name).name == official


@pytest.mark.parametrize(
    "site_name",
    ["מרכז", "המזרח", "מערב הישן", "צפון", "ראשון", 'מב"ת', "רמת הנשיא", "גן המצבה", "", None],
)
def test_vague_or_unknown_names_are_not_guessed(rishon, site_name):
    assert rishon.named(site_name) is None


def test_a_map_point_wins_over_the_sites_neighborhood_name(rishon):
    lat, lon = named(rishon, "נווה ים").label_point

    assert areas.locate(spot(neighborhood="רמז", latitude=lat, longitude=lon)) == "נווה ים"
    assert areas.locate(spot(neighborhood="רמבם")) == 'רמב"ם'
    assert areas.locate(spot()) is None


def test_a_point_just_outside_the_outlines_still_counts(rishon):
    # The easternmost corner of the city's neighborhoods
    lon, lat = max(point for area in rishon.areas for point in area.ring)
    meters_per_degree = 111_320 * math.cos(math.radians(lat))

    assert rishon.at(lat, lon + 20 / meters_per_degree) is not None
    assert rishon.at(lat, lon + 300 / meters_per_degree) is None


def test_points_outside_the_city_fall_back_to_the_name(rishon):
    west, south, east, north = rishon.bounds

    assert areas.locate(spot(neighborhood="רמז", latitude=north + 0.05, longitude=west)) == "רמז"
    assert areas.locate(spot(latitude=north + 0.05, longitude=west)) is None


def test_listings_in_cities_without_a_map_have_no_neighborhood():
    assert areas.locate(spot(city="חולון", neighborhood="רמז")) is None
    assert areas.locate(spot(city=None, neighborhood="רמז")) is None


def test_a_city_written_with_its_neighborhood_is_still_the_city():
    listing = spot(city="ראשון לציון קרית חתני פרס נובל", neighborhood="קרית חתני פרס נובל")

    assert areas.locate(listing) == "קריית חתני פרס נובל"
