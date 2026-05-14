"""Tests for warroute.scheduler (Phase 6b.2 ntfy departure alarm)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from warroute.db import run_migrations, transaction
from warroute.scheduler import find_due_departures, notify_due


def _insert_test_plan(
    plan_id: int,
    departure_at: datetime,
    arrive_by: datetime,
    last_stop_label: str | None = "Work",
    notified_at: datetime | None = None,
) -> None:
    """Seed a planned_routes + scheduled_departures pair for testing."""
    stops_json = (
        json.dumps([{"lat": 44.95, "lon": -72.20, "label": last_stop_label, "dwell_min": 0}])
        if last_stop_label is not None
        else None
    )
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO planned_routes
                (id, home_lat, home_lon, duration_min, mode, waypoints_json, stops_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, 44.94, -72.21, 60, "oneway", "[]", stops_json),
        )
        conn.execute(
            """
            INSERT INTO scheduled_departures (plan_id, departure_at, arrive_by, notified_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                plan_id,
                departure_at.isoformat(),
                arrive_by.isoformat(),
                notified_at.isoformat() if notified_at else None,
            ),
        )


def test_find_due_returns_rows_within_lead_window() -> None:
    run_migrations()
    now = datetime(2026, 5, 14, 16, 55)
    # Departure at 17:00, lead=5min -> due at now (17:00 - 5 = 16:55 == now)
    _insert_test_plan(1, departure_at=datetime(2026, 5, 14, 17, 0), arrive_by=datetime(2026, 5, 14, 17, 30))
    # Departure at 18:00, lead=5min -> NOT due (17:55 > 16:55)
    _insert_test_plan(2, departure_at=datetime(2026, 5, 14, 18, 0), arrive_by=datetime(2026, 5, 14, 18, 30))

    with transaction() as conn:
        due = find_due_departures(conn, now=now, lead_min=5)

    assert [d.plan_id for d in due] == [1]
    assert due[0].last_stop_label == "Work"


def test_find_due_skips_already_notified() -> None:
    run_migrations()
    now = datetime(2026, 5, 14, 16, 55)
    _insert_test_plan(
        1,
        departure_at=datetime(2026, 5, 14, 17, 0),
        arrive_by=datetime(2026, 5, 14, 17, 30),
        notified_at=datetime(2026, 5, 14, 16, 50),  # already notified
    )

    with transaction() as conn:
        due = find_due_departures(conn, now=now, lead_min=5)

    assert due == []


def test_find_due_includes_past_due() -> None:
    """A row whose departure_at is already past still shows as due so the user
    sees it when they next check (alarm clock catch-up semantics)."""
    run_migrations()
    now = datetime(2026, 5, 14, 17, 30)
    _insert_test_plan(
        1, departure_at=datetime(2026, 5, 14, 17, 0), arrive_by=datetime(2026, 5, 14, 17, 30)
    )

    with transaction() as conn:
        due = find_due_departures(conn, now=now, lead_min=5)

    assert len(due) == 1


@respx.mock
async def test_notify_due_fires_ntfy_and_marks_notified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "warroute-test")
    monkeypatch.setenv("NTFY_BASE_URL", "https://ntfy.sh")
    from warroute.config import get_settings

    get_settings.cache_clear()

    run_migrations()
    now = datetime(2026, 5, 14, 16, 55)
    _insert_test_plan(
        7,
        departure_at=datetime(2026, 5, 14, 17, 0),
        arrive_by=datetime(2026, 5, 14, 17, 30),
        last_stop_label="Daycare",
    )

    route = respx.post("https://ntfy.sh/warroute-test").mock(
        return_value=httpx.Response(200, text="ok")
    )

    count = await notify_due(lead_min=5, now=now)

    assert count == 1
    assert route.called
    call = route.calls[0].request
    assert b"Plan #7" in call.content
    assert b"Daycare" in call.content
    # Header was set on the request
    assert "Title" in call.headers

    # notified_at was written so a re-run doesn't double-fire.
    with transaction() as conn:
        row = conn.execute(
            "SELECT notified_at FROM scheduled_departures WHERE plan_id = ?", (7,)
        ).fetchone()
    assert row["notified_at"] is not None

    count2 = await notify_due(lead_min=5, now=now)
    assert count2 == 0  # nothing new to do


async def test_notify_due_noop_when_topic_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "")
    from warroute.config import get_settings

    get_settings.cache_clear()
    run_migrations()
    now = datetime(2026, 5, 14, 16, 55)
    _insert_test_plan(
        1, departure_at=datetime(2026, 5, 14, 17, 0), arrive_by=datetime(2026, 5, 14, 17, 30)
    )

    count = await notify_due(lead_min=5, now=now)
    assert count == 0

    # Row stays un-notified so it'll fire once topic is configured.
    with transaction() as conn:
        row = conn.execute(
            "SELECT notified_at FROM scheduled_departures WHERE plan_id = ?", (1,)
        ).fetchone()
    assert row["notified_at"] is None


def test_lead_zero_only_fires_at_or_after_departure() -> None:
    """lead_min=0 means 'fire exactly at departure time' (or later)."""
    run_migrations()
    just_before = datetime(2026, 5, 14, 16, 59, 30)
    _insert_test_plan(
        1, departure_at=datetime(2026, 5, 14, 17, 0), arrive_by=datetime(2026, 5, 14, 17, 30)
    )
    with transaction() as conn:
        assert find_due_departures(conn, now=just_before, lead_min=0) == []
        # At exactly the departure time it fires.
        due_at = find_due_departures(
            conn, now=datetime(2026, 5, 14, 17, 0), lead_min=0
        )
        assert len(due_at) == 1


# unused import to keep timedelta in scope for callers (none in this file but documents intent)
_ = timedelta
