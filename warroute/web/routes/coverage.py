"""/coverage: Leaflet map of cells colored by ownership."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from warroute.config import get_settings
from warroute.coverage.cells import OWNER_ME, all_cells
from warroute.db import transaction
from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_coverage(request: Request):  # type: ignore[no-untyped-def]
    settings = get_settings()
    return render(
        request,
        "coverage.html",
        home_lat=settings.home_lat,
        home_lon=settings.home_lon,
        radius_km=settings.home_radius_km,
    )


@router.get("/cells.geojson", response_class=JSONResponse)
async def get_cells_geojson() -> JSONResponse:
    with transaction() as conn:
        rows = all_cells(conn)

    features = []
    for row in rows:
        try:
            geom = json.loads(row.bbox_geojson)
        except (TypeError, ValueError):
            continue
        if row.wdgowars_owner == OWNER_ME:
            ownership = "me"
        elif row.wdgowars_owner:
            ownership = "rival"
        else:
            ownership = "uncaptured"
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "id": row.id,
                    "ownership": ownership,
                    "owner": row.wdgowars_owner,
                    "estimated_aps": row.estimated_total_aps,
                    "your_ap_count": row.your_ap_count,
                    "last_refreshed": row.last_refreshed.isoformat()
                    if row.last_refreshed
                    else None,
                },
            }
        )

    return JSONResponse({"type": "FeatureCollection", "features": features})
