from roomfinder.sources.base import Source


def build_sources() -> dict[str, Source]:
    sources: list[Source] = []
    return {source.name: source for source in sources}
