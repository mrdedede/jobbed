"""Tests for the empty-board diagnosis.

Each test pins one branch of `explain` by substring, not by exact wording:
these strings are prose for a human and are expected to be reworded.
"""

from __future__ import annotations

import httpx
import pytest

from job_scraper import diagnose

pytestmark = pytest.mark.anyio


class FakeResponse:
    def __init__(self, status=200, content_type="text/html", body=b""):
        self.status = status
        self.content_type = content_type
        self.body = body


class FakeSession(httpx.AsyncClient):
    """An AsyncClient that answers every request with one canned outcome."""

    def __init__(self, response=None, error=None):
        def answer(request):
            if error:
                raise error

            return httpx.Response(
                response.status, content=response.body,
                headers={"Content-Type": response.content_type},
            )

        super().__init__(transport=httpx.MockTransport(answer))


class FakeBoard:
    """A Board reduced to the four attributes `explain` reads."""

    def __init__(self, html=None, session=None, final_url=None, render=None,
                 ats=None):
        self.html = html
        self.session = session or FakeSession(FakeResponse())
        self.board_url = "https://example.test/careers"
        self.final_url = final_url or self.board_url
        self.url = self.final_url
        self.render = render
        self.ats = ats

    async def get_html(self):
        return self.html


def page(body: str = "", scripts: int = 0, head: str = "") -> str:
    tags = "<script>void 0;</script>" * scripts

    return f"<html><head>{head}{tags}</head><body>{body}</body></html>"


def links(count: int, path: str = "/about/page") -> str:
    return "".join(f'<a href="{path}-{n}">link {n}</a>' for n in range(count))


async def test_exception_is_the_explanation():
    reason = await diagnose.explain(FakeBoard(), httpx.ConnectTimeout("too slow"))

    assert "ConnectTimeout" in reason
    assert "too slow" in reason


async def test_non_200_is_reported_with_its_status():
    board = FakeBoard(session=FakeSession(FakeResponse(status=403)))

    assert "http 403" in await diagnose.explain(board)


async def test_non_textual_body_is_named():
    board = FakeBoard(
        session=FakeSession(FakeResponse(content_type="application/pdf"))
    )

    assert "non-textual" in await diagnose.explain(board)
    assert "application/pdf" in await diagnose.explain(board)


async def test_request_exception_reports_its_type():
    board = FakeBoard(session=FakeSession(error=httpx.ReadTimeout("slow")))

    assert "fetch failed: ReadTimeout" in await diagnose.explain(board)


async def test_spa_shell_reads_as_javascript_rendered():
    board = FakeBoard(html=page('<div id="root"></div>', scripts=40))
    reason = await diagnose.explain(board)

    assert "javascript-rendered" in reason
    assert "40 scripts" in reason


async def test_few_anchors_and_many_scripts_read_as_javascript_rendered():
    board = FakeBoard(html=page(links(2), scripts=diagnose.MANY_SCRIPTS))

    assert "javascript-rendered" in await diagnose.explain(board)


async def test_marketing_page_reads_as_no_job_shaped_links():
    board = FakeBoard(html=page(links(60)))
    reason = await diagnose.explain(board)

    assert "no job-shaped links" in reason
    assert "60 anchors" in reason


async def test_job_shaped_links_present_but_unread():
    board = FakeBoard(html=page(links(60, path="/jobs/senior-engineer")))

    assert "job-shaped links present" in await diagnose.explain(board)


async def test_job_shaped_link_outranks_the_spa_marker():
    """A real match on this fetch disproves the SPA-marker's own prediction
    that postings only arrive after render -- kering's shape: an id="__next"
    shell that also already carries real job-shaped anchors."""
    board = FakeBoard(html=page(
        '<div id="__next">' + links(60, path="/jobs/senior-engineer") + "</div>"
    ))

    assert "job-shaped links present" in await diagnose.explain(board)


async def test_empty_page_is_not_called_javascript():
    assert "empty body" in await diagnose.explain(FakeBoard(html="   "))


async def test_probe_falls_through_to_html_analysis():
    body = page(links(60)).encode("utf-8")
    board = FakeBoard(session=FakeSession(FakeResponse(body=body)))

    assert "no job-shaped links" in await diagnose.explain(board)


async def test_redirect_and_renderer_are_appended_as_context():
    board = FakeBoard(html=page(links(60)),
                      final_url="https://example.test/elsewhere",
                      render=lambda url: None)
    reason = await diagnose.explain(board)

    assert "redirected to https://example.test/elsewhere" in reason
    assert "renderer ran" in reason


async def test_long_redirect_url_is_truncated():
    long_url = "https://example.test/elsewhere?" + "a=1&" * 200
    board = FakeBoard(html=page(links(60)), final_url=long_url)
    reason = await diagnose.explain(board)

    assert len(reason) < len(long_url)
    assert reason.count("redirected to") == 1
    note = reason.split("redirected to ", 1)[1].rstrip(")")
    assert len(note) == diagnose.MAX_REDIRECT_LEN + len("...")
    assert note.endswith("...")
