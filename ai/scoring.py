"""Turn one posting's assessment into a grade. Pure: no model, no database.

The model reports facts (`ai.analysis.ANALYSIS_SCHEMA`); this does the
arithmetic, so the same facts always give the same grade and a policy change
can regrade stored assessments without another model call.

Two numbers come out. `fit` always uses the default policy, so it says how
well the candidate matches. `grade` uses the posting's ATS, so it says how they
are likely to fare in that system's screening. Keeping both shows, once there
are application outcomes to compare against, whether the ATS rules help.
"""

from datetime import date
from typing import List, Optional, Tuple

#: Credit for a transferable requirement, and the grade cap when a required
#: qualification is missing (None: it only loses its points). Starting guesses,
#: to calibrate against application outcomes. Workday's cap follows HiredScore:
#: meeting every basic qualification is what earns an A or B.
POLICIES = {
    "default": {"transferable": 0.5, "missing_required_cap": None},
    "workday": {"transferable": 0.3, "missing_required_cap": 60},
    "successfactors": {"transferable": 0.7, "missing_required_cap": None},
    "greenhouse": {"transferable": 0.5, "missing_required_cap": None},
    "smartrecruiters": {"transferable": 0.5, "missing_required_cap": None},
}

WEIGHTS = {"required": 3, "preferred": 1}

#: (lowest ratio of CV years to required years, points subtracted), best first.
#: 0.90 is the agreed tolerance: within 10% of the minimum costs nothing.
YEARS_DEDUCTIONS = ((0.90, 0), (0.75, 10), (0.50, 20), (0.0, 35))

#: "81-100" means every hard requirement is met, so a shortfall stays below it.
YEARS_SHORTFALL_CAP = 80

#: A recruiter's filter that a cover letter cannot talk around.
MISSING_LANGUAGE_CAP = 40

#: Small, on top of the requirement's own points: the CV may just not list it.
MISSING_CERTIFICATION_PENALTY = 10


def _months(text: str) -> int:
    """A "YYYY-MM" date as a month count. Raises ValueError on anything else."""
    year, month = text.split("-")

    if not 1 <= int(month) <= 12:
        raise ValueError(f"not a YYYY-MM date: {text!r}")

    return int(year) * 12 + int(month) - 1


def relevant_years(experiences: List[dict], today: date) -> float:
    """Years in relevant roles, overlaps counted once, both end months included.

    Args:
        experiences: `experiences` from the assessment. `end` is None for an
            ongoing role.
        today: Ends an ongoing role, and caps an end date in the future.

    Returns:
        The total, in years.

    Raises:
        ValueError: A date is malformed, or a role ends before it starts.
    """
    now = today.year * 12 + today.month - 1
    periods = []

    for experience in experiences:
        if not experience["relevant"]:
            continue

        start = _months(experience["start"])
        end = _months(experience["end"]) if experience["end"] else now

        if end < start:
            raise ValueError(f"role ends before it starts: {experience!r}")

        periods.append((start, min(end, now)))

    total = 0
    reached = None

    for start, end in sorted(periods):
        # Next free month: an adjoining or overlapping role adds only what is new.
        first = start if reached is None else max(start, reached)
        total += max(0, end + 1 - first)
        reached = end + 1 if reached is None else max(reached, end + 1)

    return total / 12


def _years_deduction(years: float, required: Optional[float]) -> int:
    if not required:
        return 0

    # Rounded so exactly 90% of a minimum is not lost to float error.
    ratio = round(years / required, 4)

    return next(points for floor, points in YEARS_DEDUCTIONS if ratio >= floor)


def _grade(assessment: dict, years: float, policy: dict) -> Tuple[int, List[str]]:
    """One policy's grade, and the notes that explain what limited it."""
    requirements = assessment["requirements"]
    credit = {"met": 1, "transferable": policy["transferable"], "missing": 0}
    earned = sum(WEIGHTS[r["importance"]] * credit[r["status"]] for r in requirements)
    possible = sum(WEIGHTS[r["importance"]] for r in requirements)
    grade = 100 * earned / possible
    notes = []
    missing = [r for r in requirements
               if r["importance"] == "required" and r["status"] == "missing"]

    deduction = _years_deduction(years, assessment["required_years"])

    if deduction:
        grade = min(grade - deduction, YEARS_SHORTFALL_CAP)
        notes.append(f"Experience: {years:.1f} years against the "
                     f"{assessment['required_years']:g} asked for, "
                     f"minus {deduction} points.")

    if any(r["kind"] == "certification" for r in missing):
        grade -= MISSING_CERTIFICATION_PENALTY
        notes.append(f"Required certification not shown, minus "
                     f"{MISSING_CERTIFICATION_PENALTY} points. If you hold it, "
                     f"make sure your CV lists it.")

    if any(r["kind"] == "language" for r in missing):
        grade = min(grade, MISSING_LANGUAGE_CAP)
        notes.append(f"A required language is not shown at the level asked "
                     f"for, so the grade is capped at {MISSING_LANGUAGE_CAP}.")

    cap = policy["missing_required_cap"]

    if cap is not None and missing and grade > cap:
        grade = cap
        notes.append(f"Capped at {cap}: a required qualification is missing. "
                     f"Where this employer uses Workday's AI grading, that "
                     f"places a candidate below the top bands.")

    return max(0, round(grade)), notes


def score(assessment: dict, ats: Optional[str], today: date) -> dict:
    """Grade one assessment.

    Args:
        assessment: `requirements`, `required_years`, `experiences` and
            `onsite_country`, as the model reported them.
        ats: The posting's detected ATS, as stored in job_postings.ats. Any
            other value, or None, gets the default policy.
        today: Ends ongoing roles.

    Returns:
        {"grade": int or None, "fit": int or None, "years": float,
        "notes": list of str}. Both grades are None when the assessment has no
        requirements: there is nothing to grade against, which is not a 0.

    Raises:
        ValueError: A date in `experiences` is malformed.
    """
    years = relevant_years(assessment["experiences"], today)

    if not assessment["requirements"]:
        return {"grade": None, "fit": None, "years": years, "notes": []}

    grade, notes = _grade(assessment, years,
                          POLICIES.get(ats, POLICIES["default"]))
    fit, _ = _grade(assessment, years, POLICIES["default"])

    if assessment["onsite_country"]:
        notes.append(f"This role is on-site in {assessment['onsite_country']}, "
                     f"a different country from the one on your CV.")

    return {"grade": grade, "fit": fit, "years": years, "notes": notes}
