"""Dashboard: coverage stats + recent runs (shell), plus the WDGoWars player card
loaded as a header-carrying partial.

Stateless tier (DECISIONS.md 2026-07-04): a full-page load of "/" carries no
credential header (localStorage can't set headers on a plain navigation), so the
player card is fetched by htmx on load (hx-trigger="load"); the global
htmx:configRequest hook attaches the browser's WDGoWars key. The shell itself
needs no credentials (cells + runs come from the local DB).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from warroute.clients.wdgowars import WdgowarsAuthError, WdgowarsClient, WdgowarsError
from warroute.config import get_settings
from warroute.db import transaction
from warroute.web.creds import web_credentials
from warroute.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class DashboardShell:
    recent_runs: list[dict[str, object]]
    cells_total: int
    cells_owned_by_me: int
    cells_with_density: int
    expose_run_data: bool


@router.get("/")
async def get_dashboard(request: Request) -> HTMLResponse:
    # Recent runs link to /runs, which serves the operator's exact scanned-AP
    # coordinates and is gated by expose_run_data. Only surface the runs list when
    # that gate is open, so the public tier doesn't advertise operator run history
    # (security-pass 2026-07-05).
    expose_runs = get_settings().expose_run_data
    with transaction() as conn:
        recent_rows = (
            conn.execute(
                """
                SELECT id, source, started_at, ended_at, total_aps, new_aps,
                       uploaded_wigle_at, uploaded_wdgowars_at
                FROM sessions
                ORDER BY started_at DESC
                LIMIT 10
                """
            ).fetchall()
            if expose_runs
            else []
        )
        cells_total = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
        cells_me = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE wdgowars_owner = 'me'"
        ).fetchone()["n"]
        cells_density = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE estimated_total_aps IS NOT NULL"
        ).fetchone()["n"]

    shell = DashboardShell(
        recent_runs=[dict(row) for row in recent_rows],
        cells_total=cells_total,
        cells_owned_by_me=cells_me,
        cells_with_density=cells_density,
        expose_run_data=expose_runs,
    )
    return render(request, "dashboard.html", shell=shell)


@router.get("/dashboard/player")
async def get_dashboard_player(request: Request) -> HTMLResponse:
    """Partial: the WDGoWars player card. Uses the WDGoWars key the browser
    attaches; no key -> 'add your key' state (never the operator's key)."""
    creds = web_credentials(request)
    player = None
    wdg_err: str | None = None
    needs_key = not creds.wdgowars_token
    if not needs_key:
        try:
            async with WdgowarsClient(token=creds.wdgowars_token) as wdg:
                player = await wdg.me()
        except WdgowarsAuthError as exc:
            wdg_err = f"WDGoWars rejected your token. Check the key you saved in Settings. ({exc})"
            logger.warning("Dashboard player: WDGoWars auth error: %s", exc)
        except WdgowarsError as exc:
            wdg_err = str(exc)
            logger.warning("Dashboard player: WDGoWars unreachable: %s", exc)

    return render(
        request,
        "dashboard_player.html",
        player=player,
        wdgowars_error=wdg_err,
        needs_wdgowars_key=needs_key,
    )
