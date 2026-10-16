"""Custom metrics the generic HTTP instrumentator (main.py) can't see,
since it only observes the HTTP layer, not which backend served a request.
"""

from prometheus_client import Counter

llm_requests_total = Counter(
    "llm_gateway_requests_total",
    "Requests handled per backend",
    ["adapter"],
)
