import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from playwright.async_api import Page

from roomfinder.schemas import ListingData, ListingDetails, SearchCriteria


class BlockedError(Exception):
    """The site answered with a bot check or captcha instead of results."""


class SourceError(Exception):
    """The site answered, but not with anything we could read."""


@dataclass
class FetchResult:
    listings: list[ListingData]
    # False when results were cut off (e.g. by a page limit), so a missing listing proves nothing
    complete: bool = True


class Source(ABC):
    name: ClassVar[str]
    label: ClassVar[str]
    default_interval_minutes: ClassVar[int] = 30
    night_interval_minutes: ClassVar[int] = 60
    # Whether listing pages are read for furniture, parking and the ad's text
    has_details: ClassVar[bool] = False

    @abstractmethod
    def search_url(self, criteria: SearchCriteria) -> str:
        """The site's own search page for these criteria."""

    @abstractmethod
    async def fetch(
        self, page: Page, criteria: SearchCriteria, custom_url: str | None
    ) -> FetchResult:
        """Load the current listings. custom_url, when set, replaces search_url()."""

    async def fetch_details(self, page: Page, source_id: str, url: str) -> ListingDetails | None:
        """Read one listing's own page (None when it's gone). Called only if has_details."""
        return None


async def human_pause(low: float = 1.0, high: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(low, high))
