"""/runs/{id}: post-run breakdown."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from warroute.db import transaction
from warroute.web.templating import render

router = APIRouter()


@router.get("/{run_id}")
async def get_run(run_id: int, request: Request):  # type: ignore[no-untyped-def]
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT id, source, csv_path, csv_sha256, started_at, ended_at,
                   total_aps, new_aps, distance_km, points_earned,
                   uploaded_wigle_at, uploaded_wdgowars_at, wdgowars_run_id, created_at
            FROM sessions WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")

        related_plan = conn.execute(
            """
            SELECT id, duration_min, mode, estimated_new_aps, estimated_drive_min
            FROM planned_routes WHERE actual_session_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    return render(
        request,
        "run.html",
        run=dict(row),
        related_plan=dict(related_plan) if related_plan else None,
    )
