"""FastAPI app factory.

Run via `warroute serve` or `uvicorn warroute.web.app:app --reload`.
Auth is intentionally absent: production deploy puts HTTP basic auth at the
Caddy layer (single-tenant), per PLAN.md section 3.4. Don't add a login form here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from warroute import __version__
from warroute.db import run_migrations
from warroute.web.routes import coverage, dashboard, plan, runs, settings

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    run_migrations()
    logger.info("WarRoute web ready")
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="WarRoute",
        version=__version__,
        docs_url=None,  # single-tenant; no need to expose Swagger
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard.router)
    app.include_router(plan.router, prefix="/plan")
    app.include_router(coverage.router, prefix="/coverage")
    app.include_router(runs.router, prefix="/runs")
    app.include_router(settings.router, prefix="/settings")

    return app


app = create_app()
