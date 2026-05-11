"""Orchestrate the full ingest of a single CSV file.

Flow:
  1. parse() the CSV (validates header, extracts observations + sha256)
  2. dedup against the sessions table by sha256 (idempotent re-runs are no-ops)
  3. count "new APs in this CSV" against our observations table
  4. asyncio.gather both uploads in parallel
  5. record a sessions row with both upload timestamps + outcomes
  6. update observations table (insert new BSSIDs, bump times_seen for repeats)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from warroute.clients.wdgowars import WdgowarsError
from warroute.clients.wigle import WigleError
from warroute.db import transaction
from warroute.uploader.parser import ParseResult, parse
from warroute.uploader.wdgowars_upload import (
    WdgowarsQuotaSkip,
    WdgowarsUploadResult,
    upload_to_wdgowars,
)
from warroute.uploader.wigle_upload import WigleUploadResult, upload_to_wigle

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    csv_path: Path
    csv_sha256: str
    session_id: int | None
    already_seen: bool
    total_aps: int
    new_aps: int
    wigle: WigleUploadResult | str | None  # success result OR error message
    wdgowars: WdgowarsUploadResult | str | None


async def ingest(csv_path: Path, source: str = "wigle-android") -> IngestResult:
    parsed = parse(csv_path)
    logger.info(
        "Parsed %s: %d observations, sha256=%s",
        csv_path.name,
        parsed.total_aps,
        parsed.csv_sha256[:12],
    )

    if existing := _existing_session(parsed.csv_sha256):
        return IngestResult(
            csv_path=csv_path,
            csv_sha256=parsed.csv_sha256,
            session_id=existing,
            already_seen=True,
            total_aps=parsed.total_aps,
            new_aps=0,
            wigle=None,
            wdgowars=None,
        )

    new_aps = _count_new_bssids(parsed)
    logger.info("New APs (vs prior observations): %d", new_aps)

    wigle_task = asyncio.create_task(_safe_wigle(csv_path))
    wdg_task = asyncio.create_task(_safe_wdgowars(csv_path, new_aps))
    wigle_outcome, wdgowars_outcome = await asyncio.gather(wigle_task, wdg_task)

    session_id = _record_session(parsed, source, new_aps, wigle_outcome, wdgowars_outcome)
    _record_observations(parsed, session_id)

    return IngestResult(
        csv_path=csv_path,
        csv_sha256=parsed.csv_sha256,
        session_id=session_id,
        already_seen=False,
        total_aps=parsed.total_aps,
        new_aps=new_aps,
        wigle=wigle_outcome,
        wdgowars=wdgowars_outcome,
    )


async def _safe_wigle(csv_path: Path) -> WigleUploadResult | str:
    try:
        return await upload_to_wigle(csv_path)
    except WigleError as exc:
        logger.warning("WiGLE upload failed: %s", exc)
        return f"failed: {exc}"


async def _safe_wdgowars(csv_path: Path, new_aps: int) -> WdgowarsUploadResult | str:
    try:
        return await upload_to_wdgowars(csv_path, new_aps_in_csv=new_aps)
    except WdgowarsQuotaSkip as exc:
        logger.info("WDGoWars upload skipped (quota): %s", exc)
        return f"skipped: {exc}"
    except WdgowarsError as exc:
        logger.warning("WDGoWars upload failed: %s", exc)
        return f"failed: {exc}"


def _existing_session(sha256: str) -> int | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE csv_sha256 = ?", (sha256,)
        ).fetchone()
    return int(row["id"]) if row else None


def _count_new_bssids(parsed: ParseResult) -> int:
    bssids = [obs.bssid for obs in parsed.observations]
    if not bssids:
        return 0
    placeholders = ",".join("?" * len(bssids))
    with transaction() as conn:
        rows = conn.execute(
            f"SELECT bssid FROM observations WHERE bssid IN ({placeholders})",
            bssids,
        ).fetchall()
    seen = {row["bssid"] for row in rows}
    return sum(1 for b in bssids if b not in seen)


def _record_session(
    parsed: ParseResult,
    source: str,
    new_aps: int,
    wigle: WigleUploadResult | str | None,
    wdg: WdgowarsUploadResult | str | None,
) -> int:
    now = datetime.utcnow().isoformat()
    wigle_at = now if isinstance(wigle, WigleUploadResult) and wigle.success else None
    wdg_at = now if isinstance(wdg, WdgowarsUploadResult) and wdg.success else None
    wdg_run = (
        str(wdg.raw.get("run_id"))
        if isinstance(wdg, WdgowarsUploadResult) and wdg.raw.get("run_id")
        else None
    )

    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sessions (
                source, csv_path, csv_sha256, started_at, ended_at,
                new_aps, total_aps,
                uploaded_wigle_at, uploaded_wdgowars_at, wdgowars_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                str(parsed.csv_path),
                parsed.csv_sha256,
                parsed.started_at.isoformat(),
                parsed.ended_at.isoformat(),
                new_aps,
                parsed.total_aps,
                wigle_at,
                wdg_at,
                wdg_run,
            ),
        )
        return int(cursor.lastrowid or 0)


def _record_observations(parsed: ParseResult, session_id: int) -> None:
    rows = [
        (
            obs.bssid,
            obs.ssid,
            obs.auth_mode,
            session_id,
            obs.lat,
            obs.lon,
            obs.first_seen.isoformat(),
        )
        for obs in parsed.observations
    ]
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO observations (
                bssid, ssid, encryption, first_seen_session,
                first_seen_lat, first_seen_lon, last_seen_at, times_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(bssid) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                times_seen   = times_seen + 1
            """,
            rows,
        )


def _conn_test_only() -> sqlite3.Connection:  # pragma: no cover
    """Indirection for tests that need to inspect the connection without touching the orchestrator."""
    raise NotImplementedError("Use warroute.db.transaction() directly in tests.")
