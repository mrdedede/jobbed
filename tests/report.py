"""Detector accuracy report against test fixture corpus.

Usage:
    python tests/report.py

Shows precision / recall / unknown-rate per ATS.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

from job_scrapper.detector import ATSDetector

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_labels() -> list[dict]:
    """Load test fixture labels.

    Returns:
        List of label dicts.

    Raises:
        SystemExit if labels.csv not found.
    """
    path = FIXTURES / "labels.csv"

    if not path.exists():
        raise SystemExit(
            "No fixtures. Run: python tests/fetch_fixtures.py"
        )

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def detect(detector: ATSDetector, row: dict):
    """Detect ATS from fixture.

    Args:
        detector: ATSDetector instance.
        row: Fixture label row.

    Returns:
        DetectionResult.
    """
    html = (FIXTURES / row["fixture"]).read_text(encoding="utf-8")

    return detector.detect_html(html, row["url"])


def run(detector: ATSDetector, row: dict) -> str | None:
    """Detect ATS name from fixture.

    Args:
        detector: ATSDetector instance.
        row: Fixture label row.

    Returns:
        Detected ATS name or None.
    """
    return detect(detector, row).detected_ats


def main() -> int:
    """Run accuracy report."""
    labels = load_labels()
    detector = ATSDetector()

    true_pos: Counter[str] = Counter()
    false_pos: Counter[str] = Counter()
    false_neg: Counter[str] = Counter()

    misses = []

    for row in labels:
        expected = row["expected"] or None
        got = run(detector, row)

        if got == expected:
            if expected:
                true_pos[expected] += 1
            continue

        if got:
            false_pos[got] += 1

        if expected:
            false_neg[expected] += 1

        misses.append((row["url"], expected, got))

    names = sorted(
        set(true_pos) | set(false_pos) | set(false_neg)
    )

    print(f"{len(labels)} fixtures\n")
    print(f"{'ATS':16} {'TP':>4} {'FP':>4} {'FN':>4} "
          f"{'prec':>6} {'recall':>7}")
    print("-" * 50)

    for name in names:
        tps, fps, fns = true_pos[name], false_pos[name], false_neg[name]

        precision = tps / (tps + fps) if tps + fps else 0.0
        recall = tps / (tps + fns) if tps + fns else 0.0

        print(f"{name:16} {tps:4} {fps:4} {fns:4} "
              f"{precision:6.2f} {recall:7.2f}")

    negatives = [row for row in labels if not row["expected"]]

    clean = sum(
        1
        for row in negatives
        if run(detector, row) is None
    )

    print("-" * 50)
    print(f"{'TOTAL':16} {sum(true_pos.values()):4} "
          f"{sum(false_pos.values()):4} {sum(false_neg.values()):4}")
    print(
        f"\nNegatives correctly rejected: "
        f"{clean}/{len(negatives)}"
        f" ({clean / max(len(negatives), 1):.0%})"
    )

    if misses:
        print(f"\n{len(misses)} misses:")

        for url, expected, got in misses:
            print(f"  {expected or 'none':14} -> {got or 'none':14} {url}")

    leads: Counter[str] = Counter()

    for row in labels:
        vendor = detect(detector, row).unknown_vendor

        if vendor:
            leads[vendor] += 1

    if leads:
        print(f"\nUnrecognized vendors on {sum(leads.values())} "
              f"undetected pages:")

        for domain, count in leads.most_common(15):
            print(f"  {count:3}  {domain}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
