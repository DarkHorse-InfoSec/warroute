"""FastAPI app factory.

Run via `warroute serve` or `uvicorn warroute.web.app:app --reload`.
Auth is intentionally absent: production deploy puts HTTP basic auth at the
Caddy layer (single-tenant), per PLAN.md section 3.4. Don't add a login form here.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from warroute import __version__
from warroute.db import run_migrations
from warroute.web.routes import coverage, dashboard, plan, runs, settings, sync

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"

# Content-Security-Policy, emitted per-request with a fresh nonce (Eng #36 finding #4).
# script-src is 'self' + a per-request nonce ONLY: no 'unsafe-inline', no CDN hosts
# (htmx + leaflet are self-hosted under /static/vendor, so 'self' covers them). Inline
# <script> blocks carry nonce="{{ request.state.csp_nonce }}"; htmx stamps the same
# nonce onto scripts it swaps in (base.html sets htmx.config.inlineScriptNonce).
# style-src keeps 'unsafe-inline' on purpose: Leaflet and many template style="" attrs
# need it, and style injection is far lower risk than script injection.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org "
    "https://*.basemaps.cartocdn.com; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

# Permissions-Policy: WarRoute uses no geolocation/camera/mic/etc., so deny them all
# (Eng #36 finding #6; parity with the sibling tool + stego apps).
_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Mint a per-request CSP nonce and set the CSP + Permissions-Policy headers.

    The nonce is stashed on request.state so templates can render it into inline
    <script nonce="..."> tags. Static/other security headers (HSTS, X-Frame-Options,
    ...) stay at the Caddy edge; CSP lives here because it needs the per-request nonce.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP_TEMPLATE.format(nonce=nonce)
        response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY
        return response


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

    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard.router)
    app.include_router(plan.router, prefix="/plan")
    app.include_router(coverage.router, prefix="/coverage")
    app.include_router(runs.router, prefix="/runs")
    app.include_router(settings.router, prefix="/settings")
    app.include_router(sync.router, prefix="/sync")

    return app


app = create_app()
