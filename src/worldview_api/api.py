"""FastAPI app factory. Routes live in worldview_api.routers.*; request/response
models in worldview_api.schemas; ingest subprocess handling in
worldview_api.ingest.orchestrator."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import close_pool, get_pool
from .routers import ALL_ROUTERS


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="worldview-api", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

for _router in ALL_ROUTERS:
    app.include_router(_router)
