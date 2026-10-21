# The nightly jobs' images. Build from the repo root:
#
#   docker build -f job_scraper/Dockerfile --target ingest -t jobbed-ingest .
#   docker build -f job_scraper/Dockerfile --target scrape -t jobbed-scrape .
#
# ingest: HTTP-only stages (ScrapeBoards, IngestJobs, the one-off migrate).
# scrape: ingest plus Playwright and Chromium, for the two render jobs.
#
# Nothing secret is baked in: DATABASE_URL and the France Travail variables
# arrive as env vars at runtime, and .dockerignore keeps user_info/ out.

# bookworm, not the floating "slim": Playwright's --with-deps maps the OS to a
# package list and does not know newer Debian releases.
FROM python:3.12-slim-bookworm AS ingest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    JOBBED_DATA_DIR=/data

WORKDIR /app

COPY job_scraper/requirements.txt job_scraper/requirements.txt
RUN pip install --no-cache-dir -r job_scraper/requirements.txt

# Only what the jobs import (see pyproject's package list for the rest).
COPY job_scraper job_scraper
COPY db db
COPY alembic alembic
COPY alembic.ini alembic.ini

# Non-root, with /data owned so a fresh volume is writable. A Kubernetes PVC
# mounted over /data takes its own ownership: set fsGroup 1000 on the pod.
RUN useradd --uid 1000 --create-home app \
    && mkdir /data && chown app /data
USER app

# The default is the nightly ingest; each CronJob overrides the command.
CMD ["python", "-m", "job_scraper.scrape_postings", "--ingest"]


FROM ingest AS scrape

ARG PLAYWRIGHT_VERSION=1.49.1

# Chromium and its system libraries are installed as root, into a path both
# users can read, then the image drops back to the unprivileged user.
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==${PLAYWRIGHT_VERSION} \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Chromium's sandbox needs kernel features a default container lacks; the
# render pool reads this and adds --no-sandbox (see job_scraper/render.py).
# Pods also need /dev/shm enlarged: an emptyDir with medium: Memory.
ENV CHROMIUM_NO_SANDBOX=1
USER app

CMD ["python", "-m", "job_scraper.scrape_boards", "--render-missed"]
