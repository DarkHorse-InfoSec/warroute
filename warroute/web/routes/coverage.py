"""/coverage: Leaflet map of cells colored by ownership + gang-territory overlay."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from warroute.clients.wdgowars import WdgowarsClient, WdgowarsError
from warroute.config import PUBLIC_MAP_DEFAULT_LAT, PUBLIC_MAP_DEFAULT_LON, get_settings
from warroute.coverage.cells import OWNER_ME, all_cells
from warroute.db import transaction
from warroute.web.creds import web_credentials
from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

# Our gang on WDGoWars ("Biscuits", gang_id 16 per DECISIONS.md 2026-05-11).
# Highlighted distinctly on the coverage overlay.
OUR_GANG_ID = 16

# WDGoWars /api/territories returns each gang's hull polygon, but the coordinate
# ORDER of a hull point is undocumented and was not captured in the 2026-05-11
# probe. GeoJSON requires [lon, lat]. We assume the API returns [lat, lon]
# (Leaflet's own convention, which the WDGoWars web map most likely uses) and
# swap to [lon, lat] below. IF THE OVERLAY RENDERS IN THE WRONG PLACE on first
# live load (e.g. mirrored into the ocean), flip this to False. Tracked as a
# needs-live-verification item in tasks/todo.md.
WDGOWARS_HULL_IS_LATLON = True


@router.get("")
async def get_coverage(request: Request):  # type: ignore[no-untyped-def]
    settings = get_settings()
    # Stateless tier: seat the map at a neutral center, not the operator's home
    # (security-pass 2026-07-05). The map fits to the cell grid once it loads, so
    # an operator with coverage data still lands on their area.
    return render(
        request,
        "coverage.html",
        home_lat=PUBLIC_MAP_DEFAULT_LAT,
        home_lon=PUBLIC_MAP_DEFAULT_LON,
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


def _hull_to_geojson_ring(hull: list[list[float]]) -> list[list[float]]:
    """Project an API hull into a closed GeoJSON linear ring ([lon, lat] pairs).

    Applies the WDGOWARS_HULL_IS_LATLON assumption and closes the ring (GeoJSON
    polygons must repeat the first point last). Returns [] for a degenerate hull
    (fewer than 3 points) so the caller can skip it.
    """
    if len(hull) < 3:
        return []
    ring = [[p[1], p[0]] if WDGOWARS_HULL_IS_LATLON else [p[0], p[1]] for p in hull]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


@router.get("/gangs.geojson", response_class=JSONResponse)
async def get_gangs_geojson(request: Request) -> JSONResponse:
    """Gang-territory hull polygons from WDGoWars /api/territories.

    Uses the signed-in user's WDGoWars token when saved, else the system token.
    Best-effort: on any WDGoWars error, returns an empty FeatureCollection with
    an `error` property so the map degrades to cells-only instead of failing.
    """
    creds = web_credentials(request)
    if not creds.wdgowars_token:
        # No WDGoWars key supplied: no overlay (map degrades to cells-only). Not an
        # error - the browser just hasn't attached a key.
        return JSONResponse({"type": "FeatureCollection", "features": []})
    try:
        async with WdgowarsClient(token=creds.wdgowars_token) as wdg:
            gangs = await wdg.gang_territories()
    except WdgowarsError as exc:
        logger.warning("Coverage: gang territories unavailable: %s", exc)
        return JSONResponse({"type": "FeatureCollection", "features": [], "error": str(exc)})

    features = []
    for gang in gangs:
        ring = _hull_to_geojson_ring(gang.hull)
        if not ring:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "name": gang.name,
                    "gang_id": gang.gang_id,
                    "color": gang.color,
                    "members": gang.members,
                    "points": gang.points,
                    "rank": gang.rank,
                    "is_ours": gang.gang_id == OUR_GANG_ID,
                },
            }
        )
    return JSONResponse({"type": "FeatureCollection", "features": features})
