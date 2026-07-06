"""/settings: client-side (localStorage) editor for the stateless access model.

DECISIONS.md 2026-07-04: keys + prefs live in the browser, never on the server.
This route only serves the editor shell; all reads/writes happen client-side in
the template's script (see warroute/web/static/app.js). There are no server-side
POST handlers here anymore - the old per-user-prefs endpoints (DECISIONS 2026-05-14)
are superseded by the stateless model.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

# Nav-app choices shown as radios in the client-side editor. full_route flags the
# ones that carry the whole multi-stop loop (Google Maps, GPX) vs first-stop-only.
NAV_CHOICES: list[dict[str, str | bool]] = [
    {"key": "google", "label": "Google Maps", "full_route": True},
    {"key": "gpx", "label": "GPX file (OsmAnd, Organic Maps, ...)", "full_route": True},
    {"key": "apple", "label": "Apple Maps", "full_route": False},
    {"key": "waze", "label": "Waze", "full_route": False},
    {"key": "geo", "label": "Device default (open app chooser)", "full_route": False},
]


@router.get("")
async def get_settings_page(request: Request) -> HTMLResponse:
    return render(request, "settings.html", nav_choices=NAV_CHOICES)
