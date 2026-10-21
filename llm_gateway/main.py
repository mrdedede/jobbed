from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from llm_gateway.routers import chat

app = FastAPI(title="Jobbed LLM Gateway", version="0.1.0")
app.include_router(chat.router)

Instrumentator().instrument(app).expose(app)