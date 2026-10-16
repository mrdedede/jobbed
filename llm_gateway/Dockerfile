# Build from the repo root:  docker build -f llm_gateway/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only the gateway's own dependencies -- not the scraper, DB or CV stack.
COPY llm_gateway/requirements.txt llm_gateway/requirements.txt
RUN pip install --no-cache-dir -r llm_gateway/requirements.txt

COPY llm_gateway llm_gateway

RUN useradd --uid 1000 --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "llm_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
