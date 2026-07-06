"""/plan: form (GET) + run planner and render result (POST)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from warroute.clients.census import CensusClient, CensusError
from warroute.clients.ors import (
    GeocodeResult,
    OrsAuthError,
    OrsClient,
    OrsError,
    OrsQuotaError,
    RouteLeg,
    Waypoint,
    haversine_km,
)
from warroute.config import PUBLIC_MAP_DEFAULT_LAT, PUBLIC_MAP_DEFAULT_LON, get_settings
from warroute.router.gpx import (
    apple_maps_url,
    geo_uri,
    google_maps_url,
    waze_url,
    write_gpx,
    write_gpx_per_day,
)
from warroute.router.planner import (
    PlannerError,
    PlanRequest,
    PlanResult,
    Stop,
    parse_arrive_hhmm,
    persist_direct_route,
)
from warroute.router.planner import plan as run_plan
from warroute.web.creds import web_credentials
from warroute.web.routing_quota import (
    OrsResolution,
    OrsSource,
    resolve_geocode_ors_key,
    resolve_ors_key,
)
from warroute.web.templating import render
from warroute.web.user_prefs import (
    DEFAULT_NAV_APP,
    VALID_NAV_APPS,
    effective_home,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _client_ip(request: Request) -> str:
    """Real client IP for rate-limiting. Behind Caddy, request.client is the proxy;
    Caddy sets X-Real-IP to the true remote host (see infra/Caddyfile)."""
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )


def _ors_unavailable_message(source: OrsSource) -> str:
    """User-facing explanation when no ORS key could be resolved for an operation."""
    if source == OrsSource.RATE_LIMITED:
        return (
            "Too many routing requests right now. Wait a minute and try again, or add"
            " your own free OpenRouteService key in Settings to skip the shared limit."
        )
    if source == OrsSource.QUOTA_EXHAUSTED:
        return (
            "The shared routing budget for today is used up. Add your own free"
            " OpenRouteService key in Settings (https://openrouteservice.org/dev) to keep planning."
        )
    return (
        "Routing needs an OpenRouteService key and none is available. Add your own"
        " free key in Settings (https://openrouteservice.org/dev)."
    )


def _preferred_nav_app(request: Request) -> str:
    """Read the nav-app preference the browser attaches from localStorage."""
    value = (request.headers.get("x-nav-app") or "").strip().lower()
    return value if value in VALID_NAV_APPS else DEFAULT_NAV_APP


_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

# US state + DC two-letter codes, for pulling the home state out of a home label
# so a bare street query can be resolved by the Census geocoder.
_US_STATES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ]
)


def _parse_latlon(text: str) -> tuple[float, float] | None:
    """Parse a 'lat,lon' string into valid coordinates, or None. Lets a user paste
    exact coordinates for a spot no geocoder can find."""
    m = _LATLON_RE.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return (lat, lon)
    return None


def _us_state_from(text: str) -> str | None:
    """Extract a US state 2-letter code from free text (e.g. a home-address label
    like '...DERBY, VT, 05829'). Returns the LAST match, since the state sits near
    the end of an address before the ZIP."""
    found = [t.upper() for t in re.findall(r"\b[A-Za-z]{2}\b", text) if t.upper() in _US_STATES]
    return found[-1] if found else None


# In-process cache so /plan/{id}/gpx can return the GPX without re-running ORS.
_GPX_CACHE: dict[int, str] = {}
# Phase 6c.2: per-day GPX cache for roadtrip plans. Key = (plan_id, day_number).
_GPX_DAY_CACHE: dict[tuple[int, int], str] = {}


@router.get("")
async def get_plan_form(request: Request) -> HTMLResponse:
    settings = get_settings()
    eff_lat, eff_lon, eff_label = effective_home(
        request, PUBLIC_MAP_DEFAULT_LAT, PUBLIC_MAP_DEFAULT_LON
    )
    return render(
        request,
        "plan_form.html",
        defaults={
            "duration_min": settings.default_duration_min,
            "home_lat": eff_lat,
            "home_lon": eff_lon,
            "home_label": eff_label,
        },
    )


@router.post("")
async def post_plan(
    request: Request,
    duration_min: Annotated[str | None, Form()] = None,
    mode: Annotated[str, Form()] = "loop",
    start: Annotated[str | None, Form()] = None,
    start_query: Annotated[str | None, Form()] = None,
    home_lat: Annotated[float | None, Form()] = None,
    home_lon: Annotated[float | None, Form()] = None,
    destination: Annotated[str | None, Form()] = None,
    destination_query: Annotated[str | None, Form()] = None,
    stops: Annotated[list[str] | None, Form()] = None,
    arrive_by: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    # Neutral map/start fallback. In the stateless model the browser supplies the
    # real start (the form's start / home fields); the server never derives it from
    # an identity, so an anonymous request falls back to a neutral center, not any
    # operator's home (security-pass 2026-07-05).
    eff_home_lat, eff_home_lon, _eff_home_label = effective_home(
        request, PUBLIC_MAP_DEFAULT_LAT, PUBLIC_MAP_DEFAULT_LON
    )
    # Time budget. Blank/absent is allowed for oneway - the "just get me there"
    # case (a road trip, or a hop to a fixed address), where we route the direct
    # path with no forced detour. A budget is only what converts extra time into
    # AP-scanning detours. Loop mode requires one (it sets the loop's size); that
    # is enforced after mode validation below.
    budget_min: int | None = None
    if duration_min is not None and duration_min.strip():
        try:
            budget_min = int(duration_min)
        except ValueError:
            budget_min = None
    # Stateless tier (DECISIONS.md 2026-07-04): keys come from the browser via
    # headers, never the server. WiGLE/WDGoWars have no fallback; ORS is the one
    # carve-out - the user's own key, else the shared key behind a rate + quota
    # guard. Resolve ORS once and thread the resolved key through all ORS calls.
    user_creds = web_credentials(request)
    ors_res: OrsResolution = resolve_ors_key(
        user_creds.ors_api_key,
        _client_ip(request),
        day=datetime.now(UTC).strftime("%Y-%m-%d"),
        now=time.monotonic(),
    )
    if not ors_res.usable:
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min or "",
                "home_lat": home_lat or eff_home_lat,
                "home_lon": home_lon or eff_home_lon,
            },
            error=_ors_unavailable_message(ors_res.source),
        )
    ors_key = ors_res.key
    if mode not in ("loop", "oneway"):
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min or "",
                "home_lat": home_lat or eff_home_lat,
                "home_lon": home_lon or eff_home_lon,
            },
            error=f"Invalid mode '{mode}'",
        )

    if mode == "loop" and (budget_min is None or budget_min <= 0):
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min or "",
                "home_lat": home_lat or eff_home_lat,
                "home_lon": home_lon or eff_home_lon,
            },
            error=(
                "A loop route needs a time budget - it sets how far the loop goes."
                " Enter minutes, or switch to one-way to just route to a destination."
            ),
        )

    # Resolve START location. Priority:
    #   1. `start` hidden field ("lat,lon" from type-ahead tap)
    #   2. `start_query` typed text (geocoded server-side)
    #   3. `home_lat`/`home_lon` legacy power-user override fields
    #   4. Per-user saved home (or .env default when no row exists)
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
        focus = Waypoint(lat=eff_home_lat, lon=eff_home_lon)
        try:
            async with OrsClient(api_key=ors_key) as ors:
                hits = await ors.geocode(start_query.strip(), focus=focus, size=1)
            if hits:
                start_lat = hits[0].lat
                start_lon = hits[0].lon
                resolved_start_label = hits[0].label or hits[0].name
        except OrsError as exc:
            logger.warning("start geocode fallback failed for %r: %s", start_query, exc)
    if start_lat is None or start_lon is None:
        start_lat = home_lat if home_lat is not None else eff_home_lat
        start_lon = home_lon if home_lon is not None else eff_home_lon

    # Resolve STOPS. New canonical path: `stops` form list contains zero or more
    # entries shaped "lat,lon" or "lat,lon:dwell_min". Legacy fallback: `destination`
    # / `destination_query` populate a single stop when no `stops` were submitted.
    stops_for_request: list[Stop] = []
    if stops:
        for raw in stops:
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                # Format: "lat,lon[:dwell[:overnight[:HHMM]]]" (Phase 6b.3)
                parts = raw.split(":")
                lat_s, lon_s = parts[0].split(",", 1)
                dwell = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                overnight = len(parts) > 2 and parts[2].lower() == "overnight"
                stop_arrive_by = parse_arrive_hhmm(parts[3]) if len(parts) > 3 else None
                stops_for_request.append(
                    Stop(
                        lat=float(lat_s),
                        lon=float(lon_s),
                        dwell_min=dwell,
                        overnight_after=overnight,
                        arrive_by=stop_arrive_by,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning("Ignoring malformed stop %r: %s", raw, exc)

    # Legacy destination field: only honored when no stops[] were submitted.
    resolved_destination_label: str | None = None
    if not stops_for_request and mode == "oneway":
        legacy_lat: float | None = None
        legacy_lon: float | None = None
        if destination and "," in destination:
            try:
                lat_s, lon_s = destination.split(",", 1)
                legacy_lat = float(lat_s)
                legacy_lon = float(lon_s)
            except ValueError:
                legacy_lat = None
                legacy_lon = None
        if (
            (legacy_lat is None or legacy_lon is None)
            and destination_query
            and destination_query.strip()
        ):
            focus = Waypoint(lat=start_lat, lon=start_lon)
            try:
                async with OrsClient(api_key=ors_key) as ors:
                    hits = await ors.geocode(destination_query.strip(), focus=focus, size=1)
                if hits:
                    legacy_lat = hits[0].lat
                    legacy_lon = hits[0].lon
                    resolved_destination_label = hits[0].label or hits[0].name
            except OrsError as exc:
                logger.warning(
                    "destination geocode fallback failed for %r: %s", destination_query, exc
                )
        if legacy_lat is not None and legacy_lon is not None:
            stops_for_request.append(
                Stop(lat=legacy_lat, lon=legacy_lon, label=resolved_destination_label)
            )

    if mode == "oneway" and not stops_for_request:
        return render(
            request,
            "plan_form.html",
            defaults={
                "duration_min": duration_min or "",
                "home_lat": home_lat or eff_home_lat,
                "home_lon": home_lon or eff_home_lon,
            },
            error=(
                "oneway mode needs at least one stop - type an address and tap a match,"
                " or paste 'lat,lon' coordinates"
            ),
        )

    # The final stop is the canonical "destination" for back-compat hooks (direct
    # leg precheck, GMaps URL). Multi-stop plans have multiple intermediate stops
    # before this one; single-stop / legacy plans just have this one.
    dest_lat = stops_for_request[-1].lat if stops_for_request else None
    dest_lon = stops_for_request[-1].lon if stops_for_request else None

    parsed_arrive_by: datetime | None = None
    if arrive_by and arrive_by.strip():
        try:
            # HTML datetime-local inputs produce 'YYYY-MM-DDTHH:MM' (no tz).
            parsed_arrive_by = datetime.fromisoformat(arrive_by.strip())
        except ValueError:
            logger.warning("Ignoring malformed arrive_by=%r", arrive_by)

    req = PlanRequest(
        home_lat=start_lat,
        home_lon=start_lon,
        # Oneway-with-no-budget overwrites this with the direct drive time below,
        # just before persisting; the planner is skipped in that case.
        duration_min=budget_min if budget_min is not None else 0,
        mode=mode,
        stops=stops_for_request,
        direct_min=None,  # populated below after the direct-route precheck
        arrive_by=parsed_arrive_by,
        ors_api_key=ors_key,
        wigle_name=user_creds.wigle_name,
        wigle_token=user_creds.wigle_token,
        wdgowars_token=user_creds.wdgowars_token,
    )

    # Sanity check: if the geocoder returned a destination way outside the budget's
    # reachable radius, bail with a clear message naming the bad match. This catches
    # cases like "Pick and Shovel Newport VT" -> "Pick and Shovel Mine, CA" (4400 km away).
    # Only meaningful when there IS a budget; a no-budget oneway trusts the explicitly
    # tapped destination (there's no radius to compare against).
    if dest_lat is not None and dest_lon is not None and budget_min is not None:
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
                    "duration_min": duration_min or "",
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

    # For oneway plans, fetch the direct-route geometry first. This lets us:
    #   1. Reject budgets that can't even reach the destination (with a useful message)
    #   2. Show the user the direct vs detour breakdown on the result page
    #   3. Gracefully fall back to a direct-only plan when no cells fit in budget
    # Geometry is captured so the fallback can render a real polyline (vs straight line).
    direct_leg: RouteLeg | None = None
    direct_min: float | None = None
    if mode == "oneway" and dest_lat is not None and dest_lon is not None:
        try:
            async with OrsClient(api_key=ors_key) as ors:
                direct_leg = await ors.directions(
                    [
                        Waypoint(start_lat, start_lon, label="start"),
                        Waypoint(dest_lat, dest_lon, label="dest"),
                    ],
                    with_geometry=True,
                )
            direct_min = direct_leg.duration_s / 60.0
        except OrsQuotaError:
            logger.warning("ORS quota on direct-route precheck; skipping budget validation")
        except OrsError as exc:
            logger.warning("Direct-route precheck failed: %s; skipping budget validation", exc)

        if budget_min is not None and direct_min is not None and budget_min < direct_min:
            picked = resolved_destination_label or f"{dest_lat:.4f},{dest_lon:.4f}"
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min or "",
                    "home_lat": req.home_lat,
                    "home_lon": req.home_lon,
                },
                error=(
                    f"Destination is ~{direct_min:.0f} min away by direct drive ({picked}),"
                    f" but your time budget is only {duration_min} min."
                    f" Increase the budget or pick a closer destination."
                ),
            )
        # Plumb direct_min into PlanRequest so the planner's corridor filter kicks in.
        req.direct_min = direct_min

    direct_fallback_notice: str | None = None
    loop_bumped_notice: str | None = None
    synthetic_density_notice: str | None = None

    # Oneway with no time budget: the "just get me there" case (road trip, or a hop
    # to a fixed address). There's no cap to optimize AP-scanning detours against, so
    # skip the planner and route the direct path straight to the destination. Setting
    # a budget is what turns extra time into detours; with none, direct is the answer.
    result: PlanResult | None = None
    if mode == "oneway" and budget_min is None:
        if direct_leg is None:
            return render(
                request,
                "plan_form.html",
                defaults={"duration_min": "", "home_lat": req.home_lat, "home_lon": req.home_lon},
                error=(
                    "Could not reach the routing service to build a direct route. Wait a"
                    " moment and try again, or add your own OpenRouteService key in Settings."
                ),
            )
        if direct_min is not None:
            req.duration_min = max(1, ceil(direct_min))
        result = persist_direct_route(req, direct_leg)
        direct_fallback_notice = (
            "No time budget set - showing the direct route to your destination"
            + (f" (~{direct_min:.0f} min)." if direct_min is not None else ".")
            + " Set a time budget to weave AP-scanning detours in along the way."
        )

    try:
        if result is None:
            result = await run_plan(req)
    except PlannerError as exc:
        # Graceful fallback paths: never leave the user with a dead-end error.
        # Oneway: 0-cell plan using the precheck's direct leg.
        # Loop:   auto-bump the budget to the minimum viable from the planner's
        #         last-attempt duration, then retry once.
        if mode == "oneway" and direct_leg is not None:
            result = persist_direct_route(req, direct_leg)
            if direct_min is not None:
                direct_fallback_notice = (
                    f"Could not fit any AP-scanning detour in your {duration_min} min budget."
                    f" Showing the direct route ({direct_min:.0f} min)."
                    f" Increase the budget to add detour cells."
                )
            else:
                direct_fallback_notice = (
                    f"Could not fit any AP-scanning detour in your {duration_min} min budget."
                    f" Showing the direct route. Increase the budget to add detour cells."
                )
        elif mode == "loop":
            # Loop always carries a budget (enforced up front), so budget_min is set.
            assert budget_min is not None
            # Pull the planner's actual minimum-needed duration from the last attempt.
            # If we got nothing back (no candidates at all), try 2x the requested budget.
            fallback_min = exc.last_attempted_min or float(budget_min) * 2.0
            bumped = max(ceil(fallback_min * 1.15), budget_min + 10)
            if bumped > 480 or bumped <= budget_min:
                return render(
                    request,
                    "plan_form.html",
                    defaults={
                        "duration_min": duration_min or "",
                        "home_lat": req.home_lat,
                        "home_lon": req.home_lon,
                    },
                    error=f"{exc} (no viable plan even at extended budget)",
                )
            try:
                bumped_req = replace(req, duration_min=bumped)
                result = await run_plan(bumped_req)
                loop_bumped_notice = (
                    f"Your {duration_min} min budget was too tight for any loop in this area."
                    f" Auto-bumped to {bumped} min so you get a route. Decrease the budget"
                    f" next time, or accept this longer drive."
                )
                req = bumped_req  # so the result page shows the bumped budget
            except PlannerError as exc2:
                return render(
                    request,
                    "plan_form.html",
                    defaults={
                        "duration_min": duration_min or "",
                        "home_lat": req.home_lat,
                        "home_lon": req.home_lon,
                    },
                    error=(
                        f"Could not fit a loop in {duration_min} min, and the auto-bump"
                        f" to {bumped} min also failed: {exc2}"
                    ),
                )
        else:
            return render(
                request,
                "plan_form.html",
                defaults={
                    "duration_min": duration_min or "",
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
                "duration_min": duration_min or "",
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
                "duration_min": duration_min or "",
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
                "duration_min": duration_min or "",
                "home_lat": req.home_lat,
                "home_lon": req.home_lon,
            },
            error=f"Routing service error: {exc}",
        )

    # Set by run_plan, a fallback path, or the no-budget direct branch above; every
    # path that leaves result None returns before here.
    assert result is not None

    if result.synthetic_density:
        if result.auto_painted_cells > 0:
            synthetic_density_notice = (
                f"No coverage data for this area yet - painted a {result.auto_painted_cells}-cell"
                f" grid and routed a geometrically spread loop through it. Wardrive this route"
                f" and your next plan will be density-optimized."
            )
        else:
            synthetic_density_notice = (
                "All routed cells are unprobed (we haven't queried WiGLE for this area)."
                " The route is geometrically spread; wardrive + upload to populate density"
                " data for the next plan."
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

    # Per-app navigation hand-offs. "google" + "gpx" carry the full loop; the
    # rest route to the first stop only (URL-scheme limit). The user's saved
    # preference decides which one renders as the primary button.
    nav_options = [
        {"key": "google", "label": "Google Maps", "url": maps_url, "full_route": True},
        {
            "key": "apple",
            "label": "Apple Maps",
            "url": apple_maps_url(result.ordered_waypoints),
            "full_route": False,
        },
        {
            "key": "waze",
            "label": "Waze",
            "url": waze_url(result.ordered_waypoints),
            "full_route": False,
        },
        {
            "key": "geo",
            "label": "Default map app",
            "url": geo_uri(result.ordered_waypoints),
            "full_route": False,
        },
    ]
    if result.planned_route_id is not None:
        nav_options.insert(
            1,
            {
                "key": "gpx",
                "label": "GPX (OsmAnd, Organic Maps)",
                "url": f"/plan/{result.planned_route_id}/gpx",
                "full_route": True,
            },
        )
    preferred_nav_app = _preferred_nav_app(request)

    if result.planned_route_id is not None:
        gpx_xml = write_gpx(
            result.ordered_waypoints,
            track_points=None,
            name=f"WarRoute {req.duration_min}min {req.mode}",
            description=f"{len(result.chosen_cells)} cells, ~{result.estimated_new_aps} new APs",
        )
        _GPX_CACHE[result.planned_route_id] = gpx_xml
        # Phase 6c.2: cache per-day GPX too, so /plan/{id}/gpx/day/{N} can serve.
        if result.days:
            per_day = write_gpx_per_day(result.ordered_waypoints, result.days)
            for day_num, body in per_day.items():
                _GPX_DAY_CACHE[(result.planned_route_id, day_num)] = body

    return render(
        request,
        "plan_result.html",
        result=result,
        request_data=req,
        waypoints_geojson=waypoints_geojson,
        route_geometry=geometry,
        maps_url=maps_url,
        nav_options=nav_options,
        preferred_nav_app=preferred_nav_app,
        resolved_start_label=resolved_start_label,
        resolved_destination_label=resolved_destination_label,
        direct_min=direct_min,
        direct_fallback_notice=direct_fallback_notice,
        loop_bumped_notice=loop_bumped_notice,
        synthetic_density_notice=synthetic_density_notice,
    )


@router.get("/geocode", response_class=HTMLResponse)
async def get_geocode_results(
    request: Request,
    q: Annotated[str, Query()] = "",
    lat: Annotated[float | None, Query()] = None,
    lon: Annotated[float | None, Query()] = None,
    near: Annotated[str, Query()] = "",
) -> HTMLResponse:
    """HTMX endpoint: return an HTML partial of geocoder hits for the type-ahead.

    The browser sends the user's home as `lat`/`lon` (focus, for nearest-first
    sorting) and `near` (their home label, e.g. "..., DERBY, VT, 05829") so a bare
    street query like "1414 Mead Hill Road" can be resolved to the exact house by
    appending the home state. Empty / too-short queries return an empty body.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return HTMLResponse("")

    if lat is not None and lon is not None and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        focus = Waypoint(lat=lat, lon=lon)
    else:
        eff_lat, eff_lon, _ = effective_home(
            request, PUBLIC_MAP_DEFAULT_LAT, PUBLIC_MAP_DEFAULT_LON
        )
        focus = Waypoint(lat=eff_lat, lon=eff_lon)

    # 1. Raw "lat,lon" -> an exact pin. Works anywhere, including spots no geocoder
    # has (e.g. a rural house you long-pressed in Google/Apple Maps).
    coord = _parse_latlon(query)
    if coord is not None:
        lat, lon = coord
        pin = GeocodeResult(
            name=f"Pin {lat:.5f}, {lon:.5f}",
            label=f"Dropped pin at {lat:.5f}, {lon:.5f}",
            lat=lat,
            lon=lon,
            layer="pin",
        )
        return render(request, "geocode_results.html", hits=[pin], error=None)

    # 2. US street addresses (leading house number): try the US Census geocoder
    # first for house-number precision - TIGER/Line covers rural roads OSM/ORS lack.
    # Fall back to ORS if it has no match or errors.
    if query[0].isdigit():
        try:
            async with CensusClient() as census:
                census_hits = await census.geocode(query, focus=focus, size=5)
                # If the query has no state of its own, retry with the home state
                # appended (Census needs a state to pin a house number; it then
                # figures out the town). "1414 Mead Hill Road" + "VT" -> the exact
                # Derby house.
                if not census_hits and _us_state_from(query) is None:
                    home_state = _us_state_from(near)
                    if home_state:
                        census_hits = await census.geocode(
                            f"{query}, {home_state}", focus=focus, size=5
                        )
            if census_hits:
                return render(request, "geocode_results.html", hits=census_hits, error=None)
        except CensusError as exc:
            logger.info("Census geocode fell back to ORS for %r: %s", query, exc)

    # 3. ORS (worldwide): the user's own key, else the shared key under the GEOCODE
    # rate limit (separate + generous vs routing; not charged against the routing
    # daily cap). No usable key -> empty dropdown (graceful).
    ors_res = resolve_geocode_ors_key(
        web_credentials(request).ors_api_key,
        _client_ip(request),
        day=datetime.now(UTC).strftime("%Y-%m-%d"),
        now=time.monotonic(),
    )
    if not ors_res.usable:
        return HTMLResponse("")
    try:
        async with OrsClient(api_key=ors_res.key) as ors:
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


@router.get("/{plan_id}/gpx/day/{day_number}", response_class=PlainTextResponse)
async def get_plan_gpx_day(plan_id: int, day_number: int) -> PlainTextResponse:
    """Phase 6c.2: per-day GPX for roadtrip plans. One file per overnight-separated day."""
    body = _GPX_DAY_CACHE.get((plan_id, day_number))
    if body is None:
        return PlainTextResponse("day GPX not found or expired (in-memory cache)", status_code=404)
    return PlainTextResponse(
        body,
        media_type="application/gpx+xml",
        headers={
            "Content-Disposition": (
                f'attachment; filename="warroute-{plan_id}-day{day_number}.gpx"'
            )
        },
    )
