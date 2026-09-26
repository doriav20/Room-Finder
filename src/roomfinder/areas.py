"""City neighborhoods: their outlines on the map, and which one a listing is in.

Yad2, Madlan and OnMap give coordinates, so their listings are placed by the outline they
fall in. Komo and Homeless give only a neighborhood name, which every site spells its own way
("רמב"ם", "רמבם", "רמב''ם"), so those are matched to the official names.
"""

import itertools
import json
import math
import re
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Protocol

from roomfinder.normalize import clean_text, normalize_city

DATA_DIR = Path(__file__).parent / "data" / "areas"
# Outlines are simplified, and an address on a border street can sit just outside both sides
EDGE_TOLERANCE_METERS = 50
METERS_PER_DEGREE_LATITUDE = 110_574
METERS_PER_DEGREE_LONGITUDE_AT_EQUATOR = 111_320

_QUOTES = re.compile(r"[\"']")
_NAME_PREFIX = re.compile(r"^(?:שכונת|שכ)\s+")
_PARTS = re.compile(r"\s*[,/()\-–]\s*")
# Words that point at a part of town rather than at one neighborhood ("המזרח", "מערב ותיק")
_GENERIC_WORDS = ("מזרח", "מערב", "צפון", "דרום", "מרכז", "ותיק", "ישן", "חדש", "העיר", "עיר")


class Locatable(Protocol):
    city: str | None
    neighborhood: str | None
    latitude: float | None
    longitude: float | None


def _name_key(text: str | None) -> str | None:
    """'שכונת רמב"ם' -> 'רמבם'."""
    cleaned = clean_text(text)
    if cleaned is None:
        return None
    return _NAME_PREFIX.sub("", _QUOTES.sub("", cleaned)).strip() or None


def _word_skeleton(word: str) -> str:
    # Sites differ in the vowel letters (קרית/קריית, נוה/נווה, שקמה/שיקמה) and in a leading
    # "ה" (הרקפות/רקפות); short words keep theirs ("הים", "הלל")
    if len(word) > 3 and word.startswith("ה"):
        word = word[1:]
    return word.replace("ו", "").replace("י", "")


def _skeleton(text: str | None) -> tuple[str, ...]:
    key = _name_key(text)
    words = (_word_skeleton(word) for word in key.split()) if key else ()
    return tuple(word for word in words if word)


_GENERIC = frozenset(_skeleton(word)[0] for word in _GENERIC_WORDS)


def _contains_run(words: tuple[str, ...], run: tuple[str, ...]) -> bool:
    return any(words[i : i + len(run)] == run for i in range(len(words) - len(run) + 1))


def _meters(lon: float, lat: float, reference_lat: float) -> tuple[float, float]:
    scale = METERS_PER_DEGREE_LONGITUDE_AT_EQUATOR * math.cos(math.radians(reference_lat))
    return lon * scale, lat * METERS_PER_DEGREE_LATITUDE


@dataclass(frozen=True)
class Area:
    name: str
    # (longitude, latitude) pairs; the first point is repeated at the end
    ring: tuple[tuple[float, float], ...]

    @cached_property
    def bounds(self) -> tuple[float, float, float, float]:
        """West, south, east, north."""
        lons = [lon for lon, _ in self.ring]
        lats = [lat for _, lat in self.ring]
        return min(lons), min(lats), max(lons), max(lats)

    def contains(self, lon: float, lat: float) -> bool:
        west, south, east, north = self.bounds
        if not (west <= lon <= east and south <= lat <= north):
            return False
        inside = False
        for (x1, y1), (x2, y2) in itertools.pairwise(self.ring):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def distance_meters(self, lon: float, lat: float) -> float:
        """How far a point is from the area (0 inside it)."""
        if self.contains(lon, lat):
            return 0.0
        px, py = _meters(lon, lat, lat)
        best = math.inf
        for start, end in itertools.pairwise(self.ring):
            ax, ay = _meters(*start, lat)
            bx, by = _meters(*end, lat)
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length)) if length else 0.0
            best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
        return best

    @cached_property
    def label_point(self) -> tuple[float, float]:
        """A point inside the area to put its name on, as (latitude, longitude)."""
        # The centroid, unless the shape curls around it; then the middle of the widest
        # stretch of the area along the centroid's latitude
        twice_area = cx = cy = 0.0
        for (x1, y1), (x2, y2) in itertools.pairwise(self.ring):
            cross = x1 * y2 - x2 * y1
            twice_area += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if not twice_area:
            return self.ring[0][1], self.ring[0][0]
        cx, cy = cx / (3 * twice_area), cy / (3 * twice_area)
        if self.contains(cx, cy):
            return cy, cx
        crossings = sorted(
            x1 + (cy - y1) * (x2 - x1) / (y2 - y1)
            for (x1, y1), (x2, y2) in itertools.pairwise(self.ring)
            if (y1 > cy) != (y2 > cy)
        )
        stretches = list(zip(crossings[::2], crossings[1::2], strict=False))
        if not stretches:
            return self.ring[0][1], self.ring[0][0]
        start, end = max(stretches, key=lambda stretch: stretch[1] - stretch[0])
        return cy, (start + end) / 2


