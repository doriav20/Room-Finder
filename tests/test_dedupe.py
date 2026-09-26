from roomfinder.dedupe import same_apartment
from roomfinder.models import Listing


def make(**overrides) -> Listing:
    fields = {
        "source": "yad2",
        "source_id": "1",
        "url": "https://example.com/1",
        "rooms": 4.0,
        "price": 6000,
        "address_key": "הרצל|12",
        "neighborhood": "רמז",
        "sqm": 95,
    } | overrides
    return Listing(**fields)


def test_same_address_rooms_and_close_price_is_the_same_apartment():
    assert same_apartment(make(), make(source="madlan", price=6150))


def test_price_difference_above_tolerance_is_a_different_apartment():
    assert not same_apartment(make(), make(price=6400))


def test_different_floor_is_a_different_apartment():
    assert not same_apartment(make(floor=2), make(floor=5))
    assert same_apartment(make(floor=2), make(floor=None))


def test_different_address_or_rooms_is_a_different_apartment():
    assert not same_apartment(make(), make(address_key="הרצל|14"))
    assert not same_apartment(make(), make(rooms=4.5))


def test_without_address_falls_back_to_neighborhood_and_size():
    assert same_apartment(make(address_key=None), make(sqm=97))
    assert not same_apartment(make(address_key=None), make(sqm=110))
    assert not same_apartment(make(address_key=None), make(neighborhood="נווה הדרים"))
    assert not same_apartment(make(address_key=None, sqm=None), make())


def test_unknown_price_is_never_a_duplicate():
    assert not same_apartment(make(price=None), make())
