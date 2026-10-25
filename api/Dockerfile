# The user-facing REST API. Build from the repo root:
#
#   docker build -f api/Dockerfile -t jobbed-api .
#
# Talks to Postgres (DATABASE_URL) and to the LLM gateway over HTTP
# (GATEWAY_URL) -- no anthropic SDK or API key in this image.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

# What the handlers import: db/ for storage, ai/ for grading and CV
# generation (it reads ai/grade-job.md and ai/generate-cv.md), and job_scraper/ because filters.py reaches
# urls -> detector -> fetching, so it cannot be cut down to a few files.
COPY api api
COPY db db
COPY ai ai
COPY job_scraper job_scraper

RUN useradd --uid 1000 --create-home app
USER app

EXPOSE 8080

# stdlib, not curl: the slim image has none.
HEALTHCHECK --interval=10s --timeout=5s --retries=5 CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

# One worker on purpose: the per-user in-flight locks and the running CV
# generations live in this process; a second worker would split them.
# Trust X-Forwarded-For from any peer: only the Ingress reaches this
# ClusterIP Service, and the auth rate limits key on the real client IP.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--forwarded-allow-ips", "*"]
