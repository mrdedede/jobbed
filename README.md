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

### Running Individual Scripts

If you want to run stages manually outside the UI:

```bash
# Scrape boards only
python -m job_scraper.main_scraper

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
│   ├── main_scraper.py   # Board scraping orchestration
│   ├── post_scraper.py   # Individual job fetching
│   └── paths.py          # Filesystem paths
├── ai/                   # AI integration (Claude)
│   ├── call_model.py     # CLI wrapper for Claude
│   ├── analysis.py       # Job grading
│   ├── cv_generation.py  # CV tailoring
│   ├── grade-job.md      # Claude grading prompt
│   └── generate-cv.md    # Claude CV generation prompt
├── db/                   # Database management
│   └── db_connection.py  # SQLite schema & queries
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
