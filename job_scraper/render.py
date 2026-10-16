"""Playwright-backed Renderer for pages that draw their content in JS.

Optional by design. Nothing in the scraper imports this module at startup --
only the `--render` flag does, and Playwright itself is imported when the
first page is rendered. A machine with no browser installed runs the rest of
the project unchanged, which is the constraint requirements.txt records.

Install (not in requirements.txt):
    pip install playwright && playwright install chromium

Usage:
    async with RenderPool() as pool:
        board = Board(company, url, render=pool.render)

One browser per pool, one context per page, and at most `contexts` pages in
flight: the browser is the expensive part (about 1.3 GB at 4 contexts) and a context is cheap. `pool.render` satisfies
detector.Renderer -- Callable[[str], Awaitable[Optional[str]]].
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

from job_scraper.fetching import polite

#: DOMContentLoaded, not networkidle: several boards (decathlon, casino) keep
#: at least one connection open indefinitely -- an analytics beacon, a
#: websocket -- so networkidle never fires and render() returns None on a
#: board whose listing finished rendering seconds earlier. `_settle` is what
#: waits for the listing's XHR instead.
WAIT_UNTIL = "domcontentloaded"

#: Per-page navigation ceiling in milliseconds.
TIMEOUT_MS = 30_000

#: Pages rendered at once. Memory-bound: 1.3 GB at 4, 1.7 GB at 6 (M5).
DEFAULT_CONTEXTS = 4

#: The page counts as settled once its DOM signature is unchanged for
#: STABLE_POLLS polls POLL_MS apart, and never waits past SETTLE_MAX_MS. The
#: fixed 3.5 s + 1.75 s this replaces was about 88% of per-page time.
POLL_MS = 250
STABLE_POLLS = 3
SETTLE_MAX_MS = 6_000

#: Resource types nobody reads the listing from.
BLOCKED_TYPES = {"image", "font", "media"}

#: Something that changes while a listing is being built and not after.
_SIGNATURE_JS = ("document.body ? document.body.innerHTML.length + ':' + "
                 "document.querySelectorAll('a').length : ''")

Launch = Callable[[], Awaitable[object]]


def launch_args() -> list:
    """Chromium flags for running in a container.

    /dev/shm is 64 MB in a default container and Chromium crashes on heavy
    pages; --disable-dev-shm-usage moves that to /tmp. The sandbox needs
    kernel features a default container lacks, so it is switched off only
    where the image says so (CHROMIUM_NO_SANDBOX, set in the scrape target).
    """
    args = ["--disable-dev-shm-usage"]

    if os.environ.get("CHROMIUM_NO_SANDBOX") == "1":
        args.append("--no-sandbox")

    return args


class RenderPool:
    """A shared browser with a bounded number of concurrent pages."""

    def __init__(self, contexts: int = DEFAULT_CONTEXTS,
                 timeout: int = TIMEOUT_MS,
                 launch: Optional[Launch] = None) -> None:
        """
        Args:
            contexts: Pages rendered concurrently.
            timeout: Navigation timeout in milliseconds.
            launch: Coroutine returning a Playwright-like browser. Defaults
                to launching headless Chromium; tests pass a fake.
        """
        self.timeout = timeout
        self._launch = launch or self._launch_chromium
        self._slots = asyncio.Semaphore(contexts)
        self._lock = asyncio.Lock()
        self._browser = None
        self._playwright = None

    async def __aenter__(self) -> "RenderPool":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _launch_chromium(self):
        # Local import: this module must be importable for its docstring
        # alone, and this is the only place that needs Playwright present.
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        return await self._playwright.chromium.launch(args=launch_args())

    async def _get_browser(self, stale=None):
        """The live browser, (re)launching it under the lock if needed.

        Args:
            stale: A browser a caller just saw die. Only that one is
                replaced, so concurrent callers share one relaunch.
        """
        async with self._lock:
            if self._browser is None or self._browser is stale:
                if self._browser is not None:
                    try:
                        await self._browser.close()
                    except Exception:
                        pass

                self._browser = await self._launch()

            return self._browser

    async def render(self, url: str) -> Optional[str]:
        """Load a page and return its settled HTML.

        Returns:
            Rendered HTML, or None if the browser is unavailable or the page
            never loaded. Deliberately broad: Playwright raises its own error
            tree (timeouts, missing binary, crashed page) and a renderer
            failure has to degrade to "this page gave nothing", never take
            down a long run.
        """
        async with self._slots:
            browser = None

            for attempt in range(2):
                try:
                    browser = await self._get_browser(stale=browser if attempt
                                                      else None)

                    return await self._render_once(browser, url)
                except Exception:
                    # Retry once, and only if the browser itself went away;
                    # a page that just fails stays failed.
                    if (attempt or browser is None
                            or browser.is_connected()):
                        return None

            return None

    async def _render_once(self, browser, url: str) -> str:
        context = await browser.new_context(service_workers="block")

        try:
            await context.route("**/*", _block_heavy)
            page = await context.new_page()
            # CDP cache off: a plain context still lets Chromium serve cached
            # responses within one load, which makes a re-scraped board look
            # unchanged.
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})

            # Politeness is per host, as for plain fetches; only the load is
            # gated, not the settle.
            async with polite(url):
                await page.goto(url, wait_until=WAIT_UNTIL,
                                timeout=self.timeout)

            await _settle(page)
            await page.mouse.wheel(0, 4000)
            await _settle(page)

            # TODO(pagination): this reads the first page of results only.
            # Candidates: follow "next" links, click "load more", or capture
            # the JSON the page itself fetches and hand it to the feed
            # strategy. M5's boards stopped at exactly 25 jobs.
            return await page.content()
        finally:
            await context.close()

    async def aclose(self) -> None:
        """Close the browser. Safe to call twice."""
        async with self._lock:
            browser, self._browser = self._browser, None

            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

            if self._playwright is not None:
                playwright, self._playwright = self._playwright, None
                await playwright.stop()


async def _block_heavy(route) -> None:
    if route.request.resource_type in BLOCKED_TYPES:
        await route.abort()
    else:
        await route.continue_()


async def _settle(page) -> None:
    """Wait until the DOM stops changing, within SETTLE_MAX_MS."""
    last, stable, waited = None, 0, 0

    while waited < SETTLE_MAX_MS and stable < STABLE_POLLS:
        await page.wait_for_timeout(POLL_MS)
        waited += POLL_MS
        signature = await page.evaluate(_SIGNATURE_JS)
        stable = stable + 1 if signature == last else 0
        last = signature
