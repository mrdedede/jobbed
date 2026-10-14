"""RenderPool: bounded contexts, blocking, recovery, no real browser."""

import asyncio

import pytest

from job_scraper import render
from job_scraper.render import RenderPool

pytestmark = pytest.mark.anyio


class FakeCDP:
    async def send(self, *args):
        pass


class FakeRequest:
    def __init__(self, kind):
        self.resource_type = kind


class FakeRoute:
    def __init__(self, kind):
        self.request = FakeRequest(kind)
        self.outcome = None

    async def abort(self):
        self.outcome = "abort"

    async def continue_(self):
        self.outcome = "continue"


class FakeMouse:
    async def wheel(self, x, y):
        pass


class FakePage:
    def __init__(self, browser, context):
        self.browser, self.context = browser, context
        self.mouse = FakeMouse()
        self.url = None

    async def goto(self, url, **kwargs):
        self.url = url

        if self.browser.die_on and url in self.browser.die_on:
            self.browser.connected = False
            raise RuntimeError("Target closed")

        if url.endswith("/boom"):
            raise RuntimeError("navigation failed")

        await asyncio.sleep(0.01)

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def evaluate(self, js):
        return "stable"

    async def content(self):
        return f"<html>{self.url}</html>"


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.router = None
        self.closed = False

    async def route(self, pattern, handler):
        self.router = handler

    async def new_page(self):
        return FakePage(self.browser, self)

    async def new_cdp_session(self, page):
        return FakeCDP()

    async def close(self):
        self.closed = True
        self.browser.open_contexts -= 1


class FakeBrowser:
    def __init__(self, die_on=()):
        self.die_on = set(die_on)
        self.connected = True
        self.open_contexts = 0
        self.peak = 0
        self.contexts = []
        self.closed = False

    def is_connected(self):
        return self.connected

    async def new_context(self, **kwargs):
        self.open_contexts += 1
        self.peak = max(self.peak, self.open_contexts)
        context = FakeContext(self)
        self.contexts.append(context)

        return context

    async def close(self):
        self.closed = True


def pool_with(*browsers, **kwargs):
    queue = list(browsers)

    async def launch():
        return queue.pop(0)

    return RenderPool(launch=launch, **kwargs)


async def test_n_concurrent_renders_never_exceed_n_contexts():
    browser = FakeBrowser()

    async with pool_with(browser, contexts=3) as pool:
        results = await asyncio.gather(
            *(pool.render(f"https://h{n}.test/") for n in range(10)))

    assert results == [f"<html>https://h{n}.test/</html>" for n in range(10)]
    assert browser.peak == 3
    assert all(c.closed for c in browser.contexts)


async def test_a_failing_page_returns_none_and_closes_its_context():
    browser = FakeBrowser()

    async with pool_with(browser) as pool:
        assert await pool.render("https://a.test/boom") is None
        assert await pool.render("https://a.test/ok") is not None

    assert all(c.closed for c in browser.contexts)


async def test_a_killed_browser_is_relaunched_and_the_page_retried():
    first = FakeBrowser(die_on={"https://a.test/x"})
    second = FakeBrowser()

    async with pool_with(first, second) as pool:
        html = await pool.render("https://a.test/x")

    assert html == "<html>https://a.test/x</html>"
    assert first.closed


async def test_images_and_fonts_are_blocked():
    browser = FakeBrowser()

    async with pool_with(browser) as pool:
        await pool.render("https://a.test/")

    handler = browser.contexts[0].router
    image, document = FakeRoute("image"), FakeRoute("document")
    await handler(image)
    await handler(document)

    assert (image.outcome, document.outcome) == ("abort", "continue")


async def test_a_missing_browser_degrades_to_none():
    async def launch():
        raise ImportError("no playwright")

    async with RenderPool(launch=launch) as pool:
        assert await pool.render("https://a.test/") is None


async def test_settle_stops_at_the_ceiling_when_the_dom_never_settles():
    counter = iter(range(10_000))

    class Busy(FakePage):
        async def evaluate(self, js):
            return next(counter)

    waited = []

    class Timed(Busy):
        async def wait_for_timeout(self, ms):
            waited.append(ms)

    await render._settle(Timed(FakeBrowser(), None))

    assert sum(waited) == render.SETTLE_MAX_MS
