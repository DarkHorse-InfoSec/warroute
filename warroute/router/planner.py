"""Greedy-pick + ORS optimization planner.

Algorithm (PLAN.md §3.3, adapted for ORS):
  1. Compute reachable radius from time budget (loop -> half the time can be outbound).
  2. Rank all cells in radius by score (scorer.rank_cells).
  3. Greedy-pick top K = MAX_OPTIMIZATION_JOBS (~25) cells.
  4. Hand to ORS /optimization with vehicle start=home, end=home (loop) or end=destination.
  5. If returned duration > budget * (1 + slack), drop lowest-scoring and retry.
  6. Return: ordered waypoints, geometry (via subsequent /directions call), persisted plan id.

Phase 6b.3: per-stop arrival deadlines (Stop.arrive_by). After the initial solve,
walk the per-segment schedule, derive the binding departure from all constraints
(per-stop arrive_by + request-level arrive_by, whichever is tighter), and re-solve
prefix segments without cells if the binding departure is in the past. The conflict
policy is "drop cells, keep deadlines" - see DECISIONS.md 2026-05-14 (evening).
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
from warroute.config import get_settings
from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.db import transaction
from warroute.router.enrich import enrich_wigle_density, wdgowars_ownership_map
from warroute.router.scorer import CellScore, rank_cells

logger = logging.getLogger(__name__)

# Tuning constants. Tunable later via config if rural-VT defaults turn out wrong.
DEFAULT_AVG_SPEED_KMH = 40.0
DURATION_SLACK = 0.10  # accept routes up to 10% over the requested budget
# Smallest number of AP-scanning cells a plan will map. 1 (not 2) so a tight
# budget maps the FEWEST scanned signals it can afford rather than failing; if
# even one cell won't fit, the caller falls back to the direct/fastest route.
MIN_WAYPOINTS = 1
EARTH_KM_PER_DEG_LAT = 111.32
# Hard cap on how many cells we'll auto-paint per plan when the DB is empty.
# Sized to comfortably cover a ~120-min loop in rural terrain (~840 cells) but
# refuse to silently insert 50k+ rows for a runaway 8-hour request - those
# should run `coverage refresh` deliberately instead.
MAX_AUTO_PAINT_CELLS = 2000
# Phase 6b: small safety buffer added to the computed departure time so the user
# isn't expected to leave the door at the literal second the route says.
DEPARTURE_BUFFER_MIN = 2
# Phase 6b.3: minimum lead-time between "now" and the computed departure for a plan
# to be considered feasible. Below this, the planner drops cells from prefix segments
# (or raises if even direct driving can't make the deadline).
MIN_DEPARTURE_LEAD_MIN = 5


def _now() -> datetime:
    """Wall-clock indirection for testability. Returns naive local datetime.

    Tests that exercise deadline-feasibility logic should monkeypatch this so
    the result doesn't depend on when the test happens to run.
    """
    return datetime.now()


def parse_arrive_hhmm(spec: str | None, now: datetime | None = None) -> datetime | None:
    """Parse a 24h HH:MM or HHMM string into a datetime today (or tomorrow if past).

    Accepts "1600", "16:00", "0830", " 0830 ". Returns None on anything malformed.
    Used by the web form (HTML <input type="time"> yields "HH:MM") and the CLI
    --stop suffix (which uses ":HHMM" inside the colon-separated payload).
    """
    if spec is None:
        return None
    s = spec.replace(":", "").strip()
    if len(s) != 4 or not s.isdigit():
        return None
    h, m = int(s[:2]), int(s[2:])
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    base = now if now is not None else _now()
    candidate = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= base:
        candidate = candidate + timedelta(days=1)
    return candidate


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

    `arrive_by` is Phase 6b.3: a hard time deadline for arrival AT this stop
    (dwell happens after arrival, so dwell at this stop is not included in the
    constraint). When set on any stop, the planner derives the latest possible
    departure as min(arrive_by - cumulative_minutes_to_stop) across all such
    constrained stops, and the conflict policy is auto-drop-cells-keep-deadlines.
    """

    lat: float
    lon: float
    label: str | None = None
    dwell_min: int = 0
    overnight_after: bool = False
    arrive_by: datetime | None = None


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
    # Per-user ORS key (DECISIONS.md 2026-05-14 very late). When None, the
    # planner constructs OrsClient with no override -> falls back to settings.
    ors_api_key: str | None = None
    # Per-user WiGLE + WDGoWars keys for LIVE cell enrichment at plan time
    # (DECISIONS.md 2026-07-05 enrich). When present, the planner queries WiGLE for
    # real AP density (cached in the shared cells table) and WDGoWars gang
    # territories for per-cell ownership. When None, it uses whatever is already in
    # the DB (or a geometric spread). Never blocks the plan on their failure.
    wigle_name: str | None = None
    wigle_token: str | None = None
    wdgowars_token: str | None = None

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

    def has_per_stop_deadline(self) -> bool:
        """True if any stop has an arrive_by constraint (Phase 6b.3)."""
        return any(s.arrive_by is not None for s in self.stops)

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

        Phase 6c.2: highway-class segments (direct_min > 30) get a 2x multiplier.
        On an interstate you cross several cells just driving the speed limit, so a
        cell 5 km off-road is fine; the in-town formula was too tight for those.
        """
        base = self.detour_budget_min() * self.avg_speed_kmh / 60.0 / 8.0
        if self.direct_min is not None and self.direct_min > 30:
            return base * 2.0
        return base

    def end_waypoint(self) -> Waypoint:
        if self.mode == "loop":
            return Waypoint(self.home_lat, self.home_lon, label="Home")
        if not self.stops:
            raise PlannerError("oneway mode requires at least one stop")
        last = self.stops[-1]
        return Waypoint(last.lat, last.lon, label=last.label or "Destination")


@dataclass(frozen=True)
class DaySegment:
    """Phase 6c: a single day of a roadtrip plan.

    Inclusive `start_idx`/`end_idx` reference positions in PlanResult.ordered_waypoints,
    so a 3-day roadtrip with 8 waypoints might produce days = [(0,2), (2,5), (5,7)]
    (overlapping ends are intentional: each day starts where the previous ended).

    `day_number` is 1-indexed (matches user-facing labeling: "Day 1", "Day 2").
    """

    day_number: int
    start_idx: int
    end_idx: int
    drive_min: float
    dwell_min: int


@dataclass(frozen=True)
class _BindingConstraint:
    """The deadline that determines departure time, plus where it came from.

    stop_index is the 0-based position in request.stops when a per-stop arrive_by
    binds; None means the request-level arrive_by (trip-end deadline) is binding.
    """

    departure: datetime
    arrive_by: datetime
    stop_index: int | None


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
    days: list[DaySegment] = field(default_factory=list)  # Phase 6c: roadtrip day splits
    # Phase 6b.3: per-stop arrival ETAs (parallel to request.stops). Populated when
    # a departure is derived; empty list otherwise.
    stop_arrivals: list[datetime] = field(default_factory=list)
    # Phase 6b.3: index of the user stop whose arrive_by bound departure. None when
    # no per-stop constraint binds (request-level deadline or no deadline at all).
    binding_stop_index: int | None = None
    # Phase 6b.3: number of cells dropped specifically to meet a deadline (separate
    # from drops_for_slack, which counts budget-driven drops).
    deadline_drops: int = 0

    @property
    def estimated_drive_min(self) -> float:
        return self.leg.duration_min

    @property
    def total_trip_min(self) -> float:
        """Drive time + total dwell at user-specified stops."""
        return self.estimated_drive_min + self.request.total_dwell_min()

    @property
    def is_roadtrip(self) -> bool:
        """True if any user stop is marked overnight_after (route spans days)."""
        return any(s.overnight_after for s in self.request.stops)


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

    # Live per-user enrichment (DECISIONS.md 2026-07-05): WiGLE density (persisted
    # to the shared cells table) + WDGoWars gang-territory ownership (per-request).
    # Then re-select candidates so the plan ranks by real data. No-op without keys.
    ownership = await _enrich_area(request)
    if request.wigle_token or request.wdgowars_token:
        candidates = _candidate_cells(request, ownership)

    home = Waypoint(request.home_lat, request.home_lon, label="Home")
    end = request.end_waypoint()

    # Right-size the initial cell count to the budget. A 20-min plan can't fit
    # 25 cells; starting with 25 just burns ORS calls on doomed iterations.
    # Heuristic: ~1 cell per 8 minutes of budget (with rural back-and-forth).
    est_initial = max(MIN_WAYPOINTS, min(MAX_OPTIMIZATION_JOBS, request.duration_min // 8))
    chosen = candidates[:est_initial]
    drops: list[str] = []
    deadline_drops_count = 0

    async with OrsClient(api_key=request.ors_api_key) as ors:
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

        # Phase 6b.3: deadline feasibility check + cell-drop retry.
        if request.has_per_stop_deadline() or request.arrive_by is not None:
            seg_legs_min = [leg.duration_s / 60.0]
            binding = _derive_binding(request, seg_legs_min)
            if binding is not None and binding.departure < _now() + timedelta(
                minutes=MIN_DEPARTURE_LEAD_MIN
            ):
                # Strip cells, fall back to a direct home->end leg.
                if chosen:
                    direct = await ors.directions([home, end], with_geometry=attach_geometry)
                    deadline_drops_count = len(chosen)
                    drops.extend(c.cell_id for c in chosen)
                    chosen = []
                    leg = direct
                    geometry = direct.geometry
                # Re-check feasibility with the direct leg.
                seg_legs_min = [leg.duration_s / 60.0]
                binding = _derive_binding(request, seg_legs_min)
                if binding is not None and binding.departure < _now() + timedelta(
                    minutes=MIN_DEPARTURE_LEAD_MIN
                ):
                    _raise_infeasible(request, binding)

    ordered_waypoints = (
        [home]
        + [Waypoint(c.center_lat, c.center_lon, label=f"Cell {c.cell_id}") for c in chosen]
        + [end]
    )

    estimated_new_aps = sum(c.estimated_aps for c in chosen if c.ownership != "me")
    synthetic = bool(chosen) and all(not c.probed for c in chosen)

    plan_id = _persist_plan(request, ordered_waypoints, leg, estimated_new_aps)

    return _finalize_schedule(
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
            deadline_drops=deadline_drops_count,
        ),
        seg_legs_min=[leg.duration_s / 60.0],
    )


def _compute_arrival_offsets_min(request: PlanRequest, seg_legs_min: list[float]) -> list[float]:
    """For each user stop, minutes from departure to ARRIVAL at that stop.

    Segment i (0-indexed) goes from prior cursor to user stop[i]; arrival at
    stop[i] happens after segment i's drive plus the dwell consumed at all prior
    stops. Dwell AT stop[i] itself is post-arrival and does NOT count toward
    the arrival offset.

    For loop mode, there's a final segment back to home that's not represented
    in this list (it has no user stop). Callers needing trip-end offset should
    use `_trip_end_offset_min`.
    """
    offsets: list[float] = []
    cum_min = 0.0
    for i in range(len(request.stops)):
        if i < len(seg_legs_min):
            cum_min += seg_legs_min[i]
        offsets.append(cum_min)
        cum_min += request.stops[i].dwell_min
    return offsets


def _trip_end_offset_min(request: PlanRequest, seg_legs_min: list[float]) -> float:
    """Minutes from departure to TRIP END (where the request-level arrive_by applies).

    Oneway: trip ends at last stop's arrival. Last stop's dwell is post-arrival.
    Loop: trip ends back at home after all segments + all stop dwells.
    """
    total_drive = sum(seg_legs_min)
    if request.mode == "oneway":
        # Sum dwells of all stops EXCEPT the last (its dwell is post-arrival).
        dwell_before_end = sum(s.dwell_min for s in request.stops[:-1])
    else:
        # Loop: all user-stop dwells happen before the return to home.
        dwell_before_end = sum(s.dwell_min for s in request.stops)
    return total_drive + dwell_before_end


def _derive_binding(request: PlanRequest, seg_legs_min: list[float]) -> _BindingConstraint | None:
    """Compute the binding deadline across per-stop and request-level constraints.

    Returns the (departure, deadline, source) tuple with the EARLIEST required
    departure - that's the constraint the planner must satisfy. Returns None when
    no deadlines exist.
    """
    arrival_offsets = _compute_arrival_offsets_min(request, seg_legs_min)
    candidates: list[_BindingConstraint] = []
    for i, s in enumerate(request.stops):
        if s.arrive_by is None:
            continue
        offset = arrival_offsets[i] if i < len(arrival_offsets) else 0.0
        dep = s.arrive_by - timedelta(minutes=offset + DEPARTURE_BUFFER_MIN)
        candidates.append(_BindingConstraint(departure=dep, arrive_by=s.arrive_by, stop_index=i))
    if request.arrive_by is not None:
        offset = _trip_end_offset_min(request, seg_legs_min)
        dep = request.arrive_by - timedelta(minutes=offset + DEPARTURE_BUFFER_MIN)
        candidates.append(
            _BindingConstraint(departure=dep, arrive_by=request.arrive_by, stop_index=None)
        )
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.departure)


def _binding_label(request: PlanRequest, stop_index: int | None) -> str:
    """Human-readable label for the binding stop (for error messages)."""
    if stop_index is None:
        return "trip-end deadline"
    if 0 <= stop_index < len(request.stops):
        stop = request.stops[stop_index]
        return stop.label or f"stop {stop_index + 1}"
    return f"stop {stop_index + 1}"


def _raise_infeasible(request: PlanRequest, binding: _BindingConstraint) -> None:
    """Raise PlannerError naming the deadline that can't be met even direct."""
    shortfall_min = (
        (_now() + timedelta(minutes=MIN_DEPARTURE_LEAD_MIN)) - binding.departure
    ).total_seconds() / 60.0
    label = _binding_label(request, binding.stop_index)
    raise PlannerError(
        f"Cannot meet {label} deadline of {binding.arrive_by.strftime('%H:%M')}: "
        f"need to leave {shortfall_min:.0f} min ago even with no AP detours."
    )


