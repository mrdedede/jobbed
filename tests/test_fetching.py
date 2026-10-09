"""Async fetching layer: retry, size cap, charset and per-host politeness."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from job_scraper import fetching
from job_scraper.fetching import fetch, fetch_json

pytestmark = pytest.mark.anyio


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             follow_redirects=True)


async def test_a_transient_503_is_retried_until_it_succeeds():
    statuses = iter([503, 503, 200])
    calls = []

    def handler(request):
        calls.append(request.url)
        status = next(statuses)

        return httpx.Response(status, text="ok",
                              headers={"Content-Type": "text/html"})

    async with client_for(handler) as client:
        assert await fetch(client, "https://a.test/") == "ok"

    assert len(calls) == 3


async def test_retry_after_is_honoured_on_429(monkeypatch):
    slept = []
    real_sleep = asyncio.sleep

    async def spy(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(fetching.asyncio, "sleep", spy)
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                    httpx.Response(200, text="ok")])

    async with client_for(lambda r: next(answers)) as client:
        assert await fetch(client, "https://a.test/") == "ok"

    assert 7 in slept


async def test_a_404_is_none_and_not_retried():
    calls = []

    def handler(request):
        calls.append(1)

        return httpx.Response(404)

    async with client_for(handler) as client:
        assert await fetch(client, "https://a.test/") is None

    assert len(calls) == 1


async def test_a_persistent_503_ends_as_none():
    async with client_for(lambda r: httpx.Response(503)) as client:
        assert await fetch(client, "https://a.test/") is None


async def test_a_transport_error_ends_as_none():
    def handler(request):
        raise httpx.ConnectError("boom")

    async with client_for(handler) as client:
        assert await fetch(client, "https://a.test/") is None


async def test_the_body_is_cut_at_max_bytes():
    async with client_for(
        lambda r: httpx.Response(200, text="x" * 1000)
    ) as client:
        assert len(await fetch(client, "https://a.test/", max_bytes=10)) == 10


async def test_a_non_textual_body_is_refused():
    async with client_for(lambda r: httpx.Response(
        200, content=b"%PDF", headers={"Content-Type": "application/pdf"}
    )) as client:
        assert await fetch(client, "https://a.test/") is None


async def test_undeclared_charset_decodes_as_utf8_not_latin1():
    body = "Développeur".encode()

    async with client_for(lambda r: httpx.Response(
        200, content=body, headers={"Content-Type": "text/html"}
    )) as client:
        assert await fetch(client, "https://a.test/") == "Développeur"


async def test_fetch_json_parses_and_tolerates_garbage():
    pages = {"/ok": '{"a": 1}', "/bad": "nope"}

    async with client_for(lambda r: httpx.Response(
        200, text=pages[r.url.path], headers={"Content-Type": "text/plain"}
    )) as client:
        assert await fetch_json(client, "https://a.test/ok") == {"a": 1}
        assert await fetch_json(client, "https://a.test/bad") is None


async def test_one_host_never_exceeds_per_host_in_flight(monkeypatch):
    monkeypatch.setattr(fetching, "PER_HOST", 2)
    live = {"now": 0, "peak": 0}

    async def handler(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1

        return httpx.Response(200, text="ok")

    async with client_for(handler) as client:
        await asyncio.gather(*(fetch(client, f"https://a.test/{n}")
                               for n in range(10)))

    assert live["peak"] == 2


async def test_different_hosts_run_side_by_side(monkeypatch):
    live = {"now": 0, "peak": 0}

    async def handler(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1

        return httpx.Response(200, text="ok")

    async with client_for(handler) as client:
        await asyncio.gather(*(fetch(client, f"https://h{n}.test/")
                               for n in range(8)))

    assert live["peak"] == 8


async def test_a_declared_charset_is_still_honoured():
    async with client_for(lambda r: httpx.Response(
        200, content="Développeur".encode("latin-1"),
        headers={"Content-Type": "text/html; charset=latin-1"},
    )) as client:
        assert await fetch(client, "https://a.test/") == "Développeur"
