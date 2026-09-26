"""Connection to the long-running Chrome in the container (see docker/start-chrome.sh)."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

log = logging.getLogger(__name__)

SUPERVISOR_CONFIG = "/app/docker/supervisord.conf"


class BrowserUnavailable(Exception):
    pass


class BrowserManager:
    """Drives the real Chrome over CDP, so it keeps a normal fingerprint and its profile.

    Chrome is started and restarted by supervisord; this class only connects to it, so the
    browser survives app restarts and stays visible through noVNC.
    """

    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._session_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def stop(self) -> None:
        # Only drops the connection; Chrome itself keeps running
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _context(self) -> BrowserContext:
        async with self._connect_lock:
            if not self.connected:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        self.cdp_url, timeout=15_000
                    )
                except PlaywrightError as exc:
                    raise BrowserUnavailable(f"Chrome is not reachable at {self.cdp_url}") from exc
                log.info("Connected to Chrome %s", self._browser.version)
            return self._browser.contexts[0]

    async def version(self) -> str | None:
        try:
            await self._context()
        except BrowserUnavailable:
            return None
        return self._browser.version if self._browser else None

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Page]:
        """A new tab in the shared browser. Sessions run one at a time."""
        async with self._session_lock:
            page = await (await self._context()).new_page()
            try:
                yield page
            finally:
                with contextlib.suppress(PlaywrightError):
                    await page.close()

    async def restart(self) -> None:
        """Restart Chrome to release memory it accumulated over a long run."""
        async with self._session_lock:
            process = await asyncio.create_subprocess_exec(
                "supervisorctl", "-c", SUPERVISOR_CONFIG, "restart", "chrome",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            output, _ = await process.communicate()
            log.info("Restarted Chrome: %s", output.decode().strip())
            self._browser = None
