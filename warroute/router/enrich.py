"""Live per-user cell enrichment for the planner (DECISIONS.md 2026-07-05 enrich).

At plan time, using the REQUESTER's own keys, so plans are density/ownership ranked
without anyone pre-running `coverage refresh`:

  - WiGLE density: query the AP count for the nearest unprobed candidate cells and
    cache it in the shared `cells` table (density is user-independent, so the shared
    cache is correct and benefits everyone). Bounded by a cell cap + a wall-clock
    budget because WiGLE is throttled to ~1 req/sec.
  - WDGoWars ownership: fetch the gang-territory hulls + the requester's gang, then
    tag each candidate cell me/rival/uncaptured by point-in-polygon. This is
    user-specific (me vs rival depends on your gang), so it is returned as an
    in-memory map and NEVER persisted to the shared table.

Best-effort throughout: any error logs and the plan proceeds with whatever data it
has (falls back to the geometric spread).
"""

from __future__ import annotations

import json
import logging
import time

from warroute.clients.ors import haversine_km
from warroute.clients.wdgowars import WdgowarsClient, WdgowarsError
from warroute.clients.wigle import BBox, WigleClient, WigleError
from warroute.coverage.cells import OWNER_ME, CellRow, update_density
from warroute.db import transaction

logger = logging.getLogger(__name__)

# Gang-territory hull coordinate order. Verified against prod 2026-07-04
# (WDGOWARS_HULL_IS_LATLON in the coverage route): hull points are [lat, lon].
_HULL_IS_LATLON = True


def _bbox_from_geojson(geojson_str: str) -> BBox | None:
    """Parse a cell's stored bbox GeoJSON polygon into a WiGLE BBox."""
    try:
        geom = json.loads(geojson_str)
        ring = geom["coordinates"][0]  # [[lon, lat], ...]
        lons = [float(p[0]) for p in ring]
        lats = [float(p[1]) for p in ring]
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if not lons or not lats:
        return None
    return BBox(south=min(lats), north=max(lats), west=min(lons), east=max(lons))


def point_in_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon. `ring` points are [lat, lon] when
    _HULL_IS_LATLON, else [lon, lat]. Non-closed rings are fine."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = (ring[i][0], ring[i][1]) if _HULL_IS_LATLON else (ring[i][1], ring[i][0])
        lat_j, lon_j = (ring[j][0], ring[j][1]) if _HULL_IS_LATLON else (ring[j][1], ring[j][0])
        if ((lat_i > lat) != (lat_j > lat)) and (
            lon < (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i
        ):
            inside = not inside
        j = i
    return inside


async def enrich_wigle_density(
    rows: list[CellRow],
    *,
    name: str,
    token: str,
    home_lat: float,
    home_lon: float,
    cap: int,
    budget_s: float,
) -> int:
    """Query WiGLE for the nearest `cap` UNPROBED rows, persist to the shared cells
    table, and set on the in-memory rows. Bounded by cap + wall-clock budget.
    Returns the number of cells enriched. Best-effort (never raises)."""
    if cap <= 0:
        return 0
    unprobed = [r for r in rows if r.estimated_total_aps is None]
    unprobed.sort(key=lambda r: haversine_km(home_lat, home_lon, r.center_lat, r.center_lon))
    targets = unprobed[:cap]
    if not targets:
        return 0
    results: dict[str, int] = {}
    started = time.monotonic()
    try:
        async with WigleClient(name=name, token=token) as wigle:
            for row in targets:
                if time.monotonic() - started > budget_s:
                    logger.info(
                        "WiGLE enrich hit %.0fs budget; enriched %d", budget_s, len(results)
                    )
                    break
                bbox = _bbox_from_geojson(row.bbox_geojson)
                if bbox is None:
                    continue
                try:
                    res = await wigle.search_bbox(bbox, result_per_page=1)
                except WigleError as exc:
                    logger.info("WiGLE enrich skip %s: %s", row.id, exc)
                    continue
                results[row.id] = res.total_results
    except WigleError as exc:
        logger.warning("WiGLE enrich unavailable: %s", exc)
        return 0
    if results:
        with transaction() as conn:
            for cid, count in results.items():
                update_density(conn, cid, count)
        by_id = {r.id: r for r in rows}
        for cid, count in results.items():
            by_id[cid].estimated_total_aps = count
    return len(results)


async def wdgowars_ownership_map(rows: list[CellRow], token: str) -> dict[str, str]:
    """Return {cell_id: owner} for cells inside a gang territory, from the
    requester's gang perspective: OWNER_ME for the user's gang, else the rival
    gang's name. Uncaptured cells are omitted. Best-effort (never raises)."""
    try:
        async with WdgowarsClient(token=token) as wdg:
            me = await wdg.me()
            gangs = await wdg.gang_territories()
    except WdgowarsError as exc:
        logger.warning("WDGoWars enrich unavailable: %s", exc)
        return {}
    my_gang_id = me.gang_id
    my_hulls = [
        g.hull
        for g in gangs
        if g.gang_id is not None and g.gang_id == my_gang_id and len(g.hull) >= 3
    ]
    rival_hulls = [
        (g.name, g.hull)
        for g in gangs
        if (my_gang_id is None or g.gang_id != my_gang_id) and len(g.hull) >= 3
    ]
    owned: dict[str, str] = {}
    for row in rows:
        lat, lon = row.center_lat, row.center_lon
        if any(point_in_ring(lat, lon, h) for h in my_hulls):
            owned[row.id] = OWNER_ME
        else:
            hit = next((nm for nm, h in rival_hulls if point_in_ring(lat, lon, h)), None)
            if hit is not None:
                owned[row.id] = hit
    return owned
