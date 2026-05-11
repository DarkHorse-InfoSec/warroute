"""Dashboard: today's quota, recent runs, badges, gang/rank."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request

from warroute.clients.wdgowars import WdgowarsClient, WdgowarsError
from warroute.db import transaction
from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class DashboardModel:
    player: object | None  # PlayerState or None if WDGoWars unreachable
    wdgowars_error: str | None
    recent_runs: list[dict[str, object]]
    cells_total: int
    cells_owned_by_me: int
    cells_with_density: int


@router.get("/")
async def get_dashboard(request: Request):  # type: ignore[no-untyped-def]
    player = None
    wdg_err: str | None = None
    try:
        async with WdgowarsClient() as wdg:
            player = await wdg.me()
    except WdgowarsError as exc:
        wdg_err = str(exc)
        logger.warning("Dashboard: WDGoWars unreachable: %s", exc)

    with transaction() as conn:
        recent_rows = conn.execute(
            """
            SELECT id, source, started_at, ended_at, total_aps, new_aps,
                   uploaded_wigle_at, uploaded_wdgowars_at
            FROM sessions
            ORDER BY started_at DESC
            LIMIT 10
            """
        ).fetchall()
        cells_total = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
        cells_me = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE wdgowars_owner = 'me'"
        ).fetchone()["n"]
        cells_density = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE estimated_total_aps IS NOT NULL"
        ).fetchone()["n"]

    model = DashboardModel(
        player=player,
        wdgowars_error=wdg_err,
        recent_runs=[dict(row) for row in recent_rows],
        cells_total=cells_total,
        cells_owned_by_me=cells_me,
        cells_with_density=cells_density,
    )
    return render(request, "dashboard.html", model=model)
