"""Shared helpers for the Streamlit pages: loading, charting, launching.

Three jobs, one file, because every page needs all three and none of them is
big enough to own a module:

* reading the pipeline's CSV files without re-reading them on every rerun,
* one chart style, so five pages do not each invent their own,
* running a pipeline stage with its progress streamed into the page.

The workflows run inline in the Streamlit script rather than as a subprocess.
Every stage function already accepts an `on_progress`/`on_board` callback --
that seam is why (see the module docstring of job_scraper/pipeline.py) -- and
the cost is that the run dies with the browser tab.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

# Streamlit puts the *script's* directory on sys.path, which is this one, not
# the repo root -- and the installed egg-info still names the pre-rename
# `ai_analysis` package, so `import ai` fails without this. Every page imports
# this module first, so one line here covers all of them.
ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from i18n import DEFAULT_LOCALE, available_locales, t  # noqa: E402

#: Sequential default from the design palette: one hue, used for every
#: single-series magnitude chart here (which is nearly all of them).
BLUE = "#2a78d6"

#: Categorical slots, in the fixed order the palette defines. Never cycled:
#: the scatter on the no-jobs page caps at three and folds the rest into GRAY,
#: which is the all-pairs limit that ordering was validated for.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")

#: De-emphasis / "everything else".
GRAY = "#9c9b95"

TEXT = "#0b0b0b"
MUTED = "#52514e"


def locale_selector() -> None:
    """Sidebar language picker, one call per page (Streamlit reruns the whole
    script per page, so the selection lives in `st.session_state`, not here).
    """
    locales = available_locales()
    current = st.session_state.get("locale", DEFAULT_LOCALE)
    st.sidebar.selectbox("Language", locales,
                         index=locales.index(current) if current in locales
                         else 0,
                         key="locale")


def style_axes(ax, xlabel: str = "", ylabel: str = "") -> None:
    """Strip a matplotlib axes down to the data.

    Args:
        ax: The axes to restyle.
        xlabel: Optional x label.
        ylabel: Optional y label.
    """
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    for side in ("left", "bottom"):
        ax.spines[side].set_color("#d8d7d2")

    ax.tick_params(colors=MUTED, length=0)
    ax.grid(axis="x", color="#eceae5", linewidth=0.8)
    ax.set_axisbelow(True)

    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=9)

    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=9)


def barh(labels: Sequence, values: Sequence, xlabel: str = "",
         color: str = BLUE, height: Optional[float] = None):
    """Horizontal bar chart, largest at the top, values labelled.

    Horizontal because every category here is a company name, an ATS name or a
    sentence-long failure reason, and those do not fit under a vertical bar.

    Args:
        labels: Category names, in the order given.
        values: One number per label.
        xlabel: Axis label for the measure.
        color: Bar colour; the sequential blue unless a page says otherwise.
        height: Figure height in inches; scales with the bar count by default.

    Returns:
        The figure, ready for st.pyplot.
    """
    labels, values = list(labels), list(values)
    figure, ax = plt.subplots(
        figsize=(7, height or max(2.0, 0.32 * len(labels) + 0.8))
    )

    # Reversed so the largest sits at the top: barh numbers upward from the
    # x axis, which would otherwise put the smallest first.
    positions = range(len(labels))
    ax.barh(positions, values[::-1], color=color, height=0.62)
    ax.set_yticks(list(positions), labels[::-1], fontsize=9)
    style_axes(ax, xlabel=xlabel)

    span = max(values) if values else 0

    for position, value in zip(positions, values[::-1]):
        ax.text(value + span * 0.01, position, f"{value:,.0f}",
                va="center", fontsize=8, color=MUTED)

    ax.set_xlim(0, span * 1.12 or 1)
    figure.tight_layout()

    return figure


def counts_chart(column: pd.Series, xlabel: str, top: int = 0):
    """`barh` of a column's value counts.

    Args:
        column: Any Series; NaN is counted as "(blank)" rather than dropped,
            because how many rows have no title is itself a finding.
        xlabel: Axis label for the measure.
        top: Keep only the N largest; 0 keeps all.

    Returns:
        The figure, or None if the column is empty.
    """
    blank = t("common.blank")
    counts = column.fillna(blank).replace("", blank).value_counts()

    if counts.empty:
        return None

    if top:
        counts = counts.head(top)

    return barh(counts.index.astype(str), counts.values, xlabel=xlabel)


#: `diagnose.explain` writes one free-text line per empty board, shaped
#: ``<cause> (<N> anchors, <M> scripts[, <SPA marker>])`` with an optional
#: ``(redirected to ...)`` tail. Reading it back here rather than changing the
#: writer keeps that file readable by a human, which is what it is for. The
#: marker field is optional and easy to miss: leaving it out of the pattern
#: silently leaves it glued to the cause, turning seven causes into nineteen.
REASON_RE = re.compile(
    r"^(?P<cause>.*?)"
    r"(?:\s*\((?P<anchors>\d+) anchors, (?P<scripts>\d+) scripts"
    r"(?:, (?P<marker>[^)]*))?\))?"
    r"(?:\s*\(redirected to (?P<redirect>[^)]*)\))?$"
)


def parse_reasons(reasons: pd.Series) -> pd.DataFrame:
    """Split no_jobs.csv `reason` lines into their fields.

    Args:
        reasons: The free-text `reason` column.

    Returns:
        Frame with `cause`, `anchors`, `scripts`, `marker` and `redirect`;
        the two counts as floats, since a reason that names an exception
        carries neither.
    """
    parsed = reasons.astype(str).str.extract(REASON_RE)
    parsed["anchors"] = parsed["anchors"].astype(float)
    parsed["scripts"] = parsed["scripts"].astype(float)

    return parsed


@st.cache_data(show_spinner=False)
def _read_csv(path: str, _mtime: float) -> pd.DataFrame:
    """Read one CSV, keyed on its mtime so a run invalidates the cache."""
    return pd.read_csv(path)


def load_csv(path: Path, columns: Sequence[str] = ()) -> pd.DataFrame:
    """A pipeline CSV, or an empty frame if the stage has not run yet.

    Args:
        path: File to read.
        columns: Column names for the empty frame, so a page can chart a
            missing file without special-casing it.

    Returns:
        The rows as a DataFrame.
    """
    if not path.exists():
        return pd.DataFrame(columns=list(columns))

    return _read_csv(str(path), path.stat().st_mtime)


def file_stamp(path: Path) -> str:
    """When a file was last written, for "last run".

    Args:
        path: File to check.

    Returns:
        A local timestamp, or "never" if the file is not there.
    """
    if not path.exists():
        return "never"

    return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M"
    )


def run_with_log(label: str, work: Callable[[Callable[[str], None]], dict]):
    """Run one workflow, streaming its progress lines into the page.

    Args:
        label: Status header shown while it runs.
        work: Callable taking a `log(line)` callback and returning the stage's
            stats dict. Everything in the pipeline already takes such a
            callback, so this is a thin adapter rather than a wrapper.

    Returns:
        The stats dict, or None if the run raised -- the traceback is shown in
        the status box instead of killing the page.
    """
    lines: List[str] = []

    with st.status(label, expanded=True) as status:
        area = st.empty()

        def log(line: str) -> None:
            lines.append(line.strip())
            # Only the tail: a full board run emits hundreds of lines and
            # rendering all of them on every callback makes the page crawl.
            area.code("\n".join(lines[-15:]))

        try:
            stats = work(log)
        except Exception as exc:
            status.update(label=t("common.status.failed", label=label),
                          state="error")
            st.exception(exc)

            return None

        status.update(label=t("common.status.done", label=label),
                      state="complete")

    return stats
