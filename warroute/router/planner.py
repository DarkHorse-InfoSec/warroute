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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from warroute.clients.ors import (
    MAX_OPTIMIZATION_JOBS,
    OrsClient,
    OrsError,
    RouteLeg,
    Waypoint,
)
from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.db import transaction
from warroute.router.scorer import CellScore, rank_cells

logger = logging.getLogger(__name__)

# Tuning constants. Tunable later via config if rural-VT defaults turn out wrong.
DEFAULT_AVG_SPEED_KMH = 40.0
DURATION_SLACK = 0.10  # accept routes up to 10% over the requested budget
MIN_WAYPOINTS = 2  # if we can't fit at least 2 cells, plan is useless
EARTH_KM_PER_DEG_LAT = 111.32
# Hard cap on how many cells we'll auto-paint per plan when the DB is empty.
# Sized to comfortably cover a ~120-min loop in rural terrain (~840 cells) but
# refuse to silently insert 50k+ rows for a runaway 8-hour request - those
# should run `coverage refresh` deliberately instead.
MAX_AUTO_PAINT_CELLS = 2000
# Phase 6b: small safety buffer added to the computed departure time so the user
# isn't expected to leave the door at the literal second the route says.
DEPARTURE_BUFFER_MIN = 2


class PlannerError(RuntimeError):
    """Planner could not satisfy the request (budget too tight, no candidates, etc.).

    Carries `last_attempted_min` when an ORS call was actually made: callers can use
    it to auto-bump the budget (e.g. loop mode: "minimum viable loop here is 46 min,
    you asked for 20; retry at 50").
    """

    def __init__(self, message: str, last_attempted_min: float | None = None) -> None:
        super().__init__(message)
        self.last_attempted_min = last_attempted_min


@dataclass(frozen=True)
class Stop:
    """A user-specified intermediate destination on a multi-stop route.

    `dwell_min` is "how long the user is parked here" (e.g. dropping off a kid
    takes 5 minutes). It counts against the duration budget but the planner
    doesn't add any cells for it - just subtracts the time.

    `overnight_after` reserved for Phase 6c roadtrip mode (split route into days
    after this stop). Ignored in 6a.
    """

    lat: float
    lon: float
    label: str | None = None
    dwell_min: int = 0
    overnight_after: bool = False


@dataclass
class PlanRequest:
    home_lat: float
    home_lon: float
    duration_min: int
    mode: str = "loop"  # 'loop' | 'oneway'
    stops: list[Stop] = field(default_factory=list)
    avg_speed_kmh: float = DEFAULT_AVG_SPEED_KMH
    direct_min: float | None = None  # T_direct in min (oneway only); enables corridor filter
    arrive_by: datetime | None = None  # Phase 6b: when set, planner computes departure

    @property
    def destination_lat(self) -> float | None:
        """Back-compat alias: the last stop is the canonical destination."""
        return self.stops[-1].lat if self.stops else None

    @property
    def destination_lon(self) -> float | None:
        return self.stops[-1].lon if self.stops else None

    @property
    def is_multistop(self) -> bool:
        """True if this request needs per-segment routing.

        Per-segment routing kicks in for:
          - loop mode + 1 or more stops (home -> stops... -> home is 2+ segments)
          - oneway mode + 2 or more stops (home -> stops... -> last is 2+ segments)

        Single-stop oneway (home -> destination) is a single segment - the
        existing `plan()` path handles it.
        """
        if not self.stops:
            return False
        if self.mode == "loop":
            return True
        return len(self.stops) > 1

    def total_dwell_min(self) -> int:
        return sum(s.dwell_min for s in self.stops)

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
        return max(0.0, self.duration_min - self.direct_min - self.total_dwell_min())

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
        if not self.stops:
            raise PlannerError("oneway mode requires at least one stop")
        last = self.stops[-1]
        return Waypoint(last.lat, last.lon, label=last.label or "Destination")


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
    synthetic_density: bool = False  # True when every chosen cell is unprobed
    auto_painted_cells: int = 0  # rows added by the empty-DB grid paint, if any
    departure_at: datetime | None = None  # Phase 6b: derived from arrive_by - duration

    @property
    def estimated_drive_min(self) -> float:
        return self.leg.duration_min

    @property
    def total_trip_min(self) -> float:
        """Drive time + total dwell at user-specified stops."""
        return self.estimated_drive_min + self.request.total_dwell_min()


