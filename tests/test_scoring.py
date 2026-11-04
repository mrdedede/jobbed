"""Tests for ai/scoring.py: the arithmetic behind a grade."""

from datetime import date

import pytest

from ai import scoring

TODAY = date(2026, 11, 15)


def req(status="met", importance="required", kind="skill"):
    return {"text": "x", "kind": kind, "importance": importance,
            "status": status, "evidence": "e", "quote": "q"}


def job(start="2020-01", end="2020-12", relevant=True):
    return {"company": "C", "role": "R", "start": start, "end": end,
            "relevant": relevant}


def assessment(requirements=None, required_years=None, experiences=None,
               onsite_country=None):
    return {"requirements": [req()] if requirements is None else requirements,
            "required_years": required_years, "experiences": experiences or [],
            "onsite_country": onsite_country}


def grade(*args, ats=None, **kwargs):
    return scoring.score(assessment(*args, **kwargs), ats, TODAY)["grade"]


# relevant_years

@pytest.mark.parametrize("jobs, years", [
    ([job("2020-01", "2020-12")], 1),                         # both ends count
    ([job("2020-01", "2020-12"), job("2020-07", "2021-06")], 1.5),  # overlap once
    ([job("2020-01", "2021-12"), job("2020-03", "2020-06")], 2),    # one inside another
    ([job("2020-01", "2020-06"), job("2020-07", "2020-12")], 1),    # adjoining
    ([job("2020-01", "2020-12"), job("2022-01", "2022-12")], 2),    # a gap adds nothing
    ([job("2020-01", "2020-12", relevant=False)], 0),
    ([job("2026-01", None)], 11 / 12),                        # ongoing, to today
    ([job("2026-01", "2030-12")], 11 / 12),                   # future end, capped
    ([], 0),
])
def test_relevant_years(jobs, years):
    assert scoring.relevant_years(jobs, TODAY) == pytest.approx(years)


@pytest.mark.parametrize("bad", [
    job("January 2020", "2020-12"),
    job("2020-13", "2020-12"),
    job("2021-01", "2020-12"),
])
def test_relevant_years_rejects_bad_dates(bad):
    with pytest.raises(ValueError):
        scoring.relevant_years([bad], TODAY)


def test_irrelevant_roles_are_not_even_parsed():
    assert scoring.relevant_years(
        [job("garbage", "garbage", relevant=False)], TODAY) == 0


# the base score

def test_required_weighs_three_times_preferred():
    assert grade([req(), req("missing", "preferred")]) == 75
    assert grade([req("missing"), req("met", "preferred")]) == 25


def test_no_requirements_is_ungraded_not_zero():
    out = scoring.score(assessment(requirements=[]), "workday", TODAY)

    assert out["grade"] is None and out["fit"] is None


# years of experience

@pytest.mark.parametrize("months, expected", [
    (60, 100),  # exactly the minimum
    (54, 100),  # exactly 90%: the tolerance
    (53, 80),   # just under: minus 10, then the shortfall cap
    (45, 80),   # exactly 75%
    (44, 80),   # minus 20, still at the cap
    (30, 80),   # exactly 50%
    (29, 65),   # minus 35
])
def test_years_deduction_boundaries(months, expected):
    start = 2026 * 12 + 10 - months  # months of Oct 2026 and the ones before it
    experience = job(f"{start // 12}-{start % 12 + 1:02d}", "2026-10")

    assert scoring.relevant_years([experience], TODAY) == pytest.approx(months / 12)
    assert grade(required_years=5, experiences=[experience]) == expected


@pytest.mark.parametrize("months, deduction", [(53, 10), (44, 20), (29, 35)])
def test_years_deduction_shows_below_the_cap(months, deduction):
    start = 2026 * 12 + 10 - months
    experience = job(f"{start // 12}-{start % 12 + 1:02d}", "2026-10")

    # 50 from the requirements, so the 80 cap plays no part.
    assert grade([req(), req("missing")], required_years=5,
                 experiences=[experience]) == 50 - deduction


def test_no_required_years_subtracts_nothing():
    assert grade(required_years=None, experiences=[]) == 100


def test_more_years_than_asked_earns_no_bonus():
    assert grade([req("missing"), req()], required_years=1,
                 experiences=[job("2010-01", None)]) == 50


def test_any_shortfall_caps_a_perfect_match_at_80():
    assert grade(required_years=5, experiences=[job("2022-01", "2025-12")]) == 80


def test_the_deduction_comes_off_a_lower_score():
    # 50 from the requirements, minus 35 for no experience at all.
    assert grade([req("missing"), req()], required_years=5) == 15


# language and certification

def test_a_missing_required_language_caps_the_grade():
    requirements = [req(), req(), req(), req("missing", kind="language")]

    assert grade(requirements) == 40


def test_a_missing_preferred_language_does_not_cap():
    assert grade([req(), req("missing", "preferred", "language")]) == 75


def test_a_missing_certification_loses_extra_points_and_says_why():
    requirements = [req(), req(), req(), req("missing", kind="certification")]
    out = scoring.score(assessment(requirements), None, TODAY)

    assert out["grade"] == 65
    assert any("certification" in note for note in out["notes"])


def test_the_grade_never_goes_below_zero():
    assert grade([req("missing", kind="certification")], required_years=5) == 0


# ATS policies

def test_workday_caps_a_missing_required_qualification_others_do_not():
    requirements = [req(), req(), req(), req("missing")]

    assert grade(requirements) == 75
    assert grade(requirements, ats="workday") == 60
    assert grade(requirements, ats="successfactors") == 75
    assert grade(requirements, ats="greenhouse") == 75
    assert grade(requirements, ats="smartrecruiters") == 75


def test_workday_cap_is_explained_only_when_it_applied():
    capped = scoring.score(assessment([req(), req(), req(), req("missing")]),
                           "workday", TODAY)
    below = scoring.score(assessment([req(), req("missing")]), "workday", TODAY)

    assert any("Workday" in note for note in capped["notes"])
    assert not any("Workday" in note for note in below["notes"])


def test_a_missing_preferred_qualification_never_triggers_the_workday_cap():
    assert grade([req(), req(), req(), req("missing", "preferred")],
                 ats="workday") == 90


@pytest.mark.parametrize("ats, expected", [
    ("workday", 30), ("successfactors", 70), ("greenhouse", 50),
    ("smartrecruiters", 50), (None, 50), ("lever", 50), ("", 50),
])
def test_transferable_credit_depends_on_the_ats(ats, expected):
    assert grade([req("transferable")], ats=ats) == expected


@pytest.mark.parametrize("ats", ["workday", "successfactors", "lever", None])
def test_fit_does_not_depend_on_the_ats(ats):
    out = scoring.score(assessment([req("transferable"), req("missing")]),
                        ats, TODAY)

    assert out["fit"] == 25


def test_every_policy_is_complete():
    for policy in scoring.POLICIES.values():
        assert set(policy) == {"transferable", "missing_required_cap"}


# what must not affect the grade

def test_being_on_site_abroad_is_a_note_not_a_penalty():
    out = scoring.score(assessment(onsite_country="Germany"), None, TODAY)

    assert out["grade"] == 100
    assert any("Germany" in note for note in out["notes"])
