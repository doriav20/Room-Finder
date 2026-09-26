from roomfinder.sources.base import Source
from roomfinder.sources.homeless import HomelessSource
from roomfinder.sources.komo import KomoSource
from roomfinder.sources.onmap import OnMapSource
from roomfinder.sources.yad2 import Yad2Source


def build_sources() -> dict[str, Source]:
    sources: list[Source] = [Yad2Source(), KomoSource(), HomelessSource(), OnMapSource()]
    return {source.name: source for source in sources}
