"""Refresh orchestration: paint the grid, pull WDGoWars ownership, pull WiGLE density."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from warroute.clients.wdgowars import WdgowarsClient, WdgowarsError
from warroute.clients.wigle import WigleClient, WigleError
from warroute.config import get_settings
from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import Cell, cells_in_radius
from warroute.db import transaction

logger = logging.getLogger(__name__)


@dataclass
class RefreshSummary:
    cells_total: int
    cells_inserted: int
    cells_density_refreshed: int
    cells_density_failed: int
    cells_owned_by_me: int
    wdgowars_synced: bool
    wdgowars_error: str | None = None


async def refresh(
    home_lat: float | None = None,
    home_lon: float | None = None,
    radius_km: float | None = None,
    density_max_age: timedelta = timedelta(hours=24),
) -> RefreshSummary:
    """Materialize the grid for the home radius, then refresh ownership + density."""
    settings = get_settings()
    home_lat = home_lat if home_lat is not None else settings.home_lat
    home_lon = home_lon if home_lon is not None else settings.home_lon
    radius_km = radius_km if radius_km is not None else settings.home_radius_km

    grid = cells_in_radius(home_lat, home_lon, radius_km)
    logger.info(
        "Painting grid: home=%.4f,%.4f radius=%.0f km -> %d cells",
        home_lat,
        home_lon,
        radius_km,
        len(grid),
    )

    with transaction() as conn:
        inserted = cells_dal.upsert_grid(conn, grid)
        stale_ids = cells_dal.stale_density_cells(conn, older_than=density_max_age)

    wdgowars_synced = False
    wdgowars_error: str | None = None
    owned_count = 0
    try:
        async with WdgowarsClient() as wdg:
            player = await wdg.me()
        if player.owned_cell_ids:
            with transaction() as conn:
                owned_count = cells_dal.mark_owned_by_me(conn, player.owned_cell_ids)
        wdgowars_synced = True
        logger.info(
            "WDGoWars: player=%s points=%s owned_cells=%d",
            player.username or "?",
            player.points,
            len(player.owned_cell_ids),
        )
    except WdgowarsError as exc:
        wdgowars_error = str(exc)
        logger.warning("WDGoWars sync skipped: %s", exc)

    refreshed = 0
    failed = 0
    if stale_ids:
        async with WigleClient() as wigle:
            for cell_id in stale_ids:
                cell = _cell_lookup(grid, cell_id)
                if cell is None:
                    continue
                try:
                    result = await wigle.search_bbox(cell.bbox(), result_per_page=1)
                except WigleError as exc:
                    failed += 1
                    logger.warning("WiGLE density failed for %s: %s", cell_id, exc)
                    continue
                with transaction() as conn:
                    cells_dal.update_density(conn, cell_id, result.total_results)
                refreshed += 1

    return RefreshSummary(
        cells_total=len(grid),
        cells_inserted=inserted,
        cells_density_refreshed=refreshed,
        cells_density_failed=failed,
        cells_owned_by_me=owned_count,
        wdgowars_synced=wdgowars_synced,
        wdgowars_error=wdgowars_error,
    )


def _cell_lookup(grid: list[Cell], cell_id: str) -> Cell | None:
    for cell in grid:
        if cell.id == cell_id:
            return cell
    return None
