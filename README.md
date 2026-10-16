# Jobbed

A job scraping and AI-powered CV matching application that helps candidates find and apply to relevant job postings by automatically analyzing job descriptions and generating tailored CVs.

## Overview

**Jobbed** is an intelligent job market tool that:

1. **Scrapes job postings** from multiple career boards and job listing websites
2. **Detects ATS systems** (Applicant Tracking Systems) used by employers
3. **Analyzes job fit** using AI (Claude) to grade how well each posting matches your CV
4. **Generates tailored CVs** using AI that rewrite your CV to emphasize skills relevant to each opportunity
5. **Manages data** in a local SQLite database for tracking analyses and generated CVs
6. **Visualizes results** through a Streamlit web interface for easy exploration and management

## Application Workflow

The application operates as a multi-stage pipeline, visualized in a Streamlit dashboard:

### Stage 1: Board Scraping
**Page:** `2_Scrape_boards.py`

- Reads a list of job boards from `user_info/job_boards.csv`
- Scrapes each board using multiple detection strategies (JSON-LD, link pagination, API discovery, optional JS rendering)
- Detects which ATS system each posting uses
- Outputs raw postings to `temp/jobs.csv`
- Optional: Use Playwright to render JavaScript-heavy boards

### Stage 2: Job Details & Filtering
**Page:** `3_Scrape_jobs.py`

- Reads postings from `temp/jobs.csv`
- Fetches full job descriptions from posting URLs
- Applies first-pass filtering using keywords and blacklist
- Stores unfiltered jobs in SQLite `job_data` table
- Outputs filtered postings to `temp/filtered_detailed_jobs.csv`

### Stage 3: AI Analysis & Grading
**Page:** `4_AI_analysis.py`

- Reads unanalyzed jobs from the database
- Sends each job description to Claude (Haiku model) with your CV
- Claude grades the posting (0-100) and provides analysis of fit
- Stores results in SQLite `ai_analysis` table
- Identifies postings within the past 24 hours awaiting analysis

### Stage 4: AI-Generated CV Production
**Page:** `5_CV_generation.py`

- Reads analyzed jobs from `ai_analysis` table
- Sends job posting + analysis to Claude (Sonnet model) with your CV template
- Generates a tailored CV emphasizing relevant skills
- Stores generated CV as JSON in `generated_cv` table
- Renders CV to DOCX format using your CV template

### Dashboard Home Page
**Page:** `Home.py`

- Displays summary statistics from the last scrape
- Shows database metrics (stored jobs, completed analyses, pending analysis queue)
- Visualizes posting distribution by company and scraping strategy

---

## Data Flow: Scraping & External AI

### Scraped Data

**Input:** Job board URLs from `user_info/job_boards.csv`
- Columns: `company`, `url`

**Output:** Raw job postings to `temp/jobs.csv`
- Columns: `company`, `title`, `url`, `place`, `via` (scraping strategy), `ats` (detected system)

**Scraped Fields:**
- Company name
- Job title
- Posting URL
- Location
- Detection method (which strategy found it)
- ATS system type

### Data Sent to External AI (Claude)

#### For Job Analysis (Haiku Model)
1. **Your CV** (from `user_info/my_cv.md`)
   - Full CV in Markdown format
   - Included in every grade request for context

2. **Grading Prompt** (from `ai/grade-job.md`)
   - Instructions on how Claude should evaluate the fit
   - Scoring criteria

3. **Job Description**
   - Full posting text from the scraped URL

**Output:** JSON with two fields
```json
{
  "adequation_grade": 75,
  "depth_analysis": "This role emphasizes Python and Django, which aligns well with your backend experience..."
}
```

#### For CV Generation (Sonnet Model)
1. **Your CV** (from `user_info/my_cv.md`)
   - Template to guide structure and content

2. **Generation Prompt** (from `ai/generate-cv.md`)
   - Instructions on how to tailor the CV
   - Required sections and format

3. **Job Analysis**
   - The `depth_analysis` from the grading stage (what Claude already identified as important)

4. **Job Description**
   - Full posting text

**Output:** JSON with locale and full CV structure
```json
{
  "locale": "en",
  "cv": {
    "cv_introduction": "...",
    "profile_text": "...",
    "skills": [...],
    "experiences": [...],
    "education": [...]
  }
}
```

