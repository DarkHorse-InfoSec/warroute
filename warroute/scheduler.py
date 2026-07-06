"""Phase 6b.2: ntfy 'time to leave' alarm scanner.

Polls the `scheduled_departures` table for plans whose departure time is within
`lead_min` of now (and not yet notified) and fires one ntfy push per plan.
`notified_at` is set as the dedup key so a re-run within the same minute (or
seconds) doesn't fire twice.

Invoked from the CLI (`warroute notify-due`) by a systemd timer firing every
minute. Idempotent and quick: a single SQLite query + at most a few HTTP POSTs.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from warroute.clients.ntfy import NtfyClient
from warroute.config import get_settings
from warroute.db import transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueDeparture:
    """A scheduled_departures row that's due for notification."""

    plan_id: int
    departure_at: datetime
    arrive_by: datetime
    last_stop_label: str | None  # NULL = no stops persisted (legacy oneway destination)


def _parse_naive_iso(value: str) -> datetime:
    """Parse the naive ISO string we wrote in _attach_departure_time."""
    return datetime.fromisoformat(value)


def find_due_departures(
    conn: sqlite3.Connection, now: datetime, lead_min: int
) -> list[DueDeparture]:
    """SELECT scheduled_departures rows due for notification.

    "Due" means: departure_at - lead_min <= now AND notified_at IS NULL.
    Joins planned_routes for the stops_json (used to surface the last stop's
    label in the push body).
    """
    rows = conn.execute(
        """
        SELECT s.plan_id, s.departure_at, s.arrive_by, p.stops_json
        FROM scheduled_departures s
        JOIN planned_routes p ON p.id = s.plan_id
        WHERE s.notified_at IS NULL
        """,
    ).fetchall()
    due: list[DueDeparture] = []
    for row in rows:
        try:
            departure = _parse_naive_iso(row["departure_at"])
            arrive = _parse_naive_iso(row["arrive_by"])
        except (TypeError, ValueError):
            logger.warning(
                "scheduled_departures row %s has unparseable timestamps; skipping",
                row["plan_id"],
            )
            continue
        # Fire when we're within lead_min of departure (inclusive of "already late").
        # Past-due rows still fire on the next tick so the user sees them when they
        # check their phone, but a (much) later cleanup pass should reap stale ones.
        from datetime import timedelta

        if departure - timedelta(minutes=lead_min) > now:
            continue
        label: str | None = None
        if row["stops_json"]:
            import json

            try:
                stops = json.loads(row["stops_json"])
                if stops:
                    label = stops[-1].get("label")
            except (TypeError, ValueError):
                pass
        due.append(
            DueDeparture(
                plan_id=row["plan_id"],
                departure_at=departure,
                arrive_by=arrive,
                last_stop_label=label,
            )
        )
    return due


def _mark_notified(conn: sqlite3.Connection, plan_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE scheduled_departures SET notified_at = ? WHERE plan_id = ?",
        (now.replace(tzinfo=None).isoformat() if now.tzinfo else now.isoformat(), plan_id),
    )


async def _send_one(client: NtfyClient, due: DueDeparture, lead_min: int) -> bool:
    """Build + send the departure ntfy push. Returns True on success."""
    settings = get_settings()
    minutes_out = max(int((due.departure_at - datetime.now()).total_seconds() // 60), 0)
    title = f"Leave in {minutes_out} min" if minutes_out > 0 else "Time to leave"
    destination = due.last_stop_label or "your destination"
    body = (
        f"Plan #{due.plan_id} - depart at {due.departure_at.strftime('%H:%M')}"
        f" to arrive by {due.arrive_by.strftime('%H:%M')} at {destination}."
    )
    click = (
        f"{settings.web_base_url.rstrip('/')}/runs/{due.plan_id}" if settings.web_base_url else None
    )
    return await client.notify(
        body,
        title=title,
        priority=4,  # high, but not max - max (5) overrides Do Not Disturb on Android
        tags=["car", "alarm_clock"],
        click_url=click,
    )


async def notify_due(*, lead_min: int | None = None, now: datetime | None = None) -> int:
    """Scan + notify entrypoint. Returns the number of departures we notified.

    `now` is injected to make the function deterministically testable; defaults
    to `datetime.now()` (NAIVE local time, matching how _attach_departure_time
    persists departure_at).
    """
    settings = get_settings()
    effective_lead = lead_min if lead_min is not None else settings.ntfy_departure_lead_min
    current = now or datetime.now()

    with transaction() as conn:
        due = find_due_departures(conn, current, effective_lead)

    if not due:
        return 0
    if not settings.ntfy_topic:
        logger.info(
            "%d scheduled departures due but NTFY_TOPIC not set; nothing sent.",
            len(due),
        )
        return 0

    notified = 0
    async with NtfyClient() as client:
        for d in due:
            try:
                ok = await _send_one(client, d, effective_lead)
            except Exception as exc:
                # Best-effort: mark + log + continue so one broken row doesn't
                # spin forever or block other due rows.
                logger.warning(
                    "ntfy send failed for plan %d: %s; marking notified anyway", d.plan_id, exc
                )
                ok = True
            if ok:
                with transaction() as conn:
                    _mark_notified(conn, d.plan_id, current)
                notified += 1
    return notified


# Unused-import suppression for runtime: UTC kept exported for callers building
# tz-aware nows.
__all__ = ["UTC", "DueDeparture", "find_due_departures", "notify_due"]
