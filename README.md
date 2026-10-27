# Jobbed

A multi-user job-matching service. Every night it scrapes career boards into one
shared Postgres table; each user then keeps their own keywords, blacklist and CV
through a REST API and asks the API to grade the postings against that CV with
Claude (or a local Ollama model).

```
career boards ──► nightly CronJobs ──► job_postings (shared, Postgres)
                                              │
 user ── REST API (JWT) ── keywords, blacklist, CVs ── ai_analysis (per user)
                    │
                    └──► LLM gateway ──► Anthropic API / Ollama
```

## How it works

**Nightly, for everyone** (four stages, see [Running with Docker / Kubernetes](#running-with-docker--kubernetes)):

1. **ScrapeBoards** reads the active `job_boards` rows and finds postings with
   several strategies (JSON-LD, link pagination, API discovery), detecting the ATS.
2. **RenderBoards** retries empty boards in headless Chromium.
3. **IngestJobs** fetches each posting's full description and stores it in
   `job_postings` (deduplicated by URL).
4. **RenderPostings** retries the pages that plain HTTP could not read.

**On demand, per user**, through the API (`/docs` lists every route):

- `POST /v1/auth/signup` / `login` return a JWT; everything else is under `/v1/me`.
- Manage **keywords**, **blacklist terms** and **CVs** (Markdown, one active).
- `POST /v1/me/cvs/{cv_id}/suggest-terms` asks the first model in
  `ANALYSIS_MODELS` for keywords and blacklist terms fitting a CV (stores nothing; at most `DAILY_TERMS_CAP` per
  day); `POST /v1/me/terms` adds the ones the user keeps in one call.
- `GET /v1/me/analyses/candidates?window_days=N` lists the recent ungraded
  postings that pass your keywords and blacklist, newest first (no
  description; `GET /v1/me/jobs/{job_id}` has it).
- `POST /v1/me/analyze` takes `{"job_ids": [...], "model": ...}` (1-50 ids
  picked from the candidates), sends each with your active CV to the LLM
  gateway, and stores a 0-100 grade plus a written analysis in `ai_analysis`.
  It answers `202 {"analysis_id"}` at once; poll `GET /v1/me/analyze/{analysis_id}`
  for `status` (`running`/`done`/`failed`), progress (`done` of `total`) and,
  when done, `result`. Unknown, already graded and over-quota ids come back
  counted in `result.skipped`.
- `GET /v1/me/analyses` lists your grades.
- **Daily quota:** at most `DAILY_ANALYSIS_CAP` (default 10) postings graded per
  user per UTC day; past that the API answers `429` with `Retry-After`.

- `POST /v1/me/cvs/generate` rewrites your active CV for one graded posting
  in the background (poll `GET /v1/me/cvs/generate/{generation_id}`);
  `GET /v1/me/generated-cvs` lists the results. At most `DAILY_CV_CAP`
  (default 2) per user per UTC day. Details in
  [docs/cv-generation.md](docs/cv-generation.md).
- `POST /v1/me/cover-letters/generate` (`{"cv_id", "answers"}`) writes a cover
  letter from a generated CV, its posting, the posting's analysis and the
  user's answers, in the posting's language (poll
  `GET /v1/me/cover-letters/generate/{generation_id}`);
  `GET /v1/me/cover-letters` lists them, `PUT /v1/me/cover-letters/{id}` saves
  an edit. At most `DAILY_LETTER_CAP` (default: `DAILY_CV_CAP`) per user per
  UTC day.

- `GET /v1/me/applications` is the application-tracking board: one card per
  posting you generated a CV for, `status` `cv_generated` until you move it.
  `PUT /v1/me/applications/{job_id}` with `{"status": ...}` moves it
  (`applied`, `interviews`, `jobbed`, `rejected`, or back to `cv_generated`).
  Rejecting records the stage in `rejected_from` and takes an optional
  `rejection_reason` and `feedback`.

## Data flow and privacy

- **Scraped:** company, title, URL, location, description, detected ATS, and
  which strategy found it. Shared by all users.
- **Sent to the model for one grade:** the grading prompt (`ai/grade-job.md`),
  the user's active CV, and the posting text. The answer is JSON:
  `{"adequation_grade": 75, "depth_analysis": "..."}`.
- **Path:** API, then the LLM gateway, then the Anthropic API. Only the gateway
  holds `ANTHROPIC_API_KEY`; it is reachable from the API and nothing else.
- **Stored:** CVs, keywords, grades and generated CVs live in Postgres, scoped
  to the user, and are deleted with the account (`DELETE /v1/me`). Passwords are
  bcrypt hashes. CV text and postings are sent to Anthropic (or to your own
  Ollama) for grading; nothing is kept by this service beyond the result.

## Configuration

### Files

Only these are still files; everything per-user is an API resource.

| File | Purpose |
|---|---|
| `user_info/job_boards.csv` | `company,url` list of boards. Loaded into `job_boards` by the `migrate` step (`python -m db.boards`). Copy `job_boards_example.csv`. |
| `user_info/france_travail_credentials.json` | Optional `client_id` / `client_secret` for the France Travail source (or `FRANCE_TRAVAIL_CLIENT_ID` / `_SECRET`). The source also needs `SEARCH_KEYWORDS` filled in `job_scraper/api_sources/france_travail.py`. |
| `user_info/CV_placeholder.docx` | DOCX template for the local Python renderer (`cv_generator`). The API returns JSON; the front end renders DOCX/PDF. |
| `user_info/{keywords,blacklist}_example.txt`, `my_cv_example.md` | Samples for the one-time [legacy import](#importing-the-old-sqlite-history). |

### Environment variables

| Variable | Used by | Default / notes |
|---|---|---|
| `DATABASE_URL` | everything | `postgresql+psycopg://jobbed:jobbed@localhost:5432/jobbed` |
| `JWT_SECRET` | API | Required, at least 32 characters; the API refuses to start otherwise. |
| `GATEWAY_URL` | API | `http://localhost:8000` |
| `ANALYSIS_MODELS` | API | `haiku,sonnet`. Models a client may request; add e.g. `ollama-qwen2.5:0.5b`. The first one also writes keyword suggestions. |
| `DAILY_ANALYSIS_CAP` | API | `10` postings per user per UTC day. |
| `DAILY_CV_CAP` | API | `2` CV generations per user per UTC day. |
| `DAILY_LETTER_CAP` | API | Cover letters per user per UTC day; defaults to `DAILY_CV_CAP`. |
| `DAILY_TERMS_CAP` | API | `5` keyword/blacklist suggestions per user per UTC day. |
| `ANTHROPIC_API_KEY` | gateway | Empty means Claude models answer 502. |
| `OLLAMA_BASE_URL`, `OLLAMA_TIMEOUT` | gateway | `http://localhost:11434`, `180` s |
| `CLAUDE_MODEL_HAIKU` / `_SONNET` / `_OPUS` | gateway | Override the model ids behind the short names. |
| `JOBBED_DATA_DIR` | scraper jobs | CSV hand-off directory (`temp/`; `/data` in the images). |
| `JOBBED_BOARDS_CSV` | `migrate` | Path to the boards CSV. |
| `FRANCE_TRAVAIL_CLIENT_ID` / `_SECRET` | scraper jobs | Optional. |
| `CHROMIUM_NO_SANDBOX` | render jobs | Set to `1` by the `scrape` image. |

`.env.example` and `k8s/secret.example.yaml` show how to supply them.

## Getting started

Prerequisites: Python 3.11+, Docker with Compose, and for the cluster path
minikube and kubectl.

```bash
pip install -r requirements.txt            # scraper, tests, tooling
cp user_info/job_boards_example.csv user_info/job_boards.csv    # then edit

export JWT_SECRET=$(openssl rand -hex 32)
export ANTHROPIC_API_KEY=sk-ant-...         # or use Ollama, see below
docker compose --profile jobs run --rm migrate    # schema + boards list
docker compose up -d api                          # Postgres, gateway, API
curl http://localhost:8080/healthz
```

Then fill the database (`scrape` and `ingest` below) and use the API as shown
under [Reaching the API](#running-with-docker--kubernetes).

### Running tests

```bash
docker compose up -d postgres
docker compose exec postgres psql -U jobbed -c "CREATE DATABASE jobbed_test"   # once
pytest tests/
```

Tests use `jobbed_test` (override with `TEST_DATABASE_URL`) and recreate its
schema each session. `tests/test_pdf_gen.py` is skipped without LibreOffice.

## Running with Docker / Kubernetes

The scrape pipeline runs unattended as four stages, one after another. Every
stage hands over through the data directory (`JOBBED_DATA_DIR`, `/data` in
the images) and the database (`DATABASE_URL`).

| Stage | Command | Reads | Writes |
|---|---|---|---|
| ScrapeBoards | `python -m job_scraper.scrape_boards` | active `job_boards` rows | `jobs.csv`, `no_jobs.csv` |
| RenderBoards | `python -m job_scraper.scrape_boards --render-missed` | `no_jobs.csv` | `jobs_rendered.csv`, `no_jobs_rendered.csv` |
| IngestJobs | `python -m job_scraper.scrape_postings --ingest [--max-minutes N]` | `jobs.csv`, `jobs_rendered.csv` | `job_postings`, `failed_postings.csv` |
| RenderPostings | `python -m job_scraper.scrape_postings --render-failed [--max-minutes N]` | `failed_postings.csv` | `job_postings` |

Ingest is resumable: URLs already stored are never refetched, so a killed or
time-boxed run (`--max-minutes`) picks up where it stopped. The two render
stages need Chromium and use the `scrape` image; the others use `ingest`.

**Images** (build from the repo root):

```bash
docker build -f job_scraper/Dockerfile --target ingest -t jobbed-ingest .
docker build -f job_scraper/Dockerfile --target scrape -t jobbed-scrape .
docker build -f api/Dockerfile -t jobbed-api .
```

**Docker Compose**, one stage at a time:

```bash
docker compose --profile jobs run --rm migrate
docker compose --profile jobs run --rm scrape
docker compose --profile jobs run --rm ingest
```

**Reaching the API** (needs the schema, so `migrate` first). Three ways to
get a base URL; the curl sequence below is the same for all of them.

```bash
# 1. compose: Postgres and the LLM gateway start with it, Ollama does not
export JWT_SECRET=$(openssl rand -hex 32)      # >= 32 chars or the API refuses to start
docker compose up -d api
API=http://localhost:8080

# 2. minikube, port-forward (manifests from the Kubernetes section)
kubectl port-forward svc/jobbed-api 8080:8080 &
API=http://localhost:8080

# 3. minikube, NodePort. On macOS with the docker driver the node IP is not
# routable from the host, so this is a tunnel you leave running:
kubectl apply -f k8s/dev/api-nodeport.yaml
API=$(minikube service jobbed-api-nodeport --url)     # Linux/VM driver: http://$(minikube ip):30080
```

```bash
H='content-type: application/json'
curl $API/healthz                                         # docs at $API/docs

# signup returns a token; login does too
TOKEN=$(curl -s $API/v1/auth/signup -H "$H" \
  -d '{"email":"me@example.com","password":"correct horse"}' | jq -r .access_token)
TOKEN=$(curl -s $API/v1/auth/login -H "$H" \
  -d '{"email":"me@example.com","password":"correct horse"}' | jq -r .access_token)
A="Authorization: Bearer $TOKEN"

curl -s -X POST $API/v1/me/keywords  -H "$H" -H "$A" -d '{"term":"python"}'
curl -s -X POST $API/v1/me/blacklist -H "$H" -H "$A" -d '{"term":"senior"}'
curl -s -X POST $API/v1/me/cvs       -H "$H" -H "$A" -d '{"content_md":"Backend engineer, Python, Postgres"}'

IDS=$(curl -s "$API/v1/me/analyses/candidates?window_days=1" -H "$A" \
  | jq -c '[.[:5][].job_id]')                             # pick the first five
RUN=$(curl -s -X POST $API/v1/me/analyze -H "$H" -H "$A" \
  -d "{\"job_ids\":$IDS,\"model\":\"haiku\"}" | jq -r .analysis_id)   # 202
curl -s $API/v1/me/analyze/$RUN -H "$A"                 # poll until status != running
curl -s "$API/v1/me/analyses?limit=20&min_grade=60" -H "$A"

JOB=$(curl -s "$API/v1/me/analyses?limit=1" -H "$A" | jq -r '.[0].job_id')
GEN=$(curl -s -X POST $API/v1/me/cvs/generate -H "$H" -H "$A" \
  -d "{\"job_id\":\"$JOB\"}" | jq -r .generation_id)    # 202
curl -s $API/v1/me/cvs/generate/$GEN -H "$A"            # poll until status != running
curl -s $API/v1/me/generated-cvs -H "$A"
```

`model` must be in the API's `ANALYSIS_MODELS` (default `haiku,sonnet`); add
e.g. `ollama-qwen2.5:0.5b` to grade locally. Every route except `/v1/auth/*`
and `/healthz` acts on the token's user (`/v1/me/...`); there is no user id in
any path.

**Gateway access control.** The LLM gateway holds the Anthropic key and is
callable by the API and by nothing else. Under compose it sits on a private
`llm` network (shared only with `api` and `ollama`) and publishes no host
port; poke it through `docker compose exec api python -c "..."`. In the
cluster, `k8s/networkpolicy-llm-gateway.yaml` allows ingress only from pods
labelled `app: jobbed-api`; it takes effect only on a CNI that enforces
NetworkPolicy, so run the probe in that file's header on any new cluster
(verified on minikube's kindnet: an unlabelled pod times out once the policy
has propagated, a few seconds after `kubectl apply`).
This restricts *who* can call; how much each user may spend is capped by the
daily quotas (`DAILY_ANALYSIS_CAP`, `DAILY_CV_CAP`).

**End-to-end check** (`scripts/e2e_api.py`, stdlib only): signs up users,
seeds tagged postings, and checks that `/v1/me/analyze` grades the same set the
CLI does for identical inputs, that other keywords give another set, and that
re-runs and paging behave. It uses a per-run random keyword token, so no real
posting is graded, and removes everything it created.

```bash
# compose (Ollama as the grader; the model must be in ANALYSIS_MODELS)
export JWT_SECRET=$(openssl rand -hex 32) ANALYSIS_MODELS=haiku,sonnet,ollama-qwen2.5:0.5b
docker compose --profile jobs run --rm migrate
docker compose up -d ollama api
docker compose exec ollama ollama pull qwen2.5:0.5b      # once
python scripts/e2e_api.py

# minikube, grading through the compose Ollama (dev overrides, undo with `env VAR-`)
kubectl set env deploy/llm-gateway OLLAMA_BASE_URL=http://host.minikube.internal:11434
kubectl set env deploy/jobbed-api ANALYSIS_MODELS=haiku,sonnet,ollama-qwen2.5:0.5b
E2E_TARGET=k8s python scripts/e2e_api.py
```

With a real `ANTHROPIC_API_KEY` set `E2E_MODEL=haiku` and skip the overrides.
The CLI takes the same knobs: `python -m ai.main_analysis <user_id> --model
… --limit … --window "-7 days"`.

**Kubernetes** (manifests in `k8s/`, schedules in Europe/Paris: scrape 00:00,
render-boards 00:45, ingest 01:15, render-postings 05:00):

```bash
kubectl apply -f k8s/pvc.yaml
kubectl create secret generic jobbed-secrets \
  --from-literal=DATABASE_URL='postgresql+psycopg://...'   # see k8s/secret.example.yaml
kubectl apply -f k8s/job-migrate.yaml
kubectl apply -f k8s/cron-scrape_boards.yaml -f k8s/cron-ingest_jobs.yaml \
              -f k8s/cron-render_boards.yaml -f k8s/cron-render_postings.yaml

# The API and its gateway (after migrate-jobs); secrets are separate, see
# k8s/secret.example.yaml
docker build -f api/Dockerfile -t jobbed-api .
docker build -f llm_gateway/Dockerfile -t jobbed-llm-gateway .
minikube image load jobbed-api:latest jobbed-llm-gateway:latest
kubectl create secret generic jobbed-api-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)"
kubectl create secret generic jobbed-gateway-secrets \
  --from-literal=ANTHROPIC_API_KEY='...'
kubectl apply -f k8s/llm-gateway.yaml -f k8s/api.yaml \
              -f k8s/networkpolicy-llm-gateway.yaml
# reach it: see "Reaching the API" above (no Ingress yet)

# Optional: self-hosted models (CPU), reachable from the gateway only
kubectl apply -f k8s/ollama.yaml
kubectl exec deploy/ollama -- ollama pull qwen2.5:0.5b
kubectl set env deploy/jobbed-api ANALYSIS_MODELS=haiku,sonnet,ollama-qwen2.5:0.5b

# Run a stage now instead of waiting for its schedule
kubectl create job --from=cronjob/scrape-boards test-scrape
kubectl logs -f jobs/test-scrape
```

Set `suspend: true` on a CronJob to switch that stage off.

Environment variables are listed under [Configuration](#environment-variables).

### Measurements (for tuning the schedule)

Taken 2026-10-12 from a laptop against the live boards (plain HTTP for the
scrape and fetch numbers, Chromium headless shell for the browser ones). The
first cluster run on minikube (2026-10-14) agrees: 738 boards, 88,601
postings, 5 boards failed, 11.7 min.

| What | Result |
|---|---|
| Full board scrape | 634 boards, **13.1 min**, 81,190 postings, 10 boards raised |
| Churn | about **3,000 new postings a night** (3.7%), about 3,100 gone |
| Detail fetch rate | **21.5 postings/s** (1,291/min), 1.5% fail or come back empty |
| First ingest (about 75,000 backlog) | about 1 h, 1.5 h with the slow-host tail |
| Steady-state ingest | about 2.5 min |

Per-host politeness caps a single host at about 5 req/s, so one big board
(e.g. `careers.ey.com`) sets a floor on the tail of a run.

Why 171 boards gave nothing in the scrape:

| Count | Reason | Needs |
|---|---|---|
| 85 | Links present, none look like jobs | A better link strategy, not a browser |
| 34 | Likely JS-rendered | RenderBoards |
| 19 | No links on page (empty shell or bot challenge) | RenderBoards, maybe |
| 17 | HTTP 403 | Blocked; rendering rarely helps |
| 9 | `TypeError` | Fixed since |
| 7 | HTTP 500/404, connect error, timeout, bad URL | Retry or drop |

So RenderBoards' real input is about 53 boards, not 171.

Browser cost (pool of one browser, N pages at once):

| Pages at once | Pages/s | 99 pages | Peak memory |
|---|---|---|---|
| 2 | 0.33 | 298 s | 1,175 MB |
| 4 (default) | 0.65 | 152 s | 1,321 MB |
| 6 | 0.91 | 109 s | 1,670 MB |

- Throughput scales about linearly, roughly 0.15 pages/s per page slot. A
  pool at 4 is about 3.4x the launch-per-call path and uses less memory.
- Most of the 6 s per page is the fixed settle sleeps (5.25 s); the load
  itself is under a second, so a smarter wait is the biggest speed win left.
- Only 13 of 53 candidate boards produced jobs (about 150 postings, 0.18% of
  the total), and 3 stopped at exactly 25 jobs: the render reads the first
  page only, with no pagination yet.
- 40% of rendered boards' postings fail plain HTTP, so they need
  RenderPostings as well.
- Expected render load: RenderBoards about 80 s; RenderPostings about 30 min
  the first night (about 1,100 failures plus rendered-board postings), about
  1 min afterwards.

Settings chosen from this: ingest `--max-minutes 225` in a 240 min slot,
render pool of 4, render pods requesting 2 Gi with a 3 Gi limit,
RenderBoards deadline 15 min and RenderPostings 120 min. Caveats: one laptop,
99 URLs for the browser runs, no per-host politeness in the browser
prototype.

### Individual scripts

```bash
python -m job_scraper.scrape_boards [--limit N] [--render-missed]
python -m job_scraper.scrape_postings --ingest [--max-minutes N]
python -m job_scraper.scrape_postings --render-failed [--max-minutes N]
python -m ai.main_analysis <user_id> [--model haiku] [--limit 20] [--window "-24 hours"]
python scripts/e2e_api.py                  # HTTP end-to-end check
python scripts/import_legacy_sqlite.py     # one-off, see below
scripts/check_clean.sh                     # fails on legacy leftovers
```

---

## Architecture

```
jobbed/
├── job_scraper/      # boards, ATS detection, strategies, posting fetch, render pool
│   ├── strategies/, api_sources/
│   ├── scrape_boards.py, scrape_postings.py, pipeline.py, paths.py
├── api/              # FastAPI: auth, /v1/me/*, JWT (routers/, schemas.py, security.py)
├── llm_gateway/      # FastAPI gateway: Claude and Ollama adapters, metrics
├── ai/               # gateway client, grading, CV generation, prompts (*.md)
├── db/               # Postgres access, SQLAlchemy Core
│   ├── tables.py     # schema (migrated by alembic/)
│   └── users.py, boards.py, jobs.py, analysis.py, generated_cv.py, usage.py
├── alembic/          # migrations
├── cv_generator/     # DOCX / PDF rendering of a generated CV
├── k8s/              # manifests (+ k8s/dev for minikube)
├── scripts/          # e2e_api.py, import_legacy_sqlite.py, check_clean.sh
├── docs/             # design notes (cv-generation.md)
├── tests/
├── user_info/        # boards CSV, credentials, templates (personal files gitignored)
└── docker-compose.yml
```

### Database schema (Postgres)

| Table | Holds |
|---|---|
| `users` | `email` (unique, case-insensitive), bcrypt `hashed_password` |
| `user_keywords`, `user_blacklist` | per-user filter terms, unique per `(user_id, term)` |
| `user_cvs` | per-user CV Markdown; at most one `is_active` |
| `job_boards` | `company`, `url`, `active`; unique on `md5(url)` |
| `job_postings` | shared postings: company, title, description, url (unique on `md5(url)`), place, via, ats, `scraped_at` |
| `ai_analysis` | one grade per `(user_id, job_id)`: `adequation_grade`, `depth_analysis`, `ai_model` |
| `generated_cv` | tailored CV as JSONB, `locale`, linked to the user, posting and analysis |
| `llm_usage` | daily counters per `(user_id, day, kind)` for the quota |

Schema changes go through Alembic: `alembic upgrade head`
(`docker compose --profile jobs run --rm migrate`, or `k8s/job-migrate.yaml`).

## Importing the old SQLite history

Before the API existed the project was single-user with a local SQLite file
(`db/joblister.db`). To bring that history into Postgres under one account:

```bash
python scripts/import_legacy_sqlite.py --sqlite db/joblister.db \
    --email you@example.com --user-info user_info/
```

It asks for a password only if the account must be created, opens the SQLite
file read-only, seeds your keywords, blacklist and active CV from the old
`user_info` text files, and copies postings (reusing any the nightly ingest
already stored), grades (latest per posting) and generated CVs. It prints
old/new/skipped counts and is safe to re-run.

### Cutover runbook

1. Back up outside the repo: copy `db/joblister.db` to e.g. `~/jobbed-archive/`
   (compare `sha256sum`), and `pg_dump` the target database.
2. Rebuild the images, then migrate: `docker compose --profile jobs build migrate`
   and `docker compose --profile jobs run --rm migrate` (on Kubernetes, build,
   `minikube image load`, then apply `k8s/job-migrate.yaml`). A stale image
   silently skips newer migrations. Let the nightly jobs run at least once.
3. Run the import, compare its counts with the SQLite file, and spot-check a few
   postings and CVs through the API.
4. Smoke test as that user: `GET /v1/me/analyses`, `POST /v1/me/analyze` with `limit=1`.
5. Keep `db/joblister.db` and your `user_info/` files where they are: both are
   gitignored, and an older tag that still reads them keeps working.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| API exits at start | `JWT_SECRET` missing or under 32 characters. |
| `relation ... does not exist` | Migrations not applied. Rebuild the `migrate` image first (`docker compose --profile jobs build migrate`), then run it; an old image has no newer migrations. |
| `401` | Token expired or missing; log in again. |
| `409 no active CV` | Add a CV (`POST /v1/me/cvs`) or activate one. |
| `409 analysis already running` | One analysis per user at a time; wait for it to finish. |
| `429` on analyze | Daily cap reached; retry after the `Retry-After` seconds, or raise `DAILY_ANALYSIS_CAP`. |
| `409 generation already running` | One CV generation per user at a time; poll the running one. |
| `429` on cvs/generate | Daily CV cap reached; retry after `Retry-After`, or raise `DAILY_CV_CAP`. |
| Poll of a generation answers `404` | Unknown id, finished over an hour ago, or the API restarted mid-run (the unit was refunded). Check `GET /v1/me/generated-cvs`. |
| `502` on analyze | Every grading call failed: no `ANTHROPIC_API_KEY` on the gateway, Ollama not running, or the model is not available. Check `docker compose logs llm_gateway`. |
| `GET /v1/me/analyses/candidates` is empty | No recent postings match your keywords. Widen `window_days` or check the nightly jobs ran. |
| A board gives nothing | Check `no_jobs.csv` in the data directory; JS-heavy boards need RenderBoards, some are blocked (403). |

## Performance notes

- Full board scrape about 13 minutes; steady-state ingest about 2.5 minutes (see
  Measurements).
- A grade is about 10 s per posting with Claude; four run concurrently.
- A CV rewrite with Sonnet is about 20 s; it runs in the background and the client polls.

## Contributing

Branch, run `pytest tests/` and `scripts/check_clean.sh`, open a pull request.
