# joblister — state of the project and refactoring priorities

Measured 2026-08-09 against the committed run artifacts (`temp/*.csv`,
`db/joblister.db`), `user_info/job_boards.csv` (101 boards), the 98-fixture
detector corpus, and a full test run (311 passed).

## 1. Pipeline as it actually runs

```
job_boards.csv (101)
  └─ main_scraper.py            → temp/jobs.csv                    19,793 rows / 55 boards
     └─ pre_processing.first_filter()  → temp/first_filtered_file.csv   8,350
        └─ post_scraper.py            → temp/detailed_jobs.csv          8,350
           └─ pre_processing.second_filter() → temp/filtered_detailed_jobs.csv  552
              └─ db_connection.insert_jobs() → job_data                   552
```

Four manual steps, no orchestrator (`main.py` is 0 bytes), and the stages do
not agree on filenames — see §4.1.

## 2. Yield per source

### 2.1 Board coverage

**55 of 101 boards return anything at all — 54%.** 46 boards yield zero rows
(atos, amazon, datadog, thales, decathlon, kicklox, coexya, sully group, bnp
paribas, michelin, kering, …). These are the JS-rendered listings the code
already documents; `--render` is the only lever and it is opt-in.

| Strategy | Boards won | Rows | Rows/board |
|---|---:|---:|---:|
| sitemap | 32 | 13,974 | 437 |
| links | 8 | 1,566 | 196 |
| wordpress | 6 | 2,145 | 358 |
| feed | 5 | 170 | 34 |
| workday | 4 | 1,938 | 484 |

The cheap, high-fidelity path (`feed`) wins **5 boards out of 55**. The
expensive, low-fidelity path (`sitemap`) carries **71% of all rows**.

### 2.2 Row validity per source — the headline number

"Valid" = the row carries a usable title, not a URL slug and not an empty
string.

| via | rows | **empty title** | slug-derived title | has place |
|---|---:|---:|---:|---:|
| sitemap | 13,974 | **56%** | 83% | 17% |
| wordpress | 2,145 | 0% | 4% | 0% |
| workday | 1,938 | 0% | 0% | 100% |
| links | 1,566 | 0% | 0% | 0% |
| feed | 170 | 0% | 0% | 100% |
| **all** | **19,793** | **39%** | 60% | 25% |

**39% of every row scraped has no title.** All of it is `sitemap`, and it is
concentrated: sander (3,083), axa (1,891), engie (1,720), vinci (934).

Two separate causes, both fixable:

1. **`_title_from_url` returns `""` for numeric slugs** —
   [board_scraper.py:966-982](job_scraper/board_scraper.py#L966-L982).
   `slug = re.sub(r"^\d+[-_]?", "", slug)` overwrites `slug`, so the
   `or slug` fallback on the last line can only ever fall back to the string
   it already emptied.
   ```
   _title_from_url("https://x.com/fr/jobs/associate-real-estate/76969") -> ''
   ```
   The descriptive segment is right there in the path, one level up.
   Same function leaks hash prefixes: `/offres/4lxbw5bd1f-data-ingenieur-h-f/`
   → `"Lxbw5bd1f Data Ingenieur H F"` (only the leading digit is stripped).

2. **`MAX_DETAIL = 200`** caps the per-posting JSON-LD read, so anything past
   posting 200 on a board gets the slug title by design. But even *within* the
   first 200, 38% are slug-titled — those boards publish no JSON-LD at all, so
   the cap is not the whole story.

### 2.3 Detail-stage success, keyed by which board strategy produced the row

Post-scraper extractor outcome for the 8,350 rows it fetched:

| board via | n | jsonld | main | body | workday | dead |
|---|---:|---:|---:|---:|---:|---:|
| sitemap | 7,929 | 47% | 51% | 0% | – | 2% |
| wordpress | 251 | 0% | 4% | **96%** | – | 0% |
| links | 90 | 18% | 3% | **79%** | – | 0% |
| workday | 57 | – | – | – | 100% | 0% |
| feed | 23 | **100%** | – | – | – | 0% |

The detail stage is the healthy part: 98% of pages yield a description,
median 3,937 chars. The `body` rung carrying 96% of wordpress rows means those
descriptions include page furniture, which then inflates `keyword_hits` in
`second_filter`.

### 2.4 ATS detection

- **28 of 55 producing boards get an ATS name — 51%.** The other 27 run the
  generic path blind.
- The detector is well-built and well-tested (98 fixtures, 60 of them
  negatives, `tests/report.py` reports precision/recall per ATS). Detection
  is not the bottleneck; the *scrapers behind* detection are — 17 of the 28
  registry entries are `_todo()` stubs.

### 2.5 End-to-end conversion

19,793 scraped → 8,350 first-filtered → 552 kept. **2.8% overall.**

## 3. The most damaging finding: the first filter is a no-op

Recomputed against `temp/first_filtered_file.csv`:

```
filtered rows                     8,350
  kept because keywords matched     947  (11%)
  kept ONLY by the ID-exemption   7,403  (89%)
  rows with an empty title        6,777
```

`first_filter` exempts a row from the keyword requirement when the title or
URL is ID-like ([pre_processing.py:106-157](user_info/pre_processing.py#L106-L157)).
The empty titles from §2.2 all sit at `/jobs/{numeric-id}` URLs, so
`_url_is_id_like` fires and **89% of what the filter passes was never filtered
at all**.

The cost is direct: `post_scraper` then fetches ~7,400 pages that had no
reason to be fetched, and `second_filter` throws 93% of the result away.
Fixing `_title_from_url` alone converts most of those rows into real titles
that the keyword filter can actually judge.

## 4. Correctness bugs

### 4.1 The pipeline is not wired
`post_scraper` reads `temp/filtered_file.csv`
([post_scraper.py:58](job_scraper/post_scraper.py#L58)); `pre_processing`
writes `temp/first_filtered_file.csv`
([pre_processing.py:9](user_info/pre_processing.py#L9)). That path does not
exist on disk. Both filter calls in `pre_processing.__main__` are half
commented out, and `first_filter`'s write used `to_csv()` without
`index=False` — the artifact still carries the stray index column. `main.py`
is empty, so nothing joins the stages.

### 4.2 Three broken SQL statements in `db/db_connection.py`
- `INSERT_NEW_AI_ANALYSIS` supplies 4 values for a 5-column table. Verified:
  `OperationalError: table ai_analysis has 5 columns but 4 values were supplied`.
- `INSERT_NEW_GENERATED_CV`: 8 values for 9 columns. Same failure.
- `SELECT_JOB_DESCRIPTION` queries table `job_detail`, which does not exist.
- Both use `$1`-style placeholders while the working insert uses `?`.
- `create_tables()` closes the cursor but never commits or closes the
  connection.

None of this has fired yet because `ai_analysis/` is an empty directory.

### 4.3 `insert_jobs` reimplements a UNIQUE constraint
[db_connection.py:100-142](db/db_connection.py#L100-L142) selects every
existing URL into a Python set, then filters with `iterrows()`. `url` is
already `TEXT UNIQUE`. `INSERT OR IGNORE` + `con.total_changes` gives both
counts in one statement and drops the whole read.

### 4.4 Packaging metadata does not match the code
`requires-python = ">=3.9"`, but `detector.py` uses `StrEnum` (3.11+),
`report.py` uses `str | None` (3.10+), `db_connection.py` uses
`tuple[int, int]` (3.9+). Also: `beautifulsoup4==4.12.2` in `pyproject.toml`
vs `==4.12.3` in `requirements.txt`; a `playwright ... ; extra == 'render'`
marker in the main dependency list that duplicates the real
`optional-dependencies` entry; and `packages = ["job_scraper"]` omits `db`
and `user_info`, which are imported as packages.

## 5. Efficiency

### 5.1 Every board page is fetched twice
`Board.detect_ats()` builds a fresh `ATSDetector` with its own
`requests.Session` and fetches `board_url`
([board_scraper.py:1478](job_scraper/board_scraper.py#L1478)); `Board.html`
then fetches the same page again with `Board.session`
([board_scraper.py:1465](job_scraper/board_scraper.py#L1465)). ~101 redundant
fetches per run, plus a discarded connection pool per board.

### 5.2 Sitemap detail fetches are sequential
`scrape_sitemap` fetches up to 200 posting pages one at a time, single
threaded, for each of 32 boards — up to ~6,400 serial round-trips, the
dominant wall-clock cost of the whole run. `post_scraper` already has the
`ThreadPoolExecutor` pattern that solves this.

### 5.3 …and then the same pages are fetched a third time
`scrape_sitemap` reads each posting's JSON-LD for a title; `post_scraper`
later re-fetches those same URLs for the description.

### 5.4 `_posting_fields` runs the full detector extractor per posting
[board_scraper.py:995](job_scraper/board_scraper.py#L995) calls
`detector.extract()`, which walks up to 20,000 elements collecting classes,
ids, data-attrs, anchors and script URLs — to read one JSON-LD block. On the
hot path, ~6,400 times per run.

### 5.5 No politeness controls
No delay, no retry/backoff, no `robots.txt` check on the fetch path (it is
read only to *find* sitemaps), and `post_scraper` runs 8 threads. `_fetch`
also does not check `Content-Type`, so a PDF reaches BeautifulSoup.

## 6. Code smells

- **`user_info/` holds both user data and code.** `pre_processing.py` — the
  filtering logic — lives in the directory of gitignored config files.
- **Cross-module private imports.** `post_scraper` imports six underscore
  names from `board_scraper` (`_fetch`, `_fetch_json`, `_dig`,
  `_first_string`, `_walk_jobpostings`, `_LOCALE_RE`). They are shared
  infrastructure wearing a private name; they belong in their own module.
- **Duplicated Workday URL parsing.** `scrape_workday`
  ([board_scraper.py:512-524](job_scraper/board_scraper.py#L512-L524)) and
  `_workday_api` ([post_scraper.py:152-163](job_scraper/post_scraper.py#L152-L163))
  derive tenant/site/locale from the URL with the same six lines.
- **Two `Job` dataclasses** with overlapping fields, one per module.
- **`board_scraper.py` is 1,622 lines** holding feed config, five strategy
  implementations, 17 stubs, URL heuristics and the orchestrator.
- **17 of 28 `VENDOR_SCRAPERS` entries are `_todo()` stubs** that raise
  `NotImplementedError` for `_feed` to swallow. The research notes in them are
  genuinely valuable; the control flow (raise-to-be-caught) is not.
- **`JOB_PATH` deliberately duplicates detector regexes.** The comment
  justifies it well — noting it only so a future reader does not "fix" it.
- **Module-level `assert`** guarding duplicate `signal_id`
  ([detector.py:1077](job_scraper/detector.py#L1077)) — vanishes under `-O`.
- **Magic thresholds without provenance.** `MIN_KEYWORD_MATCHES = 2`,
  `_DETAILED = 5`, `_INCOMPLETE = 3` — no note on how they were chosen, and
  §3 shows they are barely exercised.
- **Duplicate company names in `job_boards.csv`** (`statera` ×4,
  `uti group` ×3, distinct URLs). Company is the join key for per-board
  reporting, so counts collapse.
- **Stale comment** in `requirements.txt`: pandas is described as
  "main_scraper.py only" — `main_scraper` does not import it.
- 12 flake8 findings, all in `db/` and `user_info/`; `job_scraper/` is clean
  apart from one `E305`.

## 7. What is genuinely good

Worth protecting through any refactor:

- **`detector.py` is a strong design.** Fingerprints as data, a fixed scoring
  engine over them, source caps to stop one fact stacking, tier-based
  qualification, explicit `ambiguous` status, and an `unknown_vendor` lead
  generator. Adding an ATS is a dict entry.
- **The `via` field** on both `Job` types. It is what made this entire
  analysis possible without instrumenting anything.
- **311 passing tests in 5.4s**, plus a 98-fixture corpus that is 61%
  negatives — the hard part of a detector corpus.
- **Comments explain the *why* and cite measurements** ("Buys Extia's 20 real
  postings and costs 10 false ones"). Several record rejected approaches so
  they are not re-tried. This is unusually disciplined.
- Correct instincts throughout: opt-in Playwright behind a function-local
  import, thread-local sessions, per-row flush with resume,
  charset-over-header decoding, strategy fallbacks that return `[]` instead
  of raising.

## 8. Refactoring priorities

### P0 — restores correctness and cuts ~7,400 wasted fetches per run

1. **Fix `_title_from_url`** (§2.2). Do not overwrite `slug` before the
   fallback; fall back to the parent path segment when the last one is a bare
   id; strip leading hash-prefixes, not just leading digits. One function, and
   it fixes the 39% empty-title rate, which in turn fixes the 89% filter leak.
   Regression test: `/fr/jobs/associate-real-estate/76969` must not yield `""`.
2. **Wire the pipeline.** One filename constant for the first-filter output,
   shared by `pre_processing` and `post_scraper`; `index=False` on both
   writes; put the four stages behind `main.py`.
3. **Re-tune the filter thresholds after 1 and 2**, with the ID-exemption
   narrowed to rows that genuinely have nothing to match. Measure the new
   pass rate before touching the numbers.
4. **Fix or delete the broken SQL** (§4.2). If `ai_analysis/` is still empty,
   deleting the three statements is the smaller diff — write them with the
   feature.

### P1 — cost and structure

5. **Stop double-fetching the board page** (§5.1): pass `Board.session` into
   `ATSDetector`, and seed `Board._html` from the response the detector
   already read.
6. **Parallelise `scrape_sitemap`'s detail pass** using the
   `ThreadPoolExecutor` pattern already in `post_scraper.main`.
7. **Replace `_posting_fields`' `extract()` call** with a direct JSON-LD read
   — `soup.find_all("script", type="application/ld+json")` + the existing
   `_walk_jobpostings`. `post_scraper._from_jsonld` is already exactly this;
   share it (see 9).
8. **`INSERT OR IGNORE`** in `insert_jobs` (§4.3); drop the URL pre-read and
   `iterrows()`.
9. **Extract the shared HTTP/JSON helpers** (`_fetch`, `_fetch_json`, `_dig`,
   `_first_string`, `_walk_jobpostings`, `_LOCALE_RE`, Workday URL parsing)
   into `job_scraper/http.py` under public names. Removes the private
   cross-import and the duplicated Workday parsing in one move.
10. **Move `pre_processing.py`** out of `user_info/` into `job_scraper/`
    (or a `filters/` package), leaving `user_info/` as data only.

### P2 — hygiene

11. Fix `requires-python`, the bs4 version conflict, the stray `playwright`
    marker, and the `packages` list (§4.4).
12. Politeness: `Content-Type` check in `_fetch`, a small delay or a retry
    with backoff, and a documented concurrency ceiling (§5.5).
13. Split `board_scraper.py` — the `_todo()` research notes are the natural
    seam; they are documentation, not code, and a plain dict of notes reads
    better than 17 raising closures.
14. Deduplicate `job_boards.csv` company names, or key per-board reporting on
    URL.
15. Promote the module-level `assert` in `detector.py` to a test.
16. Clear the 12 flake8 findings and the stale pandas comment in
    `requirements.txt`.

### Explicitly not recommended

- **Rewriting the detector.** It is the best-engineered part of the codebase
  and its measured accuracy is not the limiting factor — scraper coverage is.
- **Unifying `JOB_PATH` with `ATS_REGISTRY`.** The existing comment gives a
  sound reason (gating vs. filtering); leave it.
- **New generic strategies.** `requirements.txt` records four that were
  measured and rejected on this corpus. Rendering is the remaining lever for
  the 46 dead boards, and it already exists behind `--render`.