@dataclass(frozen=True)
class CityMap:
    city: str
    source: str
    areas: tuple[Area, ...]

    @cached_property
    def bounds(self) -> tuple[float, float, float, float]:
        """West, south, east, north."""
        boxes = [area.bounds for area in self.areas]
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(area.name for area in self.areas)

    @cached_property
    def _skeletons(self) -> dict[tuple[str, ...], Area]:
        return {_skeleton(area.name): area for area in self.areas}

    def at(self, lat: float, lon: float) -> Area | None:
        """The area a point is in."""
        for area in self.areas:
            if area.contains(lon, lat):
                return area
        nearest = min(self.areas, key=lambda area: area.distance_meters(lon, lat))
        return nearest if nearest.distance_meters(lon, lat) <= EDGE_TOLERANCE_METERS else None

    def named(self, text: str | None) -> Area | None:
        """The area a site's neighborhood name refers to, when it clearly refers to one."""
        key = _name_key(text)
        if key is None:
            return None
        # "רמז, מערב ותיק" or "נווה חוף-פואבלו": the whole name, then each part of it
        candidates = [key, *(part for part in _PARTS.split(key) if part and part != key)]
        skeletons = [skeleton for skeleton in map(_skeleton, candidates) if skeleton]
        for skeleton in skeletons:
            if area := self._skeletons.get(skeleton):
                return area
        # A shortened name, e.g. "אשלים" for "נאות אשלים", if only one area has it
        for skeleton in skeletons:
            if len(skeleton) == 1 and (skeleton[0] in _GENERIC or len(skeleton[0]) < 3):
                continue
            found = [
                area for words, area in self._skeletons.items() if _contains_run(words, skeleton)
            ]
            if len(found) == 1:
                return found[0]
        return None

    def locate(self, listing: Locatable) -> Area | None:
        """By the listing's map point when it has one inside the city, else by its name."""
        has_point = listing.latitude is not None and listing.longitude is not None
        if has_point and (area := self.at(listing.latitude, listing.longitude)):
            return area
        return self.named(listing.neighborhood)


def _load(path: Path) -> CityMap:
    data = json.loads(path.read_text(encoding="utf-8"))
    areas = tuple(
        Area(
            name=feature["properties"]["name"],
            ring=tuple((lon, lat) for lon, lat in feature["geometry"]["coordinates"][0]),
        )
        for feature in data["features"]
    )
    return CityMap(city=normalize_city(data["city"]), source=data.get("source", ""), areas=areas)


@cache
def _city_maps() -> dict[str, CityMap]:
    maps = (_load(path) for path in sorted(DATA_DIR.glob("*.geojson")))
    return {city_map.city: city_map for city_map in maps}


def city_map(city: str | None) -> CityMap | None:
    """The neighborhoods map of a city, for the cities that have one."""
    return _city_maps().get(normalize_city(city) or "")


def locate(listing: Locatable) -> str | None:
    """The official neighborhood a listing is in, or None when it can't be told."""
    found = city_map(listing.city)
    area = found.locate(listing) if found else None
    return area.name if area else None
