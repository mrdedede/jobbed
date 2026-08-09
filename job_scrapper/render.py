"""Playwright-backed Renderer for boards that draw their listing in JS.

Optional by design. Nothing in the scraper imports this module at startup --
only the `--render` flag on either CLI does, and Playwright itself is imported
inside the function. A machine with no browser installed runs the rest of the
project unchanged, which is the constraint requirements.txt records.

Install (not in requirements.txt):
    pip install playwright && playwright install chromium

Usage:
    from job_scrapper.render import render
    Board(company, url, render=render)

`render` satisfies detector.Renderer -- Callable[[str], Optional[str]] -- so it
also feeds ATSDetector's existing rendered-retry path.
"""

from __future__ import annotations

from typing import Optional

#: Wait for the network to settle rather than for DOMContentLoaded: the whole
#: reason to spend a browser on these boards is the XHR that fills the listing,
#: and that fires after the document is already "loaded".
WAIT_UNTIL = "networkidle"

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
                page = browser.new_page()
                page.goto(url, wait_until=WAIT_UNTIL, timeout=timeout)

                return page.content()
            finally:
                browser.close()
    except Exception:
        # Deliberately broad. Playwright raises its own error tree (timeouts,
        # missing browser binary, crashed page) and ImportError arrives here
        # too. A renderer failure has to degrade to "this board found no
        # jobs", never take down a hundred-board run.
        return None
