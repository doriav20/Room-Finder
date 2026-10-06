import pytest
from sqlalchemy import select

from roomfinder.models import Listing
from roomfinder.search import TextSearch


def test_words_phrases_and_excluded_words():
    search = TextSearch.parse('מרפסת  "מרפסת  שמש" -שותפים, ממ״ד -')

    assert search.include == ("מרפסת", "מרפסת שמש", 'ממ"ד')
    assert search.exclude == ("שותפים",)


def test_the_query_is_folded_like_the_text():
    assert TextSearch.parse("חנייה  שירותים").include == ("חניה", "שירותים")
    assert TextSearch.parse("“Duplex”").include == ("duplex",)


def test_an_empty_query_searches_nothing():
    assert not TextSearch.parse("  ")
    assert not TextSearch.parse('""')
    assert TextSearch.parse("-שותפים")


def test_highlight_marks_every_spelling_and_escapes_the_rest():
    search = TextSearch.parse('חניה "מרפסת שמש"')

    marked = search.highlight("<b>חנייה</b> בטאבו, מרפסת\nשמש ומרפסת")

    assert marked == "&lt;b&gt;<mark>חנייה</mark>&lt;/b&gt; בטאבו, <mark>מרפסת\nשמש</mark> ומרפסת"
    assert TextSearch().highlight("<i>") == "&lt;i&gt;"
    assert search.highlight(None) == ""


def test_found_in():
    search = TextSearch.parse("מקלחות")

    assert search.found_in("2 שירותים ומקלחות")
    assert not search.found_in("מקלחת אחת")
    assert not search.found_in(None)


@pytest.fixture
async def listings(session):
    texts = {
        "a": ("דירה, הרצל 12", "דירה מרווחת עם מרפסת שמש וחנייה בטאבו"),
        "b": ("דירה, ביאליק 3", "מתאימה לשותפים, 2 מקלחות ומרפסת"),
        "c": ("פנטהאוז, רוטשילד 5", None),
        "d": ("דירה, הגפן 1", "100% שמורה, שמש_בוקר"),
    }
    for source_id, (title, description) in texts.items():
        session.add(
            Listing(
                source="yad2",
                source_id=source_id,
                url=f"https://example.com/{source_id}",
                title=title,
                description=description,
                images=[],
            )
        )
    await session.flush()


async def found(session, query: str) -> list[str]:
    conditions = TextSearch.parse(query).conditions()
    rows = await session.scalars(select(Listing.source_id).where(*conditions))
    return sorted(rows)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("מרפסת", ["a", "b"]),  # "ומרפסת" too
        ("מרפסת -שותפים", ["a"]),
        ('"מרפסת שמש"', ["a"]),
        ("חניה", ["a"]),  # written "חנייה"
        ("מקלחות מרפסת", ["b"]),
        ("פנטהאוז", ["c"]),  # in the title
        ("רוטשילד", ["c"]),
        ("100%", ["d"]),  # SQL wildcards are plain text
        ("_", ["d"]),
        ("ממ״ד", []),
    ],
)
async def test_conditions(session, listings, query, expected):
    assert await found(session, query) == expected
