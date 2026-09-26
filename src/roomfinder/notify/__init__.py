import logging
from typing import Protocol

from roomfinder.models import Listing

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def new_listing(self, listing: Listing, *, silent: bool) -> None: ...

    async def price_drop(
        self, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None: ...

    async def message(self, text: str, *, silent: bool = False) -> None: ...


class LogNotifier:
    """Used when Telegram isn't configured: notifications only go to the log."""

    async def new_listing(self, listing: Listing, *, silent: bool) -> None:
        log.info("New listing: %s %s", listing.source, listing.url)

    async def price_drop(
        self, listing: Listing, old_price: int, new_price: int, *, silent: bool
    ) -> None:
        log.info("Price drop %s -> %s: %s", old_price, new_price, listing.url)

    async def message(self, text: str, *, silent: bool = False) -> None:
        log.info("Message: %s", text)
