"""/runs/{id}: post-run breakdown + post-drive review map.

Plotting the APs you scanned is a PLAN.md section 9 "post-drive review only"
feature: the map renders after the CSV is uploaded, never live in-drive. WarRoute
is not the scanner - your phone's wardriving app captures the APs, Syncthing ships
the CSV to the box, the watcher ingests it, and then these observations exist.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from warroute.config import get_settings
from warroute.db import transaction
from warroute.web.templating import render

router = APIRouter()

# Cap markers so a big run (thousands of APs) doesn't ship a huge payload or lag the
# map. We plot the most recently seen first; a truncation note tells the user.
MAX_MAP_POINTS = 5000


def _require_run_data_exposed() -> None:
    """Guard the run-data endpoints. These serve exact scanned-AP coordinates
    (including the operator's home network), so they are OFF unless the deployment
    explicitly opts in via `expose_run_data` - which must only be set on a trusted,
    auth-gated deployment, never the public Caddyfile (security-pass 2026-07-05).
    Returns a 404 (not 403) so a public instance does not even advertise the route."""
    if not get_settings().expose_run_data:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/{run_id}")
async def get_run(run_id: int, request: Request):  # type: ignore[no-untyped-def]
    _require_run_data_exposed()
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT id, source, csv_path, csv_sha256, started_at, ended_at,
                   total_aps, new_aps, distance_km, points_earned,
                   uploaded_wigle_at, uploaded_wdgowars_at, wdgowars_run_id, created_at
            FROM sessions WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")

        related_plan = conn.execute(
            """
            SELECT id, duration_min, mode, estimated_new_aps, estimated_drive_min
            FROM planned_routes WHERE actual_session_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        # Post-drive review map: how many APs from this run have a location, plus a
        # center to seat the map before it fits to the points.
        geo = conn.execute(
            """
            SELECT COUNT(*) AS n, AVG(first_seen_lat) AS clat, AVG(first_seen_lon) AS clon
            FROM observations
            WHERE first_seen_session = ?
              AND first_seen_lat IS NOT NULL AND first_seen_lon IS NOT NULL
            """,
            (run_id,),
        ).fetchone()

    mapped = int(geo["n"] or 0)
    return render(
        request,
        "run.html",
        run=dict(row),
        related_plan=dict(related_plan) if related_plan else None,
        mapped_aps=mapped,
        map_center_lat=geo["clat"],
        map_center_lon=geo["clon"],
        map_truncated=mapped > MAX_MAP_POINTS,
        map_cap=MAX_MAP_POINTS,
    )


@router.get("/{run_id}/observations.geojson")
async def get_run_observations(run_id: int) -> JSONResponse:
    """GeoJSON of the APs first seen on this run, plotted where they were seen.

    Powers the post-drive review map. Fetched client-side by run.html, mirroring
    the coverage page's /coverage/cells.geojson pattern. Gated: serves exact AP
    coordinates, so it 404s unless `expose_run_data` is enabled.
    """
    _require_run_data_exposed()
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT bssid, ssid, encryption, first_seen_lat, first_seen_lon, times_seen
            FROM observations
            WHERE first_seen_session = ?
              AND first_seen_lat IS NOT NULL AND first_seen_lon IS NOT NULL
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (run_id, MAX_MAP_POINTS),
        ).fetchall()

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [r["first_seen_lon"], r["first_seen_lat"]],
            },
            "properties": {
                "bssid": r["bssid"],
                "ssid": r["ssid"],
                "encryption": r["encryption"],
                "times_seen": r["times_seen"],
            },
        }
        for r in rows
    ]
    return JSONResponse({"type": "FeatureCollection", "features": features})
