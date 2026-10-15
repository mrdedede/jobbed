from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from api.routers import infra, user, auth

app = FastAPI(title="Joblister User Rest API", version="0.1.0")
app.include_router(infra.router)
app.include_router(user.router)
app.include_router(auth.router)

Instrumentator().instrument(app).expose(app)