"""The one piece of real logic in the Streamlit layer: reason parsing.

`common.parse_reasons` reads back what `diagnose.explain` wrote. The two are
only joined by a string format, so this test pins that format's shapes -- an
under-greedy pattern does not fail loudly, it just leaves debris on the cause
and multiplies the categories on the chart.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

# What `streamlit run visualization/Home.py` does: the app directory is on the
# path, so pages can `import common`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "visualization"))

common = pytest.importorskip("common", reason="streamlit not installed")


def parse(reason):
    """Parse one reason line and return it as a plain dict."""
    return common.parse_reasons(pd.Series([reason])).iloc[0].to_dict()


def test_reads_the_anchor_and_script_counts():
    parsed = parse("no job-shaped links (72 anchors, 51 scripts)")

    assert parsed["cause"] == "no job-shaped links"
    assert (parsed["anchors"], parsed["scripts"]) == (72.0, 51.0)
    assert pd.isna(parsed["marker"])


def test_keeps_the_spa_marker_out_of_the_cause():
    parsed = parse("likely javascript-rendered (6 anchors, 51 scripts, "
                   'id="__next")')

    assert parsed["cause"] == "likely javascript-rendered"
    assert parsed["marker"] == 'id="__next"'


def test_reads_the_redirect_tail():
    parsed = parse("no links on the page (0 anchors, 1 scripts) "
                   "(redirected to https://example.com/challenge)")

    assert parsed["cause"] == "no links on the page"
    assert parsed["anchors"] == 0.0
    assert parsed["redirect"] == "https://example.com/challenge"


def test_survives_a_reason_with_no_counts_at_all():
    parsed = parse("fetch failed: SSLError")

    assert parsed["cause"] == "fetch failed: SSLError"
    assert pd.isna(parsed["anchors"])


def test_every_recorded_reason_parses_to_a_known_cause():
    """Guard against a new diagnose format silently becoming its own cause."""
    from job_scraper import paths

    if not paths.NO_JOBS_CSV.exists():
        pytest.skip("no no_jobs.csv on this machine")

    causes = common.parse_reasons(
        pd.read_csv(paths.NO_JOBS_CSV)["reason"]
    )["cause"]

    assert not causes.str.contains(r"\(").any(), (
        f"unparsed tail left on: {causes[causes.str.contains(r'\(')].iloc[0]}"
    )
