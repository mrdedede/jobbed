"""Tests for the empty-board diagnosis.

Each test pins one branch of `explain` by substring, not by exact wording:
these strings are prose for a human and are expected to be reworded.
"""

from __future__ import annotations

import httpx
import pytest

from job_scraper import diagnose
from job_scraper.scrape_boards import JS_SHELL_REASONS

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
                 ats=None, rendered_html=None, render_ran=False):
        self.html = html
        self.rendered_html = rendered_html
        self.render_ran = render_ran
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


async def test_a_bot_refusing_platform_is_named_and_kept_out_of_rendering():
    board = FakeBoard(html=page(links(60, path="/jobs/senior-engineer")))
    board.board_url = "https://www.apec.fr/candidat/recherche-emploi.html"
    reason = await diagnose.explain(board)

    assert "bot-protected platform (apec)" in reason
    assert not reason.startswith(JS_SHELL_REASONS)


async def test_job_shaped_links_in_the_menus_do_not_count_as_present():
    """Capgemini: its only job-shaped links are menu links scrape_links now
    skips, and "links present" would keep it out of RenderBoards."""
    board = FakeBoard(html=page(
        "<nav>" + links(60, path="/careers/why-join-us") + "</nav>"
        '<div id="__next"></div>'
    ))

    assert "likely javascript-rendered" in await diagnose.explain(board)


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


async def test_redirect_is_appended_as_context():
    board = FakeBoard(html=page(links(60)),
                      final_url="https://example.test/elsewhere",
                      render=lambda url: None)
    reason = await diagnose.explain(board)

    assert "redirected to https://example.test/elsewhere" in reason
    # A configured renderer is not a render: only the cause may say one ran.
    assert "render" not in reason


async def test_a_render_that_returned_no_page_says_so():
    board = FakeBoard(html=page(links(60)), render_ran=True)

    assert (await diagnose.explain(board)).startswith("render failed")


async def test_a_rendered_page_is_judged_not_the_served_one():
    """The served shell had 0 anchors; the browser drew 60, none a posting.
    Its SPA marker survives the render, so it must not read as JS-built."""
    board = FakeBoard(html=page('<div id="__next"></div>'), render_ran=True,
                      rendered_html=page('<div id="__next">' + links(60)
                                         + "</div>"))
    reason = await diagnose.explain(board)

    assert reason.startswith("after render: no job-shaped links (60 anchors")
    assert "javascript-rendered" not in reason


#: The body APEC served on 2026-10-27, trimmed: its only words are about
#: JS and ad blockers, so it is known by the DataDome host it loads.
DATADOME_PAGE = (
    '<html lang="en"><head><title>apec.fr</title></head><body>'
    '<p id="cmsg">Please enable JS and disable any ad blocker</p>'
    '<script src="https://ct.captcha-delivery.com/c.js"></script>'
    "</body></html>"
)


async def test_a_bot_check_after_render_is_named_blocked():
    board = FakeBoard(render_ran=True, rendered_html=DATADOME_PAGE)

    assert (await diagnose.explain(board)
            == "blocked after render (apec.fr)")


async def test_evidence_names_listing_iframes_but_not_trackers():
    board = FakeBoard(render_ran=True, rendered_html=page(
        '<iframe src="https://www.googletagmanager.com/ns.html"></iframe>'
        '<iframe src="https://widget.jarvi.tech/offers?c=tse"></iframe>'))

    facts = await diagnose.evidence(board)

    assert "iframes: widget.jarvi.tech" in facts
    assert "googletagmanager" not in facts


async def test_long_redirect_url_is_truncated():
    long_url = "https://example.test/elsewhere?" + "a=1&" * 200
    board = FakeBoard(html=page(links(60)), final_url=long_url)
    reason = await diagnose.explain(board)

    assert len(reason) < len(long_url)
    assert reason.count("redirected to") == 1
    note = reason.split("redirected to ", 1)[1].rstrip(")")
    assert len(note) == diagnose.MAX_REDIRECT_LEN + len("...")
    assert note.endswith("...")


async def test_evidence_names_ats_token_near_misses_and_no_openings():
    """The shapes no_jobs triage kept needing by hand: an ATS embed with a
    readable token, a posting URL JOB_URL_RE turns down, a mailto posting."""
    board = FakeBoard(html=page(
        '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">'
        "</iframe>"
        '<a href="https://www.jobposting.pro/emploi-2731999-999">Dev</a>'
        '<a href="mailto:rh@acme.test?subject=Dev (H/F)">Dev</a>'
        "<p>Aucune offre pour le moment</p>"
    ))
    facts = await diagnose.evidence(board)

    assert "ats refs: greenhouse" in facts
    assert "greenhouse=acme" in facts
    assert "/emploi-2731999-999" in facts
    assert "1 mailto postings" in facts
    assert "no openings" in facts


async def test_evidence_is_empty_without_a_page():
    assert await diagnose.evidence(FakeBoard()) == ""
