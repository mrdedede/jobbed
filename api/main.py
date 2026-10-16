import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from api.routers import auth, infra, user
from api.security import jwt_secret

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    jwt_secret()  # refuse to start unsigned, rather than 500 on first login

    yield


app = FastAPI(title="Joblister User Rest API", version="0.1.0",
              lifespan=lifespan)
app.include_router(infra.router)
app.include_router(user.router)
app.include_router(auth.router)


@app.exception_handler(RuntimeError)
async def database_error(request: Request, exc: RuntimeError) -> JSONResponse:
    """db/* wraps failures as RuntimeError carrying SQL text; log it, never
    send it.

    There is deliberately no global ValueError handler: pydantic's
    ValidationError subclasses ValueError, so it would turn response-model
    bugs into 409s. Handlers map the ValueErrors they expect themselves.
    """
    logger.exception("unhandled error on %s %s", request.method,
                     request.url.path)

    return JSONResponse(status_code=500, content={"detail": "internal error"})


Instrumentator().instrument(app).expose(app)
