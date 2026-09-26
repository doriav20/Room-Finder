from roomfinder.matching import listing_matches, mismatch_reasons
from roomfinder.models import Listing
from roomfinder.schemas import ListingData, PropertyType, SearchCriteria


def make_listing(**overrides) -> ListingData:
    fields = {
        "source": "yad2",
        "source_id": "1",
        "url": "https://example.com/1",
        "city": "ראשון לציון",
        "rooms": 4,
        "price": 6000,
        "property_type": PropertyType.APARTMENT,
    } | overrides
    return ListingData(**fields)


def test_default_criteria_match_a_4_room_apartment_in_rishon():
    assert listing_matches(make_listing(), SearchCriteria())


def test_city_aliases_are_the_same_city():
    assert listing_matches(make_listing(city='ראשל"צ'), SearchCriteria())


def test_other_city_does_not_match():
    assert mismatch_reasons(make_listing(city="חולון"), SearchCriteria()) == ["city"]


def test_rooms_must_be_within_range():
    criteria = SearchCriteria()
    assert mismatch_reasons(make_listing(rooms=3.5), criteria) == ["rooms"]
    assert mismatch_reasons(make_listing(rooms=4.5), criteria) == ["rooms"]
    assert listing_matches(make_listing(rooms=4.5), SearchCriteria(rooms_min=3.5, rooms_max=4.5))


def test_price_limits():
    criteria = SearchCriteria(price_min=4000, price_max=6500)
    assert listing_matches(make_listing(price=6500), criteria)
    assert mismatch_reasons(make_listing(price=7000), criteria) == ["price"]
    assert mismatch_reasons(make_listing(price=3000), criteria) == ["price"]


def test_unknown_values_do_not_disqualify():
    listing = make_listing(city=None, rooms=None, price=None, property_type=None)
    assert listing_matches(listing, SearchCriteria(price_max=5000))


def test_property_type_must_be_selected():
    listing = make_listing(property_type=PropertyType.HOUSE)
    assert mismatch_reasons(listing, SearchCriteria()) == ["property_type"]


def test_private_only_rejects_agencies():
    criteria = SearchCriteria(private_only=True)
    assert mismatch_reasons(make_listing(is_agency=True), criteria) == ["agency"]
    assert listing_matches(make_listing(is_agency=False), criteria)
    assert listing_matches(make_listing(is_agency=None), criteria)


def test_furniture_preference():
    furnished_only = SearchCriteria(furnished="yes")
    unfurnished_only = SearchCriteria(furnished="no")

    assert listing_matches(make_listing(furnished=True), furnished_only)
    assert mismatch_reasons(make_listing(furnished=False), furnished_only) == ["furnished"]
    assert mismatch_reasons(make_listing(furnished=True), unfurnished_only) == ["furnished"]
    assert listing_matches(make_listing(furnished=None), furnished_only)
    assert listing_matches(make_listing(furnished=False), SearchCriteria())


def test_parking_requirement():
    criteria = SearchCriteria(parking_required=True)

    assert listing_matches(make_listing(parking=True), criteria)
    assert mismatch_reasons(make_listing(parking=False), criteria) == ["parking"]
    assert listing_matches(make_listing(parking=None), criteria)


def test_chosen_neighborhoods():
    criteria = SearchCriteria(areas=["רמז", "נווה ים"])

    assert listing_matches(make_listing(neighborhood="רמז, מערב ותיק"), criteria)
    assert mismatch_reasons(make_listing(neighborhood="רמבם"), criteria) == ["area"]
    assert listing_matches(make_listing(neighborhood="רמבם"), SearchCriteria())


def test_listings_that_cant_be_placed_fit_unless_turned_off():
    listing = make_listing(neighborhood="מרכז")

    assert listing_matches(listing, SearchCriteria(areas=["רמז"]))
    strict = SearchCriteria(areas=["רמז"], unknown_area_ok=False)
    assert mismatch_reasons(listing, strict) == ["area"]


def test_neighborhoods_are_ignored_for_a_city_without_a_map():
    criteria = SearchCriteria(city="חולון", areas=["רמז"], unknown_area_ok=False)

    assert listing_matches(make_listing(city="חולון", neighborhood="נאות רחל"), criteria)


def test_exclude_keywords_match_whole_words_only():
    criteria = SearchCriteria()
    assert listing_matches(make_listing(description="חצר משותפת וגג משותף"), criteria)
    assert mismatch_reasons(make_listing(title="סאבלט ל-3 חודשים"), criteria) == ["keyword"]


def test_stored_listings_can_be_matched_too():
    stored = Listing(
        source="yad2",
        source_id="1",
        url="https://example.com/1",
        city="ראשון לציון",
        rooms=4.0,
        price=6000,
        property_type="apartment",
    )
    assert listing_matches(stored, SearchCriteria())
    assert not listing_matches(stored, SearchCriteria(property_types=[PropertyType.GARDEN]))
