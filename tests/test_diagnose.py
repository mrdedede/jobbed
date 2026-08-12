"""Tests for the empty-board diagnosis.

Each test pins one branch of `explain` by substring, not by exact wording:
these strings are prose for a human and are expected to be reworded.
"""

from __future__ import annotations

import requests

from job_scraper import diagnose


class FakeRaw:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, *args, **kwargs) -> bytes:
        return self.body


class FakeResponse:
    """Enough of a requests.Response for `_probe`, including the with-block."""

    def __init__(self, status=200, content_type="text/html", body=b""):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.raw = FakeRaw(body)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error

        return self.response


class FakeBoard:
    """A Board reduced to the four attributes `explain` reads."""

    def __init__(self, html=None, session=None, final_url=None, render=None):
        self.html = html
        self.session = session or FakeSession()
        self.board_url = "https://example.test/careers"
        self.final_url = final_url or self.board_url
        self.url = self.final_url
        self.render = render


def page(body: str = "", scripts: int = 0, head: str = "") -> str:
    tags = "<script>void 0;</script>" * scripts

    return f"<html><head>{head}{tags}</head><body>{body}</body></html>"


def links(count: int, path: str = "/about/page") -> str:
    return "".join(f'<a href="{path}-{n}">link {n}</a>' for n in range(count))


def test_exception_is_the_explanation():
    reason = diagnose.explain(FakeBoard(), requests.ConnectTimeout("too slow"))

    assert "ConnectTimeout" in reason
    assert "too slow" in reason


def test_non_200_is_reported_with_its_status():
    board = FakeBoard(session=FakeSession(FakeResponse(status=403)))

    assert "http 403" in diagnose.explain(board)


def test_non_textual_body_is_named():
    board = FakeBoard(
        session=FakeSession(FakeResponse(content_type="application/pdf"))
    )

    assert "non-textual" in diagnose.explain(board)
    assert "application/pdf" in diagnose.explain(board)


def test_request_exception_reports_its_type():
    board = FakeBoard(session=FakeSession(error=requests.ReadTimeout()))

    assert "fetch failed: ReadTimeout" in diagnose.explain(board)


def test_spa_shell_reads_as_javascript_rendered():
    board = FakeBoard(html=page('<div id="root"></div>', scripts=40))
    reason = diagnose.explain(board)

    assert "javascript-rendered" in reason
    assert "40 scripts" in reason


def test_few_anchors_and_many_scripts_read_as_javascript_rendered():
    board = FakeBoard(html=page(links(2), scripts=diagnose.MANY_SCRIPTS))

    assert "javascript-rendered" in diagnose.explain(board)


def test_marketing_page_reads_as_no_job_shaped_links():
    board = FakeBoard(html=page(links(60)))
    reason = diagnose.explain(board)

    assert "no job-shaped links" in reason
    assert "60 anchors" in reason


def test_job_shaped_links_present_but_unread():
    board = FakeBoard(html=page(links(60, path="/jobs/senior-engineer")))

    assert "job-shaped links present" in diagnose.explain(board)


def test_job_shaped_link_outranks_the_spa_marker():
    """A real match on this fetch disproves the SPA-marker's own prediction
    that postings only arrive after render -- kering's shape: an id="__next"
    shell that also already carries real job-shaped anchors."""
    board = FakeBoard(html=page(
        '<div id="__next">' + links(60, path="/jobs/senior-engineer") + "</div>"
    ))

    assert "job-shaped links present" in diagnose.explain(board)


def test_empty_page_is_not_called_javascript():
    assert "empty body" in diagnose.explain(FakeBoard(html="   "))


def test_probe_falls_through_to_html_analysis():
    body = page(links(60)).encode("utf-8")
    board = FakeBoard(session=FakeSession(FakeResponse(body=body)))

    assert "no job-shaped links" in diagnose.explain(board)


def test_redirect_and_renderer_are_appended_as_context():
    board = FakeBoard(html=page(links(60)),
                      final_url="https://example.test/elsewhere",
                      render=lambda url: None)
    reason = diagnose.explain(board)

    assert "redirected to https://example.test/elsewhere" in reason
    assert "renderer ran" in reason
