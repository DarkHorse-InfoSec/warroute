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
    haversine_km,
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
    start: Annotated[str | None, Form()] = None,
    start_query: Annotated[str | None, Form()] = None,
    home_lat: Annotated[float | None, Form()] = None,
    home_lon: Annotated[float | None, Form()] = None,
    destination: Annotated[str | None, Form()] = None,
    destination_query: Annotated[str | None, Form()] = None,
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

    # Resolve START location. Priority:
    #   1. `start` hidden field ("lat,lon" from type-ahead tap)
    #   2. `start_query` typed text (geocoded server-side)
    #   3. `home_lat`/`home_lon` legacy power-user override fields
    #   4. settings.home_lat / settings.home_lon (.env default)
    start_lat: float | None = None
    start_lon: float | None = None
    resolved_start_label: str | None = None
    if start and "," in start:
        try:
            lat_s, lon_s = start.split(",", 1)
            start_lat = float(lat_s)
            start_lon = float(lon_s)
        except ValueError:
            start_lat = None
            start_lon = None
    if (start_lat is None or start_lon is None) and start_query and start_query.strip():
        focus = Waypoint(lat=settings.home_lat, lon=settings.home_lon)
        try:
            async with OrsClient() as ors:
                hits = await ors.geocode(start_query.strip(), focus=focus, size=1)
            if hits:
                start_lat = hits[0].lat
                start_lon = hits[0].lon
                resolved_start_label = hits[0].label or hits[0].name
        except OrsError as exc:
            logger.warning("start geocode fallback failed for %r: %s", start_query, exc)
    if start_lat is None or start_lon is None:
        start_lat = home_lat if home_lat is not None else settings.home_lat
        start_lon = home_lon if home_lon is not None else settings.home_lon

    dest_lat: float | None = None
    dest_lon: float | None = None
    resolved_destination_label: str | None = None
    if mode == "oneway":
        # Path 1: hidden field populated by the type-ahead JS ("lat,lon").
        if destination and "," in destination:
            try:
                lat_s, lon_s = destination.split(",", 1)
                dest_lat = float(lat_s)
                dest_lon = float(lon_s)
            except ValueError:
                dest_lat = None
                dest_lon = None
        # Path 2: user typed but didn't tap a result (or hidden parse failed) —
        # resolve the typed text via geocoder and use the first hit. Focus bias
        # on the resolved start so "Pizza Hut" near origin ranks above globally.
        if (dest_lat is None or dest_lon is None) and destination_query and destination_query.strip():
            focus = Waypoint(lat=start_lat, lon=start_lon)
            try:
                async with OrsClient() as ors:
                    hits = await ors.geocode(destination_query.strip(), focus=focus, size=1)
                if hits:
                    dest_lat = hits[0].lat
                    dest_lon = hits[0].lon
                    resolved_destination_label = hits[0].label or hits[0].name
            except OrsError as exc:
                logger.warning("destination geocode fallback failed for %r: %s", destination_query, exc)
        if dest_lat is None or dest_lon is None:
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min,
                    "home_lat": home_lat or settings.home_lat,
                    "home_lon": home_lon or settings.home_lon,
                },
                error=(
                    "oneway mode needs a destination - type a place name and tap a match,"
                    " or paste 'lat,lon' coordinates"
                ),
            )

    req = PlanRequest(
        home_lat=start_lat,
        home_lon=start_lon,
        duration_min=duration_min,
        mode=mode,
        destination_lat=dest_lat,
        destination_lon=dest_lon,
    )

    # Sanity check: if the geocoder returned a destination way outside the budget's
    # reachable radius, bail with a clear message naming the bad match. This catches
    # cases like "Pick and Shovel Newport VT" -> "Pick and Shovel Mine, CA" (4400 km away).
    if dest_lat is not None and dest_lon is not None:
        reachable_km = req.reachable_radius_km()
        # 2x slack: allow destinations up to 2x reachable (back-roads, longer routes)
        max_dist_km = max(reachable_km * 2.0, 50.0)
        dest_dist_km = haversine_km(start_lat, start_lon, dest_lat, dest_lon)
        if dest_dist_km > max_dist_km:
            picked = resolved_destination_label or f"{dest_lat:.4f},{dest_lon:.4f}"
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min,
                    "home_lat": req.home_lat,
                    "home_lon": req.home_lon,
                },
                error=(
                    f"Destination is {dest_dist_km:.0f} km from start - far beyond the "
                    f"{duration_min} min budget ({reachable_km:.0f} km reachable). "
                    f"Geocoder picked: {picked}. Try a more specific query, increase the "
                    f"duration, or tap a closer match in the dropdown."
                ),
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
    except OrsQuotaError:
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min,
                "home_lat": req.home_lat,
                "home_lon": req.home_lon,
            },
            error=(
                "ORS quota or rate limit hit. Wait ~60s and try again, or check"
                " your daily quota at https://openrouteservice.org/dev"
                " (free tier: 500 optimize/day, 40/min)."
            ),
        )
    except OrsAuthError:
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min,
                "home_lat": req.home_lat,
                "home_lon": req.home_lon,
            },
            error="ORS rejected the API key. Check ORS_API_KEY in .env.",
        )
    except OrsError as exc:
        logger.warning("ORS error during planning: %s", exc)
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min,
                "home_lat": req.home_lat,
                "home_lon": req.home_lon,
            },
            error=f"Routing service error: {exc}",
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
        resolved_start_label=resolved_start_label,
        resolved_destination_label=resolved_destination_label,
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
