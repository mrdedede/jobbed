"""Playwright-backed Renderer for boards that draw their listing in JS.

Optional by design. Nothing in the scraper imports this module at startup --
only the `--render` flag on either CLI does, and Playwright itself is imported
inside the function. A machine with no browser installed runs the rest of the
project unchanged, which is the constraint requirements.txt records.

Install (not in requirements.txt):
    pip install playwright && playwright install chromium

Usage:
    from job_scraper.render import render
    Board(company, url, render=render)

`render` satisfies detector.Renderer -- Callable[[str], Optional[str]] -- so it
also feeds ATSDetector's existing rendered-retry path.
"""

from __future__ import annotations

from typing import Optional

#: DOMContentLoaded, not networkidle: several boards (decathlon, casino) keep
#: at least one connection open indefinitely -- an analytics beacon, a
#: websocket -- so networkidle never fires and render() returns None on a
#: board whose listing finished rendering seconds earlier. The settle delay
#: below is what actually waits for the listing's XHR.
WAIT_UNTIL = "domcontentloaded"

#: Fixed post-load settle, in milliseconds, before reading the DOM -- and
#: again, halved, after the scroll below.
SETTLE_MS = 3_500

#: Per-page ceiling in milliseconds. Playwright's own default is 30s; a board
#: that has not settled by then is not worth a longer wall-clock hit across a
#: hundred-board run.
TIMEOUT_MS = 30_000


def render(url: str, timeout: int = TIMEOUT_MS) -> Optional[str]:
    """Load a page in headless Chromium and return the settled HTML.

    Args:
        url: Page to load.
        timeout: Navigation timeout in milliseconds.

    Returns:
        Rendered HTML, or None if the browser is unavailable or the page
        never loaded.
    """
    try:
        # Local import: this module must be importable for its docstring
        # alone, and the flag branch that calls it is the only place that
        # needs Playwright present.
        from playwright.sync_api import sync_playwright

        with sync_playwright() as play:
            browser = play.chromium.launch()

            try:
                # service_workers="block" + CDP setCacheDisabled: a plain new
                # context/page still lets Chromium serve cached responses (disk
                # cache, or a site's own service worker) within this one load,
                # which is enough to make a re-scraped board look unchanged.
                context = browser.new_context(service_workers="block")
                page = context.new_page()
                cdp = context.new_cdp_session(page)
                cdp.send("Network.enable")
                cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
                page.goto(url, wait_until=WAIT_UNTIL, timeout=timeout)
                page.wait_for_timeout(SETTLE_MS)
                # ponytail: one fixed scroll + settle, not a real "wait until
                # the listing stops growing" loop. Upgrade path:
                # wait_for_selector on a per-ATS results container, or poll
                # DOM anchor count until it stops changing, if boards beyond
                # this batch still come back empty after render.
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(SETTLE_MS // 2)

                return page.content()
            finally:
                browser.close()
    except Exception:
        # Deliberately broad. Playwright raises its own error tree (timeouts,
        # missing browser binary, crashed page) and ImportError arrives here
        # too. A renderer failure has to degrade to "this board found no
        # jobs", never take down a hundred-board run.
        return None