async def plan(request: PlanRequest, attach_geometry: bool = True) -> PlanResult:
    """End-to-end plan. Reads cells from DB, calls ORS, persists planned_routes row.

    For multi-stop requests (`request.is_multistop`), routing is delegated to
    `_plan_multistop` which runs ORS optimization per segment.
    """
    if request.is_multistop:
        return await _plan_multistop(request, attach_geometry)
    candidates = _candidate_cells(request)
    auto_painted = 0
    if not candidates:
        # No cells in the DB for this area yet (typical for a brand-new install or
        # an area `coverage refresh` has never touched). Paint the grid for the
        # reachable radius - rows only, no WiGLE calls - and re-rank. Unprobed cells
        # get a unit density proxy via the scorer, so we produce a geometrically
        # spread plan the user can wardrive to seed real density data.
        auto_painted = _paint_grid_for_request(request)
        candidates = _candidate_cells(request)
        if not candidates:
            raise PlannerError(
                "Could not generate any candidate cells around the start point."
                " Check HOME_LAT/HOME_LON or the reachable radius."
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
    synthetic = bool(chosen) and all(not c.probed for c in chosen)

    plan_id = _persist_plan(request, ordered_waypoints, leg, estimated_new_aps)

    return _attach_departure_time(
        PlanResult(
            request=request,
            chosen_cells=chosen,
            ordered_waypoints=ordered_waypoints,
            leg=leg,
            geometry=geometry,
            estimated_new_aps=estimated_new_aps,
            planned_route_id=plan_id,
            drops_for_slack=drops,
            synthetic_density=synthetic,
            auto_painted_cells=auto_painted,
        )
    )


def _attach_departure_time(result: PlanResult) -> PlanResult:
    """Phase 6b: compute departure time when the request has arrive_by set.

    Persists a row to scheduled_departures so a future ntfy job can fire an
    alarm at (departure - 5 min). Returns the result with departure_at populated.

    No-op when arrive_by is None or the plan wasn't persisted (planned_route_id
    is None).
    """
    if result.request.arrive_by is None or result.planned_route_id is None:
        return result
    total_min = result.total_trip_min + DEPARTURE_BUFFER_MIN
    departure = result.request.arrive_by - timedelta(minutes=total_min)
    with transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO scheduled_departures (plan_id, departure_at, arrive_by)
            VALUES (?, ?, ?)
            """,
            (
                result.planned_route_id,
                departure.replace(tzinfo=None).isoformat()
                if departure.tzinfo
                else departure.isoformat(),
                result.request.arrive_by.replace(tzinfo=None).isoformat()
                if result.request.arrive_by.tzinfo
                else result.request.arrive_by.isoformat(),
            ),
        )
    return replace(result, departure_at=departure)


def _paint_grid_for_request(request: PlanRequest) -> int:
    """Insert grid cells covering the request's reachable radius around the start.

    Used as the on-demand bootstrap when the DB has no cells in this area yet.
    Inserts rows only (id, center, bbox) - density and ownership stay NULL so the
    scorer marks them `probed=False`. Capped at MAX_AUTO_PAINT_CELLS; if the
    request's radius would generate more, we paint nothing and let the caller
    raise (the user should run `warroute coverage refresh` deliberately for that
    size of area).

    Returns the number of rows inserted (0 if nothing new).
    """
    radius_km = request.reachable_radius_km()
    grid = cells_in_radius(request.home_lat, request.home_lon, radius_km)
    if len(grid) > MAX_AUTO_PAINT_CELLS:
        logger.warning(
            "Grid auto-paint refused: %d cells for radius %.1f km exceeds cap %d",
            len(grid),
            radius_km,
            MAX_AUTO_PAINT_CELLS,
        )
        return 0
    with transaction() as conn:
        inserted = cells_dal.upsert_grid(conn, grid)
    if inserted:
        logger.info(
            "Auto-painted %d cells (radius %.1f km) into empty area around (%.4f, %.4f)",
            inserted,
            radius_km,
            request.home_lat,
            request.home_lon,
        )
    return inserted


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
        f"Could not fit any plan in {request.duration_min} min budget; tried backing off.",
        last_attempted_min=last_leg.duration_min if last_leg else None,
    )


async def _plan_multistop(
    request: PlanRequest, attach_geometry: bool = True
) -> PlanResult:
    """Multi-leg plan: route each consecutive (start, stop) pair as its own segment.

    Each segment runs its own candidate selection + ORS optimization, with back-off
    if the segment overshoots its share of the time budget. The aggregated route is
    home -> stop[0] (with cells in between) -> stop[1] (with cells) -> ... -> end,
    where end = home (loop mode) or stops[-1] (oneway).

    v1 simplifications:
      - Per-segment budget is an even split of (duration_min - total_dwell_min).
      - No corridor filter per segment (uses plain reachable radius from segment start).
        The back-off picks cells along the actual ORS-optimized path; off-route picks
        get dropped on over-budget. Corridor filter per-segment can be added later if
        rural multi-stop plans drift too far off-path.
      - One final ORS /directions call on the full waypoint chain gives accurate
        total distance + geometry. Per-segment durations are summed as a fallback.
    """
    home = Waypoint(request.home_lat, request.home_lon, label="Home")

    segments: list[tuple[Waypoint, Waypoint]] = []
    cursor = home
    for i, stop in enumerate(request.stops):
        stop_wp = Waypoint(stop.lat, stop.lon, label=stop.label or f"Stop {i + 1}")
        segments.append((cursor, stop_wp))
        cursor = stop_wp
    if request.mode == "loop":
        segments.append((cursor, home))

    total_dwell = request.total_dwell_min()
    avail_min = max(request.duration_min - total_dwell, len(segments))
    per_seg_min = max(avail_min // len(segments), 5)

    all_chosen: list[CellScore] = []
    all_waypoints: list[Waypoint] = [home]
    all_drops: list[str] = []
    total_duration_s = 0.0
    total_distance_m = 0.0
    auto_painted_total = 0

    async with OrsClient() as ors:
        for i, (seg_start, seg_end) in enumerate(segments):
            sub_req = PlanRequest(
                home_lat=seg_start.lat,
                home_lon=seg_start.lon,
                duration_min=per_seg_min,
                mode="oneway",
                stops=[Stop(lat=seg_end.lat, lon=seg_end.lon, label=seg_end.label)],
            )
            seg_cands = _candidate_cells(sub_req)
            if not seg_cands:
                auto_painted_total += _paint_grid_for_request(sub_req)
                seg_cands = _candidate_cells(sub_req)

            seg_chosen: list[CellScore] = []
            seg_leg: RouteLeg
            if seg_cands:
                est_initial = max(
                    MIN_WAYPOINTS, min(MAX_OPTIMIZATION_JOBS, per_seg_min // 8)
                )
                seg_chosen_init = seg_cands[:est_initial]
                try:
                    seg_leg, seg_chosen = await _solve_with_backoff(
                        ors, seg_start, seg_end, seg_chosen_init, sub_req, all_drops
                    )
                except PlannerError as exc:
                    logger.info(
                        "Segment %d (%s -> %s): no plan fits, using direct: %s",
                        i + 1,
                        seg_start.label,
                        seg_end.label,
                        exc,
                    )
                    seg_leg = await ors.directions(
                        [seg_start, seg_end], with_geometry=False
                    )
                    seg_chosen = []
            else:
                seg_leg = await ors.directions([seg_start, seg_end], with_geometry=False)

            all_chosen.extend(seg_chosen)
            all_waypoints.extend(
                Waypoint(c.center_lat, c.center_lon, label=f"Cell {c.cell_id}")
                for c in seg_chosen
            )
            all_waypoints.append(seg_end)
            total_duration_s += seg_leg.duration_s
            total_distance_m += seg_leg.distance_m

        total_duration_s += total_dwell * 60

        geometry = None
        if attach_geometry and len(all_waypoints) >= 2:
            try:
                full_leg = await ors.directions(all_waypoints, with_geometry=True)
                geometry = full_leg.geometry
                total_duration_s = full_leg.duration_s + total_dwell * 60
                total_distance_m = full_leg.distance_m
            except OrsError as exc:
                logger.warning(
                    "Full-chain directions failed; using per-segment sums: %s", exc
                )

    aggregate_leg = RouteLeg(
        distance_m=total_distance_m,
        duration_s=total_duration_s,
        geometry=geometry,
        waypoint_order=list(range(len(all_chosen))),
        raw={"multistop_segments": len(segments)},
    )

    estimated_new_aps = sum(c.estimated_aps for c in all_chosen if c.ownership != "me")
    synthetic = bool(all_chosen) and all(not c.probed for c in all_chosen)

    plan_id = _persist_plan(request, all_waypoints, aggregate_leg, estimated_new_aps)

    return _attach_departure_time(
        PlanResult(
            request=request,
            chosen_cells=all_chosen,
            ordered_waypoints=all_waypoints,
            leg=aggregate_leg,
            geometry=geometry,
            estimated_new_aps=estimated_new_aps,
            planned_route_id=plan_id,
            drops_for_slack=all_drops,
            synthetic_density=synthetic,
            auto_painted_cells=auto_painted_total,
        )
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
    stops_payload = (
        json.dumps(
            [
                {
                    "lat": s.lat,
                    "lon": s.lon,
                    "label": s.label,
                    "dwell_min": s.dwell_min,
                    "overnight_after": s.overnight_after,
                }
                for s in request.stops
            ]
        )
        if request.stops
        else None
    )
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO planned_routes (
                created_at, home_lat, home_lon, duration_min, mode,
                destination_lat, destination_lon, waypoints_json,
                estimated_new_aps, estimated_drive_min, stops_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).replace(tzinfo=None).isoformat(),
                request.home_lat,
                request.home_lon,
                request.duration_min,
                request.mode,
                request.destination_lat,
                request.destination_lon,
                payload,
                estimated_new_aps,
                leg.duration_min,
                stops_payload,
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