### Privacy Note
- All Claude API calls are made via the Claude CLI (`claude` command) with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` set, preventing context memory storage
- Job descriptions and your CV are sent to Anthropic for analysis, not stored locally in a memory system
- Generated CVs are stored locally in SQLite as JSON

---

## Configuration

### Required Files

All configuration files are in `user_info/` directory. Examples are provided with `_example` suffix.

#### 1. **Job Boards List** (`user_info/job_boards.csv`)
List of career boards to scrape.

**Format:**
```csv
company,url
Company A,https://careers.companya.com
Company B,https://careers.companyb.com
LinkedIn,https://linkedin.com/jobs
Indeed,https://indeed.com
```

**Required columns:**
- `company`: Display name for the board
- `url`: URL to scrape

#### 2. **Your CV** (`user_info/my_cv.md`)
Your CV in Markdown format. Used by Claude for grading jobs and as a template for generating tailored CVs.

**Should include:**
- Personal summary
- Key skills (grouped by competence area)
- Work experience (with dates, companies, locations, achievements)
- Education

**Example structure:**
```markdown
# [Your Name]

## Summary
5 years of full-stack development experience...

## Skills
### Backend
- Python, Django, PostgreSQL

### Frontend
- React, TypeScript, CSS

## Experience
### Senior Software Engineer
**Company X** | Location | 2020-Present
- Achievement 1
- Achievement 2

## Education
### Bachelor's in Computer Science
University Name | 2015-2019
```

#### 3. **CV Template** (`user_info/CV_placeholder.docx`)
A DOCX template for rendering generated CVs.

**Setup:**
1. Download a template or create a new one in Microsoft Word
2. Add placeholders like `[NAME]`, `[SUMMARY]`, `[EXPERIENCE]` in the template
3. Save as `CV_placeholder.docx` in `user_info/`

The application will replace these placeholders with generated content.

#### 4. **Keywords List** (`user_info/keywords.txt`)
Keywords for first-pass filtering. Jobs containing any of these keywords pass the first filter.

**Format (one per line):**
```
Python
Django
Backend
Full-stack
Remote
```

#### 5. **Blacklist** (`user_info/blacklist.txt`)
Keywords that disqualify a job. Postings containing these are filtered out.

**Format (one per line):**
```
PHP
Require relocation
Requires clearance
```

### Database Files (Auto-created)

- `db/joblister.db` - SQLite database (auto-created on first run)
  - `job_data` - Scraped postings
  - `ai_analysis` - AI grades and analyses
  - `generated_cv` - Generated CVs for each analyzed posting

### Temporary Files (Auto-created)

All in `temp/` directory, created on each stage:
- `jobs.csv` - Raw scraped postings
- `first_filtered_file.csv` - After keyword/blacklist filtering
- `detailed_jobs.csv` - With full descriptions
- `filtered_detailed_jobs.csv` - Final filtered list
- `no_jobs.csv` - Boards that returned no results

---

## Running the Application

### Prerequisites

1. **Python 3.11+**
   ```bash
   python --version  # Must be 3.11 or higher
   ```

2. **Claude CLI installed**
   - Install: https://github.com/anthropics/claude-code
   - Verify: `claude --version`
   - Must have API access configured

3. **Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Optional: Playwright (for JS rendering)**
   ```bash
   pip install playwright
   playwright install chromium
   ```

### Setup Steps

1. **Clone/Navigate to project:**
   ```bash
   cd joblister
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure your profile:**
   - Copy examples: `cp user_info/*_example.* user_info/`
   - Edit `user_info/my_cv.md` with your CV
   - Edit `user_info/job_boards.csv` with boards to scrape
   - Edit `user_info/keywords.txt` with relevant keywords
   - Edit `user_info/blacklist.txt` with disqualifying keywords
   - Replace `user_info/CV_placeholder.docx` with your template

4. **Optional: Set up a CV template**
   - Create or download a CV template in Microsoft Word
   - Add placeholders (the app will replace them)
   - Save as `user_info/CV_placeholder.docx`

### Running the Dashboard

```bash
streamlit run visualization/Home.py
```

This launches the Streamlit web interface at `http://localhost:8501` with five pages:

1. **Home** - Dashboard overview
2. **1_Boards_without_jobs** - Boards that had no postings
3. **2_Scrape_boards** - Launch board scraping
4. **3_Scrape_jobs** - Fetch full descriptions and apply filters
5. **4_AI_analysis** - Grade jobs against your CV
6. **5_CV_generation** - Generate tailored CVs

### Workflow Example

1. Open `Home` page to see current database state
2. Go to `2_Scrape_boards` and click "Scrape boards"
   - Adjust the limit if scraping only specific boards
   - Enable "Render JS listings" for JavaScript-heavy sites
3. Review scraped postings
4. Go to `3_Scrape_jobs` to fetch full descriptions and filter
5. Check `4_AI_analysis` page to see pending jobs
6. Click "Analyze jobs" to run Claude grading on pending postings
7. Once graded, go to `5_CV_generation` to generate tailored CVs
8. Download generated CVs as DOCX files

### Running Tests