def _finalize_schedule(result: PlanResult, seg_legs_min: list[float]) -> PlanResult:
    """Phase 6b/6b.3: derive departure + per-stop arrivals from deadline constraints.

    Walks the schedule (cumulative drive + dwell) from a candidate departure to
    produce per-stop ETAs. If no deadlines are set, returns the result unchanged.
    Persists a row to scheduled_departures so a future ntfy job can fire the
    departure alarm.
    """
    request = result.request
    if (
        not request.has_per_stop_deadline() and request.arrive_by is None
    ) or result.planned_route_id is None:
        return result

    binding = _derive_binding(request, seg_legs_min)
    if binding is None:
        return result

    arrival_offsets = _compute_arrival_offsets_min(request, seg_legs_min)
    departure = binding.departure
    stop_arrivals = [departure + timedelta(minutes=offset) for offset in arrival_offsets]

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
                binding.arrive_by.replace(tzinfo=None).isoformat()
                if binding.arrive_by.tzinfo
                else binding.arrive_by.isoformat(),
            ),
        )
    return replace(
        result,
        departure_at=departure,
        stop_arrivals=stop_arrivals,
        binding_stop_index=binding.stop_index,
    )


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


def _in_range_rows(request: PlanRequest) -> list[cells_dal.CellRow]:
    """Candidate CellRows within reachable range (radius or oneway corridor), by
    cell center distance. Used to TARGET live enrichment (WiGLE + WDGoWars) at the
    cells a plan could actually route through. No scoring/ownership filter here."""
    radius_km = request.reachable_radius_km()
    with transaction() as conn:
        all_rows = cells_dal.all_cells(conn)
    use_corridor = (
        request.mode == "oneway"
        and request.destination_lat is not None
        and request.destination_lon is not None
        and request.direct_min is not None
    )
    if use_corridor:
        assert request.destination_lat is not None
        assert request.destination_lon is not None
        corridor_km = max(request.corridor_half_width_km(), 2.0)
        return [
            r
            for r in all_rows
            if _point_to_segment_km(
                r.center_lat,
                r.center_lon,
                request.home_lat,
                request.home_lon,
                request.destination_lat,
                request.destination_lon,
            )
            <= corridor_km
        ]
    return [
        r
        for r in all_rows
        if _km_between(request.home_lat, request.home_lon, r.center_lat, r.center_lon) <= radius_km
    ]


