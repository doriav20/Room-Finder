"""Free-text search in a listing's own words: the title, the ad's text and the address.

Every word must appear, as part of a word too, so "מרפסת" finds "ומרפסת" and "המרפסת".
"Quoted words" must appear together, and -word must not appear at all.
"""

import re
from dataclasses import dataclass

from markupsafe import Markup, escape
from sqlalchemy import ColumnElement, String, func, not_

from roomfinder.models import Listing
from roomfinder.normalize import clean_text

MAX_QUERY_LENGTH = 200
MAX_TERMS = 10

# Run on the stored text (in SQL) and on the query alike. Hebrew is spelled with and without
# an extra yod or vav ("חניה" / "חנייה"), so a doubled one counts as one.
_FOLDS = (("״", '"'), ("׳", "'"), ("\n", " "), ("יי", "י"), ("וו", "ו"))
# A phrase opens with a quote at the start of a word, so the quote in ממ"ד is not one
_TERM = re.compile(r'(?<!\S)(-?)"([^"]+)"(?!\S)|(\S+)')
_EDGE = ".,;:!?()[]{}\"'-"
# How the folded letters may be written in the original text, for highlighting
_LETTER_PATTERNS = {
    "י": "י{1,2}",
    "ו": "ו{1,2}",
    '"': '["״“”]',
    "'": "['׳‘’`]",
    " ": r"\s+",
}
_TEXT_COLUMNS = (
    Listing.title,
    Listing.description,
    Listing.furniture_details,
    Listing.street,
    Listing.neighborhood,
)


def fold(text: str) -> str:
    text = text.lower()
    for old, new in _FOLDS:
        text = text.replace(old, new)
    return text


def _searchable_text() -> ColumnElement[str]:
    text: ColumnElement[str] = func.coalesce(_TEXT_COLUMNS[0], "")
    for column in _TEXT_COLUMNS[1:]:
        text = text + " " + func.coalesce(column, "")
    text = func.lower(text, type_=String)
    for old, new in _FOLDS:
        text = func.replace(text, old, new, type_=String)
    return text


def _pattern(term: str) -> str:
    return "".join(_LETTER_PATTERNS.get(letter) or re.escape(letter) for letter in term)


@dataclass(frozen=True)
class TextSearch:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def parse(cls, query: str) -> "TextSearch":
        include: list[str] = []
        exclude: list[str] = []
        # clean_text writes every kind of quote the plain way, like the stored text
        for match in _TERM.finditer(clean_text(query[:MAX_QUERY_LENGTH]) or ""):
            negative, phrase, word = match.groups()
            if word is not None:
                negative = "-" if word.startswith("-") and len(word) > 1 else ""
                phrase = word[len(negative) :]
            term = fold(phrase.strip(_EDGE + " "))
            terms = exclude if negative else include
            if term and term not in terms:
                terms.append(term)
        return cls(tuple(include[:MAX_TERMS]), tuple(exclude[:MAX_TERMS]))

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)

    def conditions(self) -> list[ColumnElement[bool]]:
        """For a query on Listing: every term in, every excluded term out."""
        text = _searchable_text()
        return [text.contains(term, autoescape=True) for term in self.include] + [
            not_(text.contains(term, autoescape=True)) for term in self.exclude
        ]

    def found_in(self, text: str | None) -> bool:
        folded = fold(text or "")
        return any(term in folded for term in self.include)

    def highlight(self, text: str | None) -> Markup:
        """The text, HTML-escaped, with the searched words marked."""
        if not text:
            return Markup("")
        if not self.include:
            return escape(text)
        # Longest first, so a phrase is marked whole rather than one of its words
        terms = sorted(self.include, key=len, reverse=True)
        pattern = re.compile("|".join(_pattern(term) for term in terms), re.IGNORECASE)
        parts: list[Markup] = []
        end = 0
        for match in pattern.finditer(text):
            parts += [escape(text[end : match.start()]), Markup("<mark>%s</mark>") % match[0]]
            end = match.end()
        parts.append(escape(text[end:]))
        return Markup("").join(parts)
