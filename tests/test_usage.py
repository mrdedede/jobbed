"""Daily LLM quota: db.usage and the 429 on /analyze."""

from concurrent.futures import ThreadPoolExecutor

from api_helpers import as_user, make_user

from api.routers import user as user_router
from db import usage
from test_api_analyze import ID, analyze, chat, client, posting  # noqa: F401
from db import jobs


def test_reserve_grants_up_to_cap_then_nothing(db):
    uid = make_user()

    assert usage.reserve(uid, "analysis", 6, 10) == 6
    assert usage.reserve(uid, "analysis", 6, 10) == 4
    assert usage.reserve(uid, "analysis", 1, 10) == 0


def test_refund_gives_units_back_but_never_below_zero(db):
    uid = make_user()
    usage.reserve(uid, "analysis", 5, 10)

    usage.refund(uid, "analysis", 3)
    usage.refund(uid, "analysis", 99)

    assert usage.reserve(uid, "analysis", 10, 10) == 10


def test_kinds_are_counted_separately(db):
    uid = make_user()
    usage.reserve(uid, usage.KIND_ANALYSIS, 10, 10)

    assert usage.reserve(uid, usage.KIND_CV, 2, 2) == 2


def test_concurrent_reserves_never_exceed_cap(db):
    uid = make_user()

    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(
            lambda _: usage.reserve(uid, "analysis", 1, 3), range(8)))

    assert sum(got) == 3


def test_analyze_429_when_cap_reached_and_unused_units_refunded(
        client, chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(user_router, "DAILY_ANALYSIS_CAP", 1)
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Go Two", "go " * 6)])
    uid = make_user("go")

    first = analyze(client, uid)
    second = analyze(client, uid)

    assert first.status_code == 200 and first.json()["analysed"] == 1
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


def test_analyze_refunds_when_nothing_was_graded(client, chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(user_router, "DAILY_ANALYSIS_CAP", 1)
    uid = make_user("go")  # an unknown posting: nothing to grade

    assert analyze(client, uid, [ID]).json()["analysed"] == 0
    assert usage.reserve(uid, usage.KIND_ANALYSIS, 1, 1) == 1
