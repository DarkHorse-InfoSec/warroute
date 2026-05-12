"""Greedy-pick + ORS optimization planner.

Algorithm (PLAN.md §3.3, adapted for ORS):
  1. Compute reachable radius from time budget (loop -> half the time can be outbound).
  2. Rank all cells in radius by score (scorer.rank_cells).
  3. Greedy-pick top K = MAX_OPTIMIZATION_JOBS (~25) cells.
  4. Hand to ORS /optimization with vehicle start=home, end=home (loop) or end=destination.
  5. If returned duration > budget * (1 + slack), drop lowest-scoring and retry.
  6. Return: ordered waypoints, geometry (via subsequent /directions call), persisted plan id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from warroute.clients.ors import (
    MAX_OPTIMIZATION_JOBS,
    OrsClient,
    OrsError,
    RouteLeg,
    Waypoint,
)
from warroute.coverage import cells as cells_dal
from warroute.db import transaction
from warroute.router.scorer import CellScore, rank_cells

logger = logging.getLogger(__name__)

# Tuning constants. Tunable later via config if rural-VT defaults turn out wrong.
DEFAULT_AVG_SPEED_KMH = 40.0
DURATION_SLACK = 0.10  # accept routes up to 10% over the requested budget
MIN_WAYPOINTS = 2  # if we can't fit at least 2 cells, plan is useless
EARTH_KM_PER_DEG_LAT = 111.32


class PlannerError(RuntimeError):
    """Planner could not satisfy the request (budget too tight, no candidates, etc.)."""


@dataclass
class PlanRequest:
    home_lat: float
    home_lon: float
    duration_min: int
    mode: str = "loop"  # 'loop' | 'oneway'
    destination_lat: float | None = None
    destination_lon: float | None = None
    avg_speed_kmh: float = DEFAULT_AVG_SPEED_KMH
    direct_min: float | None = None  # T_direct in min (oneway only); enables corridor filter

    def reachable_radius_km(self) -> float:
        """Half the time-budget for loops (you have to come back); full for one-way."""
        hours = self.duration_min / 60.0
        if self.mode == "loop":
            return hours * self.avg_speed_kmh / 2.0
        return hours * self.avg_speed_kmh

    def detour_budget_min(self) -> float:
        """Minutes left over for AP-scanning detours after the direct drive (oneway)."""
        if self.mode != "oneway" or self.direct_min is None:
            return float(self.duration_min)
        return max(0.0, self.duration_min - self.direct_min)

    def corridor_half_width_km(self) -> float:
        """Max distance from the home->destination line for a cell to be a viable candidate.

        Heuristic: with detour budget S minutes, and assuming we visit ~4 cells, each cell
        adds ~2 * d_to_corridor of out-and-back. So d_max ≈ S * speed / (60 * 8). The 8 in
        the denominator is a back-off-friendly upper bound (real picks rarely visit 4 cells
        at the max distance; the planner backs off greedy until the route fits).
        """
        return self.detour_budget_min() * self.avg_speed_kmh / 60.0 / 8.0

    def end_waypoint(self) -> Waypoint:
        if self.mode == "loop":
            return Waypoint(self.home_lat, self.home_lon, label="Home")
        if self.destination_lat is None or self.destination_lon is None:
            raise PlannerError("oneway mode requires destination_lat + destination_lon")
        return Waypoint(self.destination_lat, self.destination_lon, label="Destination")


@dataclass
class PlanResult:
    request: PlanRequest
    chosen_cells: list[CellScore]
    ordered_waypoints: list[Waypoint]
    leg: RouteLeg  # ORS optimization summary (or directions if attached)
    geometry: object | None = None  # GeoJSON LineString from /directions
    estimated_new_aps: int = 0
    planned_route_id: int | None = None
    drops_for_slack: list[str] = field(default_factory=list)

    @property
    def estimated_drive_min(self) -> float:
        return self.leg.duration_min


async def plan(request: PlanRequest, attach_geometry: bool = True) -> PlanResult:
    """End-to-end plan. Reads cells from DB, calls ORS, persists planned_routes row."""
    candidates = _candidate_cells(request)
    if not candidates:
        raise PlannerError(
            "No scored cells in reachable radius. Run `warroute coverage refresh` first."
        )

    home = Waypoint(request.home_lat, request.home_lon, label="Home")
    end = request.end_waypoint()

    # Right-size the initial cell count to the budget. A 20-min plan can't fit
    # 25 cells; starting with 25 just burns ORS calls on doomed iterations.
    # Heuristic: ~1 cell per 8 minutes of budget (with rural back-and-forth).
    est_initial = max(MIN_WAYPOINTS, min(MAX_OPTIMIZATION_JOBS, request.duration_min // 8))
    chosen = candidates[:est_initial]
    drops: list[str] = []

    async with OrsClient() as ors:
        leg, chosen = await _solve_with_backoff(ors, home, end, chosen, request, drops)

        geometry = None
        if attach_geometry and chosen:
            full_route_points = (
                [home]
                + [Waypoint(c.center_lat, c.center_lon, label=f"Cell {c.cell_id}") for c in chosen]
                + [end]
            )
            try:
                directions_leg = await ors.directions(full_route_points, with_geometry=True)
                geometry = directions_leg.geometry
                # Prefer directions's distance/duration if available - more accurate than VRP estimate
                leg = RouteLeg(
                    distance_m=directions_leg.distance_m,
                    duration_s=directions_leg.duration_s,
                    geometry=directions_leg.geometry,
                    waypoint_order=leg.waypoint_order,
                    raw=leg.raw,
                )
            except OrsError as exc:
                logger.warning("Directions follow-up failed; keeping VRP-only result: %s", exc)

    ordered_waypoints = (
        [home]
        + [Waypoint(c.center_lat, c.center_lon, label=f"Cell {c.cell_id}") for c in chosen]
        + [end]
    )

    estimated_new_aps = sum(c.estimated_aps for c in chosen if c.ownership != "me")

    plan_id = _persist_plan(request, ordered_waypoints, leg, estimated_new_aps)

    return PlanResult(
        request=request,
        chosen_cells=chosen,
        ordered_waypoints=ordered_waypoints,
        leg=leg,
        geometry=geometry,
        estimated_new_aps=estimated_new_aps,
        planned_route_id=plan_id,
        drops_for_slack=drops,
    )


def _candidate_cells(request: PlanRequest) -> list[CellScore]:
    """Cells that are viable detour candidates given the request.

    Loop mode: cells within reachable_radius of home (the symmetric old behavior).

    Oneway mode with `direct_min` set: cells within `corridor_half_width_km` of the
    home->destination line segment. This keeps detours along the path instead of
    radiating around home (which produced absurd 80-min routes for 15-min destinations).
    """
    radius_km = request.reachable_radius_km()
    if radius_km <= 0:
        raise PlannerError(f"duration_min={request.duration_min} yields zero reachable radius")

    with transaction() as conn:
        all_rows = cells_dal.all_cells(conn)
    scored = rank_cells(all_rows)

    use_corridor = (
        request.mode == "oneway"
        and request.destination_lat is not None
        and request.destination_lon is not None
        and request.direct_min is not None
    )
    if use_corridor:
        # `use_corridor` already guarantees these are not None; assert for mypy.
        assert request.destination_lat is not None
        assert request.destination_lon is not None
        dest_lat = request.destination_lat
        dest_lon = request.destination_lon
        corridor_km = max(request.corridor_half_width_km(), 2.0)  # min 2km for tight budgets
        in_range = [
            s
            for s in scored
            if _point_to_segment_km(
                s.center_lat,
                s.center_lon,
                request.home_lat,
                request.home_lon,
                dest_lat,
                dest_lon,
            )
            <= corridor_km
            and s.ownership != "me"
        ]
    else:
        in_range = [
            s
            for s in scored
            if _km_between(request.home_lat, request.home_lon, s.center_lat, s.center_lon)
            <= radius_km
            and s.ownership != "me"
        ]
    return in_range


async def _solve_with_backoff(
    ors: OrsClient,
    home: Waypoint,
    end: Waypoint,
    chosen: list[CellScore],
    request: PlanRequest,
    drops: list[str],
) -> tuple[RouteLeg, list[CellScore]]:
    """Call /optimization; if over budget, halve the cell list and retry.

    Halve-style back-off bounds the call count at ceil(log2(N)) instead of N.
    Critical for ORS's 40-calls-per-minute rate limit: a 25-cell drop-one back-off
    burns ~70 calls/min mid-plan, hitting 429 quickly.
    """
    budget_s = request.duration_min * 60 * (1 + DURATION_SLACK)
    last_leg: RouteLeg | None = None
    while len(chosen) >= MIN_WAYPOINTS:
        jobs = [Waypoint(c.center_lat, c.center_lon, label=c.cell_id) for c in chosen]
        leg = await ors.optimize(start=home, jobs=jobs, end=end)
        last_leg = leg
        if leg.duration_s <= budget_s:
            ordered = [chosen[i] for i in leg.waypoint_order if 0 <= i < len(chosen)]
            return leg, ordered
        # Halve the chosen list (keep the higher-scoring half).
        new_len = max(MIN_WAYPOINTS, len(chosen) // 2)
        dropped_cells = chosen[new_len:]
        drops.extend(c.cell_id for c in dropped_cells)
        chosen = chosen[:new_len]
        logger.info(
            "Plan over budget (%.1f > %.1f min); halved to %d cells",
            leg.duration_min,
            budget_s / 60,
            len(chosen),
        )
        # If we just halved down to MIN_WAYPOINTS and it still overshot, no more iters left
        # — exit loop so we don't re-call with the same cells (the previous iteration's leg
        # is still our best info, and we'll raise after).
        if len(chosen) == MIN_WAYPOINTS and new_len == len(chosen) and leg.duration_s > budget_s:
            break
    if last_leg is not None and last_leg.duration_s <= budget_s * 1.2:
        # Edge: last_leg was 20% over budget but we ran out of cells to drop. Accept it
        # with a small slack rather than fail entirely.
        ordered = [chosen[i] for i in last_leg.waypoint_order if 0 <= i < len(chosen)]
        return last_leg, ordered
    raise PlannerError(
        f"Could not fit any plan in {request.duration_min} min budget; tried backing off."
    )


def persist_direct_route(request: PlanRequest, direct_leg: RouteLeg) -> PlanResult:
    """Build a 0-cell PlanResult that's just the direct drive home -> destination.

    Used as the graceful fallback when no AP-scanning detour fits in the budget,
    so the user still gets to their destination with a Google Maps link instead
    of a hard error. Only valid for oneway plans (destination required).
    """
    if request.destination_lat is None or request.destination_lon is None:
        raise PlannerError("Direct-only fallback requires destination_lat + destination_lon")
    home = Waypoint(request.home_lat, request.home_lon, label="Home")
    end = Waypoint(request.destination_lat, request.destination_lon, label="Destination")
    waypoints = [home, end]
    plan_id = _persist_plan(request, waypoints, direct_leg, estimated_new_aps=0)
    return PlanResult(
        request=request,
        chosen_cells=[],
        ordered_waypoints=waypoints,
        leg=direct_leg,
        geometry=direct_leg.geometry,
        estimated_new_aps=0,
        planned_route_id=plan_id,
        drops_for_slack=[],
    )


def _persist_plan(
    request: PlanRequest,
    waypoints: list[Waypoint],
    leg: RouteLeg,
    estimated_new_aps: int,
) -> int:
    import json

    payload = json.dumps([{"lat": w.lat, "lon": w.lon, "label": w.label} for w in waypoints])
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO planned_routes (
                created_at, home_lat, home_lon, duration_min, mode,
                destination_lat, destination_lon, waypoints_json,
                estimated_new_aps, estimated_drive_min
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                request.home_lat,
                request.home_lon,
                request.duration_min,
                request.mode,
                request.destination_lat,
                request.destination_lon,
                payload,
                estimated_new_aps,
                leg.duration_min,
            ),
        )
        return int(cursor.lastrowid or 0)


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _point_to_segment_km(
    plat: float, plon: float, alat: float, alon: float, blat: float, blon: float
) -> float:
    """Distance (km) from point P to segment AB.

    Uses an equirectangular projection around the segment midpoint - good to ~1% for
    segments under ~50km at mid-latitudes, plenty for WarRoute corridor filtering.
    """
    import math

    KM_PER_DEG_LAT = 111.32
    lat0 = (alat + blat) / 2.0
    cos_lat0 = math.cos(math.radians(lat0))
    ax = alon * cos_lat0 * KM_PER_DEG_LAT
    ay = alat * KM_PER_DEG_LAT
    bx = blon * cos_lat0 * KM_PER_DEG_LAT
    by = blat * KM_PER_DEG_LAT
    px = plon * cos_lat0 * KM_PER_DEG_LAT
    py = plat * KM_PER_DEG_LAT
    abx = bx - ax
    aby = by - ay
    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / ab_len_sq
    t = max(0.0, min(1.0, t))
    fx = ax + t * abx
    fy = ay + t * aby
    return math.hypot(px - fx, py - fy)
