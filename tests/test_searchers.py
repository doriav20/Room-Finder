from roomfinder.schemas import ListingData, PropertyType, SearchCriteria
from roomfinder.searchers import FetchScope, combined_searches, in_earlier_reach


def listing(**overrides) -> ListingData:
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


def test_overlapping_room_ranges_are_read_as_one():
    [search] = combined_searches(
        [SearchCriteria(rooms_min=4, rooms_max=5), SearchCriteria(rooms_min=5, rooms_max=5.5)]
    )

    assert (search.rooms_min, search.rooms_max) == (4, 5.5)


def test_separate_ranges_are_still_one_search_to_spare_page_loads():
    [search] = combined_searches(
        [SearchCriteria(rooms_min=2, rooms_max=2), SearchCriteria(rooms_min=5, rooms_max=6)]
    )

    assert (search.rooms_min, search.rooms_max) == (2, 6)


def test_a_search_without_a_price_limit_lifts_it():
    capped = combined_searches(
        [SearchCriteria(price_min=4000, price_max=6000), SearchCriteria(price_max=8000)]
    )
    open_ended = combined_searches([SearchCriteria(price_max=6000), SearchCriteria()])

    assert (capped[0].price_min, capped[0].price_max) == (None, 8000)
    assert (open_ended[0].price_min, open_ended[0].price_max) == (None, None)


def test_property_types_add_up_and_none_chosen_means_all():
    gardens = SearchCriteria(property_types=[PropertyType.GARDEN])
    flats = SearchCriteria(property_types=[PropertyType.APARTMENT, PropertyType.PENTHOUSE])

    [both] = combined_searches([gardens, flats])
    [anything] = combined_searches([gardens, SearchCriteria(property_types=[])])

    assert both.property_types == [
        PropertyType.APARTMENT,
        PropertyType.GARDEN,
        PropertyType.PENTHOUSE,
    ]
    assert anything.property_types == []


def test_only_what_the_sites_filter_by_is_combined():
    [search] = combined_searches(
        [
            SearchCriteria(private_only=True, furnished="yes", areas=["רמז"], sqm_min=80),
            SearchCriteria(private_only=True, parking_required=True, exclude_keywords=["סאבלט"]),
        ]
    )

    assert search.private_only is True
    assert (search.furnished, search.parking_required, search.sqm_min) == ("any", False, None)
    assert (search.areas, search.exclude_keywords) == ([], [])
    assert (
        combined_searches([SearchCriteria(private_only=True), SearchCriteria()])[0].private_only
        is False
    )


def test_each_city_gets_its_own_search():
    searches = combined_searches(
        [SearchCriteria(rooms_max=5), SearchCriteria(city="חולון"), SearchCriteria(city='ראשל"צ')]
    )

    assert [search.city for search in searches] == ["חולון", "ראשון לציון"]
    assert searches[1].rooms_max == 5


def test_nobody_searching_means_nothing_to_read():
    assert combined_searches([]) == []


def test_listings_the_earlier_reading_reached_are_news():
    four = FetchScope.of(combined_searches([SearchCriteria()]), None)
    wider = FetchScope.of(combined_searches([SearchCriteria(rooms_min=4, rooms_max=5)]), None)

    assert in_earlier_reach(four, four, listing(rooms=4))
    assert in_earlier_reach(four, wider, listing(rooms=4))
    assert not in_earlier_reach(four, wider, listing(rooms=5))
    assert not in_earlier_reach(None, four, listing(rooms=4))
    # A listing the site returned although it's out of its own filters is news all the same
    assert in_earlier_reach(four, four, listing(rooms=6))


def test_a_new_custom_url_reads_the_source_afresh():
    plain = FetchScope.of(combined_searches([SearchCriteria()]), None)
    custom = FetchScope.of([SearchCriteria()], "https://www.yad2.co.il/realestate/rent?x=1")
    other = FetchScope.of([SearchCriteria()], "https://www.yad2.co.il/realestate/rent?x=2")

    assert custom.searches == []
    assert in_earlier_reach(custom, custom, listing(rooms=9))
    assert not in_earlier_reach(plain, custom, listing())
    assert not in_earlier_reach(custom, other, listing())
    assert not in_earlier_reach(custom, plain, listing())


def test_scopes_survive_being_stored():
    scope = FetchScope.of(combined_searches([SearchCriteria(price_max=7000)]), None)

    assert FetchScope.model_validate(scope.model_dump(mode="json")) == scope