```bash
pytest tests/
```

Run specific test:
```bash
pytest tests/test_analysis.py -v
```

With coverage:
```bash
pytest tests/ --cov=.
```

### Running the nightly jobs (Docker / Kubernetes)

The scrape pipeline runs unattended as four stages, one after another. Every
stage hands over through the data directory (`JOBLISTER_DATA_DIR`, `/data` in
the images) and the job database (`DATABASE_URL`).

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
docker build -f job_scraper/Dockerfile --target ingest -t joblister-ingest .
docker build -f job_scraper/Dockerfile --target scrape -t joblister-scrape .
docker build -f api/Dockerfile -t joblister-api .
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
kubectl port-forward svc/joblister-api 8080:8080 &
API=http://localhost:8080

# 3. minikube, NodePort. On macOS with the docker driver the node IP is not
# routable from the host, so this is a tunnel you leave running:
kubectl apply -f k8s/dev/api-nodeport.yaml
API=$(minikube service joblister-api-nodeport --url)     # Linux/VM driver: http://$(minikube ip):30080
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

curl -s "$API/v1/me/analyses/due?window_days=1" -H "$A"   # how many would be graded
curl -s -X POST $API/v1/me/analyze -H "$H" -H "$A" \
  -d '{"limit":5,"window_days":1,"model":"haiku"}'        # repeat while "remaining" > 0
curl -s "$API/v1/me/analyses?limit=20&min_grade=60" -H "$A"
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
labelled `app: joblister-api`; it takes effect only on a CNI that enforces
NetworkPolicy, so run the probe in that file's header on any new cluster
(verified on minikube's kindnet: an unlabelled pod times out once the policy
has propagated, a few seconds after `kubectl apply`).
This restricts *who* can call, not how much the API itself may spend.

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
kubectl set env deploy/joblister-api ANALYSIS_MODELS=haiku,sonnet,ollama-qwen2.5:0.5b
E2E_TARGET=k8s python scripts/e2e_api.py
```

With a real `ANTHROPIC_API_KEY` set `E2E_MODEL=haiku` and skip the overrides.
The CLI takes the same knobs: `python -m ai.main_analysis <user_id> --model
… --limit … --window "-7 days"`.

**Kubernetes** (manifests in `k8s/`, schedules in Europe/Paris: scrape 00:00,
render-boards 00:45, ingest 01:00, render-postings 05:00):

```bash
kubectl apply -f k8s/pvc.yaml
kubectl create secret generic joblister-secrets \
  --from-literal=DATABASE_URL='postgresql+psycopg://...'   # see k8s/secret.example.yaml
kubectl apply -f k8s/job-migrate.yaml
kubectl apply -f k8s/cron-scrape_boards.yaml -f k8s/cron-ingest_jobs.yaml \
              -f k8s/cron-render_boards.yaml -f k8s/cron-render_postings.yaml

# The API and its gateway (after migrate-jobs); secrets are separate, see
# k8s/secret.example.yaml
docker build -f api/Dockerfile -t joblister-api .
docker build -f llm_gateway/Dockerfile -t joblister-llm-gateway .
minikube image load joblister-api:latest joblister-llm-gateway:latest
kubectl create secret generic joblister-api-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)"
kubectl create secret generic joblister-gateway-secrets \
  --from-literal=ANTHROPIC_API_KEY='...'
kubectl apply -f k8s/llm-gateway.yaml -f k8s/api.yaml \
              -f k8s/networkpolicy-llm-gateway.yaml
# reach it: see "Reaching the API" above (no Ingress yet)

# Run a stage now instead of waiting for its schedule
kubectl create job --from=cronjob/scrape-boards test-scrape
kubectl logs -f jobs/test-scrape
```

Set `suspend: true` on a CronJob to switch that stage off.

**Environment**: `DATABASE_URL`, `JOBLISTER_DATA_DIR`, `JOBLISTER_BOARDS_CSV`,
`FRANCE_TRAVAIL_CLIENT_ID` / `FRANCE_TRAVAIL_CLIENT_SECRET` (optional; the
France Travail source also needs `SEARCH_KEYWORDS` filled in), and
`CHROMIUM_NO_SANDBOX=1` (set by the `scrape` image).

#### Measurements (for tuning the schedule)

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

### Running Individual Scripts

If you want to run stages manually outside the UI:

```bash
# Scrape boards only
python -m job_scraper.scrape_boards

# Analyze jobs with Claude
python -m ai.main_analysis