async def _enrich_area(request: PlanRequest) -> dict[str, str]:
    """Live-enrich the candidate area with the requester's own keys, so plans rank
    by real data without anyone pre-running `coverage refresh`. WiGLE density is
    persisted to the shared cells table (user-independent); WDGoWars ownership is
    returned as a per-request map (user-specific). Best-effort. See
    warroute/router/enrich.py and DECISIONS.md 2026-07-05."""
    rows = _in_range_rows(request)
    if not rows:
        return {}
    settings = get_settings()
    if request.wigle_name and request.wigle_token:
        try:
            n = await enrich_wigle_density(
                rows,
                name=request.wigle_name,
                token=request.wigle_token,
                home_lat=request.home_lat,
                home_lon=request.home_lon,
                cap=settings.live_density_cell_cap,
                budget_s=settings.live_density_budget_s,
            )
            if n:
                logger.info("Live WiGLE density enriched %d cells", n)
        except Exception as exc:
            logger.warning("WiGLE density enrichment failed (ignored): %s", exc)
    if request.wdgowars_token:
        try:
            return await wdgowars_ownership_map(rows, request.wdgowars_token)
        except Exception as exc:
            logger.warning("WDGoWars ownership enrichment failed (ignored): %s", exc)
    return {}


def _candidate_cells(
    request: PlanRequest, ownership_map: dict[str, str] | None = None
) -> list[CellScore]:
    """Cells that are viable detour candidates given the request.

    Loop mode: cells within reachable_radius of home (the symmetric old behavior).

    Oneway mode with `direct_min` set: cells within `corridor_half_width_km` of the
    home->destination line segment. This keeps detours along the path instead of
    radiating around home (which produced absurd 80-min routes for 15-min destinations).

    `ownership_map` (from live WDGoWars enrichment) overrides each cell's ownership
    in-memory before scoring, so uncaptured cells outrank owned ones.
    """
    radius_km = request.reachable_radius_km()
    if radius_km <= 0:
        raise PlannerError(f"duration_min={request.duration_min} yields zero reachable radius")

    with transaction() as conn:
        all_rows = cells_dal.all_cells(conn)
    if ownership_map:
        for r in all_rows:
            if r.id in ownership_map:
                r.wdgowars_owner = ownership_map[r.id]
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
        # - exit loop so we don't re-call with the same cells (the previous iteration's leg
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


