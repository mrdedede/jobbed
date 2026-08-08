"""Build test fixtures from live URLs.

Saves HTML samples under tests/fixtures/ and writes labels.csv for offline testing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PLATFORM_TO_ATS = {
    "lever": "lever",
    "teamtailor": "teamtailor",
    "recruitee": "recruitee",
}

ATS_OWNED_HOSTS = {
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "icims.com": "icims",
    "taleo.net": "taleo",
    "jobs.lever.co": "lever",
    "teamtailor.com": "teamtailor",
    "recruitee.com": "recruitee",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "bamboohr.com": "bamboohr",
    "comeet.co": "comeet",
    "onlyfy.jobs": "onlyfy",
    "breezy.hr": "breezy",
    "talentlyft.com": "talentlyft",
    "jobs.personio.de": "personio",
    "jobs.personio.com": "personio",
    "pinpointhq.com": "pinpoint",
    "jobs.jobvite.com": "jobvite",
    "applytojob.com": "jazzhr",
    "app.jazz.co": "jazzhr",
    "avature.net": "avature",
    "careers.hibob.com": "hibob",
    "njoyn.com": "njoyn",
    "softgarden.de": "softgarden",
    "digitalrecruiters.com": "digitalrecruiters",
}


# Vendor-owned *infrastructure* referenced by a page. A custom career domain
# that loads assets from, or links its postings to, one of these is running
# that ATS -- this is how the corpus labels custom-domain deployments, which
# jobs.csv records as "generic". Derived by grepping the corpus, deliberately
# limited to domains a third party cannot plausibly serve.
ATS_ASSET_MARKERS = {
    "teamtailor-cdn.com": "teamtailor",
    "app.teamtailor.com": "teamtailor",
    "cdn.lever.co": "lever",
    "jobs.lever.co": "lever",
    "app.ashbyhq.com": "ashby",
    "jobs.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "api.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
    "api.breezy.hr": "breezy",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.jobvite.com": "jobvite",
    "jobs.personio.de": "personio",
    "applytojob.com": "jazzhr",
    "myworkdayjobs.com": "workday",
    # Enterprise career-site platforms: the employer's own domain loads the
    # vendor's CDN. No hostname of their own to key on.
    "cdn.phenompeople.com": "phenom",
    "tbcdn.talentbrew.com": "radancy",
    "tmpwebeng.com": "radancy",
    "front.hibob.com": "hibob",
    "api.digitalrecruiters.com": "digitalrecruiters",
    "career.softgarden.de": "softgarden",
}


def ground_truth(url: str, html: str, fallback: str) -> str:
    """Determine ATS label from page hostname and vendor markers.

    Args:
        url: Page URL.
        html: Page HTML content.
        fallback: Default label if uncertain.

    Returns:
        ATS name or fallback.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")

    for domain, ats in ATS_OWNED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return ats

    lowered = html.lower()

    hits = {
        ats
        for marker, ats in ATS_ASSET_MARKERS.items()
        if marker in lowered
    }

    if len(hits) == 1:
        return hits.pop()

    return fallback


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ATSDetector/2.0; "
        "+https://example.com/ats-detector)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}


def rows_from_jobs() -> list[dict]:
    """Load rows from jobs.csv.

    Returns:
        List of {url, expected, origin} dicts.
    """
    path = ROOT / "jobs.csv"

    if not path.exists():
        return []

    out = []

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            platform = (row.get("platform") or "").strip().lower()

            out.append({
                "url": row["url"],
                "expected": PLATFORM_TO_ATS.get(platform, ""),
                "origin": f"jobs.csv:{platform}",
            })

    return out


def rows_from_boards() -> list[dict]:
    """Load rows from job_boards.csv.

    Returns:
        List of {url, expected, origin} dicts.
    """
    path = ROOT / "job_boards.csv"

    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "url": row["url"],
                "expected": "",
                "origin": "job_boards.csv",
            }
            for row in csv.DictReader(handle)
        ]


def sample(rows: list[dict], per_host: int, limit: int) -> list[dict]:
    """Sample rows uniformly by hostname.

    Args:
        rows: Input rows.
        per_host: Maximum rows per hostname.
        limit: Total row limit.

    Returns:
        Sampled rows.
    """
    """Keep at most `per_host` URLs per hostname, for variety over volume."""
    random.seed(0)
    random.shuffle(rows)

    seen: dict[str, int] = {}
    picked = []

    for row in rows:
        host = urlparse(row["url"]).netloc.lower()

        if not host or seen.get(host, 0) >= per_host:
            continue

        seen[host] = seen.get(host, 0) + 1
        picked.append(row)

    # Labeled rows first so a low --limit still covers every ATS.
    picked.sort(key=lambda item: (not item["expected"], item["url"]))

    return picked[:limit]


def fixture_name(url: str, expected: str) -> str:
    host = urlparse(url).netloc.lower().replace(":", "_")
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "_", f"{expected or 'none'}_{host}").strip("_")

    return f"{slug}_{digest}.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-host", type=int, default=2)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    FIXTURES.mkdir(parents=True, exist_ok=True)

    rows = sample(
        rows_from_jobs() + rows_from_boards(),
        args.per_host,
        args.limit,
    )

    session = requests.Session()
    session.max_redirects = 5
    session.headers.update(HEADERS)

    labels = []

    for index, row in enumerate(rows, start=1):
        url = row["url"]
        name = fixture_name(url, row["expected"])
        target = FIXTURES / name

        if target.exists():
            print(f"[{index}/{len(rows)}] cached  {url}")

            labels.append({
                **row,
                "fixture": name,
                "expected": ground_truth(
                    url,
                    target.read_text(encoding="utf-8"),
                    row["expected"],
                ),
            })
            continue

        try:
            response = session.get(url, timeout=20, stream=True)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()

            if "html" not in content_type:
                print(f"[{index}/{len(rows)}] skip    {url} ({content_type})")
                continue

            raw = response.raw.read(5_000_000, decode_content=True)

            html = raw.decode(
                response.encoding or response.apparent_encoding or "utf-8",
                errors="replace",
            )
        except (requests.RequestException, ValueError) as exc:
            print(f"[{index}/{len(rows)}] FAIL    {url}: {exc}")
            continue

        target.write_text(html, encoding="utf-8")

        labels.append({
            **row,
            "fixture": name,
            # The URL the page actually resolved to; redirects are part of
            # what the detector reasons about.
            "url": response.url,
            "expected": ground_truth(response.url, html, row["expected"]),
        })

        print(f"[{index}/{len(rows)}] saved   {response.url} -> {name}")
        time.sleep(args.delay)

    with (FIXTURES / "labels.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["fixture", "url", "expected", "origin"],
        )
        writer.writeheader()
        writer.writerows(labels)

    print(f"\n{len(labels)} fixtures in {FIXTURES}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
