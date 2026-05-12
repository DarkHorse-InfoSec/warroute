"""/plan: form (GET) + run planner and render result (POST)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from warroute.clients.ors import (
    OrsAuthError,
    OrsClient,
    OrsError,
    OrsQuotaError,
    Waypoint,
)
from warroute.config import get_settings
from warroute.router.gpx import google_maps_url, write_gpx
from warroute.router.planner import PlannerError, PlanRequest
from warroute.router.planner import plan as run_plan
from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

# In-process cache so /plan/{id}/gpx can return the GPX without re-running ORS.
_GPX_CACHE: dict[int, str] = {}


@router.get("")
async def get_plan_form(request: Request) -> HTMLResponse:
    settings = get_settings()
    return render(
        request,
        "plan_form.html",
        defaults={
            "duration_min": settings.default_duration_min,
            "home_lat": settings.home_lat,
            "home_lon": settings.home_lon,
        },
    )


@router.post("")
async def post_plan(
    request: Request,
    duration_min: Annotated[int, Form()] = 90,
    mode: Annotated[str, Form()] = "loop",
    home_lat: Annotated[float | None, Form()] = None,
    home_lon: Annotated[float | None, Form()] = None,
    destination: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    settings = get_settings()
    if mode not in ("loop", "oneway"):
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min,
                "home_lat": home_lat or settings.home_lat,
                "home_lon": home_lon or settings.home_lon,
            },
            error=f"Invalid mode '{mode}'",
        )

    dest_lat: float | None = None
    dest_lon: float | None = None
    if mode == "oneway":
        if not destination or "," not in destination:
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min,
                    "home_lat": home_lat or settings.home_lat,
                    "home_lon": home_lon or settings.home_lon,
                },
                error="oneway mode needs a destination as 'lat,lon'",
            )
        try:
            lat_s, lon_s = destination.split(",")
            dest_lat = float(lat_s)
            dest_lon = float(lon_s)
        except ValueError:
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min,
                    "home_lat": home_lat or settings.home_lat,
                    "home_lon": home_lon or settings.home_lon,
                },
                error="destination must be 'lat,lon' with two numbers",
            )

    req = PlanRequest(
        home_lat=home_lat if home_lat is not None else settings.home_lat,
        home_lon=home_lon if home_lon is not None else settings.home_lon,
        duration_min=duration_min,
        mode=mode,
        destination_lat=dest_lat,
        destination_lon=dest_lon,
    )

    try:
        result = await run_plan(req)
    except PlannerError as exc:
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min,
                "home_lat": req.home_lat,
                "home_lon": req.home_lon,
            },
            error=str(exc),
        )

    waypoints_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [w.lon, w.lat]},
                "properties": {"label": w.label or "", "order": idx},
            }
            for idx, w in enumerate(result.ordered_waypoints)
        ],
    }
    geometry = result.geometry if result.geometry else None
    maps_url = google_maps_url(result.ordered_waypoints)

    if result.planned_route_id is not None:
        gpx_xml = write_gpx(
            result.ordered_waypoints,
            track_points=None,
            name=f"WarRoute {req.duration_min}min {req.mode}",
            description=f"{len(result.chosen_cells)} cells, ~{result.estimated_new_aps} new APs",
        )
        _GPX_CACHE[result.planned_route_id] = gpx_xml

    return render(
        request,
        "plan_result.html",
        result=result,
        request_data=req,
        waypoints_geojson=waypoints_geojson,
        route_geometry=geometry,
        maps_url=maps_url,
    )


@router.get("/geocode", response_class=HTMLResponse)
async def get_geocode_results(
    request: Request,
    q: Annotated[str, Query()] = "",
) -> HTMLResponse:
    """HTMX endpoint: return an HTML partial of geocoder hits for the type-ahead.

    Empty / too-short queries return an empty body (clears the dropdown). Errors
    render a small flash but never propagate — the form stays usable.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return HTMLResponse("")

    settings = get_settings()
    focus = Waypoint(lat=settings.home_lat, lon=settings.home_lon)
    try:
        async with OrsClient() as ors:
            hits = await ors.geocode(query, focus=focus, size=5)
    except OrsAuthError:
        return render(request, "geocode_results.html", hits=[], error="ORS auth error")
    except OrsQuotaError:
        return render(
            request, "geocode_results.html", hits=[], error="ORS geocode quota exhausted today"
        )
    except OrsError as exc:
        logger.warning("geocode failed for q=%r: %s", query, exc)
        return render(request, "geocode_results.html", hits=[], error="Geocoder error")

    return render(request, "geocode_results.html", hits=hits, error=None)


@router.get("/{plan_id}/gpx", response_class=PlainTextResponse)
async def get_plan_gpx(plan_id: int) -> PlainTextResponse:
    body = _GPX_CACHE.get(plan_id)
    if body is None:
        return PlainTextResponse("plan not found or expired (in-memory cache)", status_code=404)
    return PlainTextResponse(
        body,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="warroute-{plan_id}.gpx"'},
    )