async def _solve_segment(
    ors: OrsClient,
    seg_start: Waypoint,
    seg_end: Waypoint,
    request: PlanRequest,
    per_seg_min: int,
    all_drops: list[str],
    ownership: dict[str, str] | None = None,
) -> tuple[list[CellScore], list[Waypoint], RouteLeg, int]:
    """Solve a single segment: pick cells, run optimize, fall back to direct on failure.

    Returns (chosen_cells, cell_waypoints, leg, auto_painted_count).
    Mutates `all_drops` with any cells dropped during back-off.
    """
    seg_km = _km_between(seg_start.lat, seg_start.lon, seg_end.lat, seg_end.lon)
    seg_direct_min = (seg_km / DEFAULT_AVG_SPEED_KMH) * 60.0
    sub_req = PlanRequest(
        home_lat=seg_start.lat,
        home_lon=seg_start.lon,
        duration_min=per_seg_min,
        mode="oneway",
        stops=[Stop(lat=seg_end.lat, lon=seg_end.lon, label=seg_end.label)],
        direct_min=seg_direct_min,
    )
    seg_cands = _candidate_cells(sub_req, ownership)
    auto_painted = 0
    if not seg_cands:
        auto_painted = _paint_grid_for_request(sub_req)
        seg_cands = _candidate_cells(sub_req, ownership)

    if seg_cands:
        est_initial = max(MIN_WAYPOINTS, min(MAX_OPTIMIZATION_JOBS, per_seg_min // 8))
        seg_chosen_init = seg_cands[:est_initial]
        try:
            seg_leg, seg_chosen = await _solve_with_backoff(
                ors, seg_start, seg_end, seg_chosen_init, sub_req, all_drops
            )
        except PlannerError as exc:
            logger.info(
                "Segment %s -> %s: no plan fits, using direct: %s",
                seg_start.label,
                seg_end.label,
                exc,
            )
            seg_leg = await ors.directions([seg_start, seg_end], with_geometry=False)
            seg_chosen = []
    else:
        seg_leg = await ors.directions([seg_start, seg_end], with_geometry=False)
        seg_chosen = []

    cell_waypoints = [
        Waypoint(c.center_lat, c.center_lon, label=f"Cell {c.cell_id}") for c in seg_chosen
    ]
    return seg_chosen, cell_waypoints, seg_leg, auto_painted


async def _plan_multistop(request: PlanRequest, attach_geometry: bool = True) -> PlanResult:
    """Multi-leg plan: route each consecutive (start, stop) pair as its own segment.

    Each segment runs its own candidate selection + ORS optimization, with back-off
    if the segment overshoots its share of the time budget. After all segments solve,
    Phase 6b.3 checks deadline feasibility: if any per-stop arrive_by requires
    leaving in the past, prefix segments leading to the binding stop are re-solved
    without cells (direct legs). Raises if even direct driving can't make the
    deadline.

    v1 simplifications:
      - Per-segment budget is an even split of (duration_min - total_dwell_min).
      - No corridor filter per segment (uses plain reachable radius from segment start).
        The back-off picks cells along the actual ORS-optimized path; off-route picks
        get dropped on over-budget.
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

    # Per-segment state (Phase 6b.3 refactor): captured during the initial solve so
    # the deadline retry can rewrite prefix segments without redoing the whole loop.
    seg_chosen: list[list[CellScore]] = []
    seg_cell_waypoints: list[list[Waypoint]] = []
    seg_legs: list[RouteLeg] = []
    seg_drops: list[list[str]] = []
    auto_painted_total = 0
    deadline_drops_count = 0

    # Live per-user enrichment once for the whole area (DECISIONS.md 2026-07-05):
    # WiGLE density persists to the shared cells table so each segment reads it;
    # WDGoWars ownership is a per-request map passed to every segment. No-op without keys.
    ownership = await _enrich_area(request)

    async with OrsClient(api_key=request.ors_api_key) as ors:
        # Phase A: solve each segment independently.
        for seg_start, seg_end in segments:
            local_drops: list[str] = []
            chosen, cell_wps, leg, painted = await _solve_segment(
                ors, seg_start, seg_end, request, per_seg_min, local_drops, ownership
            )
            seg_chosen.append(chosen)
            seg_cell_waypoints.append(cell_wps)
            seg_legs.append(leg)
            seg_drops.append(local_drops)
            auto_painted_total += painted

        # Phase B: Phase 6b.3 deadline feasibility check + cell-drop retry.
        if request.has_per_stop_deadline() or request.arrive_by is not None:
            seg_legs_min = [L.duration_s / 60.0 for L in seg_legs]
            binding = _derive_binding(request, seg_legs_min)
            if binding is not None and binding.departure < _now() + timedelta(
                minutes=MIN_DEPARTURE_LEAD_MIN
            ):
                # Strip cells from prefix segments. For per-stop binding, prefix is
                # [0..binding.stop_index]. For trip-end binding (request.arrive_by),
                # strip all segments.
                if binding.stop_index is not None:
                    prefix_end = binding.stop_index
                else:
                    prefix_end = len(segments) - 1
                for j in range(prefix_end + 1):
                    if not seg_chosen[j]:
                        continue
                    direct = await ors.directions(
                        [segments[j][0], segments[j][1]], with_geometry=False
                    )
                    deadline_drops_count += len(seg_chosen[j])
                    seg_drops[j].extend(c.cell_id for c in seg_chosen[j])
                    seg_chosen[j] = []
                    seg_cell_waypoints[j] = []
                    seg_legs[j] = direct
                # Re-check with stripped prefix.
                seg_legs_min = [L.duration_s / 60.0 for L in seg_legs]
                binding = _derive_binding(request, seg_legs_min)
                if binding is not None and binding.departure < _now() + timedelta(
                    minutes=MIN_DEPARTURE_LEAD_MIN
                ):
                    _raise_infeasible(request, binding)

        # Phase C: aggregate flat lists + roadtrip day segments.
        all_chosen: list[CellScore] = []
        all_waypoints: list[Waypoint] = [home]
        all_drops: list[str] = []
        days: list[DaySegment] = []
        has_overnights = any(s.overnight_after for s in request.stops)
        day_n = 1
        day_start_idx = 0
        day_drive_s = 0.0
        day_dwell_min = 0
        for j, (_seg_start, seg_end) in enumerate(segments):
            all_chosen.extend(seg_chosen[j])
            all_waypoints.extend(seg_cell_waypoints[j])
            all_waypoints.append(seg_end)
            all_drops.extend(seg_drops[j])
            if has_overnights:
                day_drive_s += seg_legs[j].duration_s
                if j < len(request.stops):
                    day_dwell_min += request.stops[j].dwell_min
                if j < len(request.stops) and request.stops[j].overnight_after:
                    end_idx = len(all_waypoints) - 1
                    days.append(
                        DaySegment(
                            day_number=day_n,
                            start_idx=day_start_idx,
                            end_idx=end_idx,
                            drive_min=day_drive_s / 60.0,
                            dwell_min=day_dwell_min,
                        )
                    )
                    day_n += 1
                    day_start_idx = end_idx
                    day_drive_s = 0.0
                    day_dwell_min = 0
        if has_overnights:
            days.append(
                DaySegment(
                    day_number=day_n,
                    start_idx=day_start_idx,
                    end_idx=len(all_waypoints) - 1,
                    drive_min=day_drive_s / 60.0,
                    dwell_min=day_dwell_min,
                )
            )

        # Phase D: totals + optional full-chain /directions.
        total_duration_s = sum(L.duration_s for L in seg_legs) + total_dwell * 60
        total_distance_m = sum(L.distance_m for L in seg_legs)
        geometry = None
        if attach_geometry and len(all_waypoints) >= 2:
            try:
                full_leg = await ors.directions(all_waypoints, with_geometry=True)
                geometry = full_leg.geometry
                total_duration_s = full_leg.duration_s + total_dwell * 60
                total_distance_m = full_leg.distance_m
            except OrsError as exc:
                logger.warning("Full-chain directions failed; using per-segment sums: %s", exc)

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

    final_seg_legs_min = [L.duration_s / 60.0 for L in seg_legs]
    return _finalize_schedule(
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
            days=days,
            deadline_drops=deadline_drops_count,
        ),
        seg_legs_min=final_seg_legs_min,
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
                    "arrive_by": s.arrive_by.isoformat() if s.arrive_by else None,
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
