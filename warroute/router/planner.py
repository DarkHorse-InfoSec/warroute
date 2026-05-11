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

    def reachable_radius_km(self) -> float:
        """Half the time-budget for loops (you have to come back); full for one-way."""
        hours = self.duration_min / 60.0
        if self.mode == "loop":
            return hours * self.avg_speed_kmh / 2.0
        return hours * self.avg_speed_kmh

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
    leg: RouteLeg                         # ORS optimization summary (or directions if attached)
    geometry: object | None = None        # GeoJSON LineString from /directions
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

    chosen = candidates[:MAX_OPTIMIZATION_JOBS]
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
    """All scored cells whose center is within the reachable radius of home."""
    radius_km = request.reachable_radius_km()
    if radius_km <= 0:
        raise PlannerError(
            f"duration_min={request.duration_min} yields zero reachable radius"
        )

    with transaction() as conn:
        all_rows = cells_dal.all_cells(conn)
    scored = rank_cells(all_rows)
    in_range = [
        s for s in scored
        if _km_between(request.home_lat, request.home_lon, s.center_lat, s.center_lon) <= radius_km
        and s.ownership != "me"  # don't waste a slot on cells we already own
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
    """Call /optimization; if over budget, drop the lowest-scoring cell and retry."""
    budget_s = request.duration_min * 60 * (1 + DURATION_SLACK)
    while len(chosen) >= MIN_WAYPOINTS:
        jobs = [
            Waypoint(c.center_lat, c.center_lon, label=c.cell_id) for c in chosen
        ]
        leg = await ors.optimize(start=home, jobs=jobs, end=end)
        if leg.duration_s <= budget_s:
            # Reorder chosen to match ORS's optimized job ordering.
            ordered = [chosen[i] for i in leg.waypoint_order if 0 <= i < len(chosen)]
            return leg, ordered
        dropped = chosen.pop()
        drops.append(dropped.cell_id)
        logger.info(
            "Plan over budget (%.1f > %.1f min); dropped cell %s, retrying with %d cells",
            leg.duration_min,
            budget_s / 60,
            dropped.cell_id,
            len(chosen),
        )
    raise PlannerError(
        f"Could not fit any plan in {request.duration_min} min budget; tried backing off."
    )


def _persist_plan(
    request: PlanRequest,
    waypoints: list[Waypoint],
    leg: RouteLeg,
    estimated_new_aps: int,
) -> int:
    import json

    payload = json.dumps(
        [
            {"lat": w.lat, "lon": w.lon, "label": w.label}
            for w in waypoints
        ]
    )
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