# Generate CVs
python -m cv_generator.docx_gen
```

---

## Architecture

### Directory Structure

```
jobbed/
├── job_scraper/          # Board scraping & job fetching
│   ├── strategies/       # Detection strategies (JSON-LD, links, API, etc.)
│   ├── detector.py       # ATS detection
│   ├── board.py          # One board: detection + strategy dispatch
│   ├── scrape_boards.py  # Stage 1: scrape every board
│   ├── scrape_postings.py # Stage 2: fetch every posting's page
│   ├── debug_board.py    # Debug CLI: scrape one board and print it
│   └── paths.py          # Filesystem paths
├── ai/                   # AI integration (via the LLM gateway)
│   ├── gateway.py        # async client for llm_gateway
│   ├── analysis.py       # Job grading
│   ├── cv_generation.py  # CV tailoring
│   ├── grade-job.md      # Claude grading prompt
│   └── generate-cv.md    # Claude CV generation prompt
├── db/                   # Postgres access (SQLAlchemy Core)
│   ├── tables.py         # schema, migrated by alembic/
│   ├── users.py, boards.py, jobs.py, analysis.py, generated_cv.py
├── llm_gateway/          # FastAPI gateway: Claude + Ollama adapters
├── cv_generator/         # CV rendering to DOCX
│   └── docx_gen.py       # DOCX generation
├── visualization/        # Streamlit dashboard
│   ├── Home.py           # Main dashboard
│   ├── pages/            # Dashboard pages (5 total)
│   └── common.py         # Shared utilities
├── tests/                # Test suite
├── user_info/            # Configuration (gitignored)
│   ├── my_cv.md          # Your CV
│   ├── job_boards.csv    # Boards to scrape
│   ├── keywords.txt      # Include keywords
│   ├── blacklist.txt     # Exclude keywords
│   └── CV_placeholder.docx # CV template
├── db/                   # Database (gitignored)
│   └── joblister.db      # SQLite database
└── temp/                 # Temp CSVs (gitignored)
```

### Database Schema

#### job_data
Scraped job postings
```sql
- id (INTEGER PRIMARY KEY)
- company (TEXT)
- title (TEXT)
- description (TEXT)
- url (TEXT UNIQUE)
- place (TEXT)
- timestamp (DATETIME)
```

#### ai_analysis
Claude's job grades and analyses
```sql
- id (INTEGER PRIMARY KEY)
- adequation_grade (INT 0-100)
- depth_analysis (TEXT)
- ai_model (TEXT) - "haiku"
- job_id (FOREIGN KEY)
```

#### generated_cv
Generated CVs for analyzed jobs
```sql
- id (INTEGER PRIMARY KEY)
- locale (TEXT) - "en", "es", "fr", "pt"
- cv (JSON) - Full CV structure
- job_id (FOREIGN KEY)
- ai_analysis_id (FOREIGN KEY)
```

---

## Environment Variables

The application uses the following environment variable (set automatically):

- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` - Prevents Claude CLI from storing conversation memory during analyses (privacy)

If you're using a custom Claude installation, ensure the `claude` command is in your PATH.

---

## Troubleshooting

### "Claude CLI not found"
```bash
# Install Claude CLI
pip install claude-cli
# Or configure if already installed
which claude
```

### "temp/jobs.csv is empty"
- Run `2_Scrape_boards` first to populate the jobs list
- Check that `user_info/job_boards.csv` has valid board URLs

### "database unavailable"
- Ensure you have write permissions to the `db/` directory
- Delete `db/joblister.db` to reset the database
- Check that `user_info/my_cv.md` exists and is readable

### AI analysis failing
- Verify Claude CLI is installed: `claude --version`
- Check that you have API access: `claude -p "test"`
- Ensure `user_info/my_cv.md` is valid Markdown
- Check `ai/grade-job.md` and `ai/generate-cv.md` exist

### CV generation not working
- Ensure you've graded jobs first (run AI analysis)
- Check that `user_info/CV_placeholder.docx` exists
- Verify your CV has the required sections (skills, experience, education)

### Boards return no results
- Check the board URL is still active and publicly accessible
- Some boards may require authentication
- Try enabling "Render JS listings" if the board is JavaScript-heavy
- Review `temp/no_jobs.csv` for error details

---

## Performance Notes

- **First run:** Can take 10-30 minutes depending on number of boards (network I/O bound)
- **AI analysis:** ~10 seconds per job (Claude API call overhead)
- **CV generation:** ~20 seconds per job (larger model, more computation)
- **Database queries:** Fast on databases <10K jobs; consider archiving old data if larger

---

## Contributing

To contribute improvements:

1. Create a feature branch
2. Run tests: `pytest tests/`
3. Ensure lint passes: `flake8 .`
4. Submit a pull request

---

## License

[Add your license here]

---

## Feedback & Support

For issues, feature requests, or questions:
- GitHub Issues: [Link to repo]
- Email: [Your email]
