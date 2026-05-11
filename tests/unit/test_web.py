"""End-to-end tests for the FastAPI web UI. External APIs mocked via respx."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from warroute.clients.wdgowars import ME_PATH, WDGOWARS_API_BASE
from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.db import run_migrations, transaction
from warroute.web.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    run_migrations()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _seed_grid(radius_km: float = 4.0) -> list[str]:
    grid = cells_in_radius(44.9367, -72.2051, radius_km)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells").fetchall()]
        for i, cid in enumerate(ids):
            cells_dal.update_density(conn, cid, estimated_total_aps=10 + i)
    return ids


@respx.mock
def test_dashboard_renders_with_wdgowars_online(client: TestClient) -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "username": "darkhorse",
                "total": 61819,
                "wifi": 34870,
                "ble": 26949,
                "recent_today": 0,
            },
        )
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert "darkhorse" in resp.text
    assert "61,819" in resp.text


@respx.mock
def test_dashboard_renders_when_wdgowars_offline(client: TestClient) -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(500))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "WDGoWars unreachable" in resp.text
    assert "offline" in resp.text


def test_plan_form_renders(client: TestClient) -> None:
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert "Plan a drive" in resp.text
    assert 'name="duration_min"' in resp.text


def test_plan_post_with_no_cells_renders_error(client: TestClient) -> None:
    resp = client.post("/plan", data={"duration_min": "60", "mode": "loop"})
    assert resp.status_code == 200
    assert "No scored cells" in resp.text


def test_plan_post_oneway_without_destination_errors(client: TestClient) -> None:
    resp = client.post("/plan", data={"duration_min": "60", "mode": "oneway"})
    assert resp.status_code == 200
    assert "destination" in resp.text.lower()


def test_plan_invalid_mode(client: TestClient) -> None:
    resp = client.post("/plan", data={"duration_min": "60", "mode": "bogus"})
    assert resp.status_code == 200
    assert "Invalid mode" in resp.text


def test_coverage_renders_without_data(client: TestClient) -> None:
    resp = client.get("/coverage")
    assert resp.status_code == 200
    assert "Coverage" in resp.text
    assert "Mine" in resp.text  # legend


def test_coverage_geojson_empty_when_no_cells(client: TestClient) -> None:
    resp = client.get("/coverage/cells.geojson")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == "FeatureCollection"
    assert payload["features"] == []


def test_coverage_geojson_includes_seeded_cells(client: TestClient) -> None:
    _seed_grid()
    resp = client.get("/coverage/cells.geojson")
    payload = resp.json()
    assert len(payload["features"]) > 0
    sample = payload["features"][0]
    assert sample["geometry"]["type"] == "Polygon"
    assert "ownership" in sample["properties"]
    assert "estimated_aps" in sample["properties"]


def test_runs_404_for_missing_id(client: TestClient) -> None:
    resp = client.get("/runs/9999")
    assert resp.status_code == 404


def test_runs_renders_existing_session(client: TestClient) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions (source, csv_path, csv_sha256, started_at, ended_at,
                                  total_aps, new_aps, uploaded_wigle_at, uploaded_wdgowars_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wigle-android",
                "/tmp/x.csv",
                "deadbeef" * 8,
                "2026-05-11T10:00:00",
                "2026-05-11T10:30:00",
                100,
                47,
                "2026-05-11T10:31:00",
                None,
            ),
        )
        run_id = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()["id"]

    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    assert "+47" in resp.text
    assert "WiGLE" in resp.text


def test_settings_renders_and_redacts_secrets(client: TestClient) -> None:
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    # The conftest fixture sets WIGLE_TOKEN=test-token. Full value must not appear.
    assert "test-token" not in resp.text
    # But we should see the masked fingerprint
    assert "last4=oken" in resp.text
    # Other masked secrets too
    assert "WDGOWARS_TOKEN" in resp.text


def test_static_app_css_serves(client: TestClient) -> None:
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "WarRoute" in resp.text


def test_no_swagger_or_openapi(client: TestClient) -> None:
    """Single-tenant: docs/openapi disabled to keep the surface tight."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
