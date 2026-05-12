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


@respx.mock
def test_plan_post_oneway_falls_back_to_geocoding_typed_query(client: TestClient) -> None:
    """If JS didn't set the hidden lat/lon, geocode the typed text server-side.

    We don't seed the grid; the planner will fail with 'no scored cells' AFTER
    geocoding the query. That tells us the destination-resolution path worked.
    """
    from warroute.clients.ors import DIRECTIONS_PATH, GEOCODE_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"type": "Point", "coordinates": [-72.30, 44.92]},
                        "properties": {
                            "name": "Kohl's",
                            "label": "Kohl's, Burlington, VT",
                            "layer": "venue",
                        },
                    }
                ]
            },
        )
    )
    # New: oneway now precheck-calls /directions to validate budget covers direct drive.
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 12000, "duration": 1200}, "geometry": None}]},
        )
    )
    # No `destination` hidden value, only `destination_query` typed text.
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "oneway",
            "destination": "",
            "destination_query": "Kohls Burlington VT",
        },
    )
    assert resp.status_code == 200
    # The geocoder was actually called by the fallback path.
    assert geocode_route.called
    # We did NOT bail with "needs a destination" — the fallback resolved it.
    assert "needs a destination" not in resp.text
    # Planner has no cells -> oneway gracefully falls back to direct route.
    assert "direct route" in resp.text.lower()
    assert "Plan #" in resp.text


@respx.mock
def test_plan_post_oneway_explicit_destination_skips_geocoder(client: TestClient) -> None:
    """When the hidden destination field is set, don't call the geocoder."""
    from warroute.clients.ors import DIRECTIONS_PATH, GEOCODE_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    # Oneway path now precheck-calls /directions for direct-route time.
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 12000, "duration": 1200}, "geometry": None}]},
        )
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "oneway",
            # Close enough to home that the distance pre-check doesn't reject it,
            # so the test actually exercises the "hidden field beats query" path.
            "destination": "44.96,-72.20",
            "destination_query": "noise that should be ignored",
        },
    )
    assert resp.status_code == 200
    assert not geocode_route.called
    # Planner has no cells -> falls back to direct route. The hidden field was used
    # (not the query) and the destination resolved into the direct leg.
    assert "Plan #" in resp.text
    assert "direct route" in resp.text.lower()


def test_plan_form_has_start_search_box(client: TestClient) -> None:
    """Starting location is now a type-ahead, mirroring destination."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert 'name="start_query"' in resp.text
    assert 'id="start-hits"' in resp.text
    assert 'data-field="start"' in resp.text
    assert 'data-field="destination"' in resp.text


@respx.mock
def test_plan_post_loop_uses_start_typed_query_via_geocoder(client: TestClient) -> None:
    """User types a starting address (not at home), didn't tap a hit."""
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"type": "Point", "coordinates": [-73.10, 44.50]},
                        "properties": {"name": "907 Smart St", "label": "907 Smart St, Newport, VT"},
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "loop",
            "start": "",
            "start_query": "907 Smart St Newport VT",
        },
    )
    assert resp.status_code == 200
    assert geocode_route.called
    # Planner fails downstream (no cells) but the start path succeeded.
    assert "No scored cells" in resp.text


@respx.mock
def test_plan_post_explicit_start_skips_geocoder(client: TestClient) -> None:
    """When the hidden start field has 'lat,lon', the geocoder is not called."""
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "loop",
            "start": "44.99,-72.13",
            "start_query": "ignored noise",
        },
    )
    assert resp.status_code == 200
    assert not geocode_route.called
    assert "No scored cells" in resp.text


def test_plan_post_blank_start_uses_settings_home(client: TestClient) -> None:
    """Empty start fields fall back to .env home — no geocoder call needed."""
    # No respx.mock decorator: this should NOT hit the network.
    resp = client.post(
        "/plan",
        data={"duration_min": "60", "mode": "loop", "start": "", "start_query": ""},
    )
    assert resp.status_code == 200
    assert "No scored cells" in resp.text


@respx.mock
def test_plan_post_oneway_rejects_destination_beyond_reachable_radius(
    client: TestClient,
) -> None:
    """The classic 'Pick and Shovel Newport VT' bug: geocoder returns a Pick-and-Shovel
    in California (~4400 km away). The route handler should reject this with a clear
    message naming the bad match instead of running the planner against an absurd target.
    """
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-120.674761, 35.354927],  # San Luis Obispo CA
                        },
                        "properties": {
                            "name": "Pick and Shovel",
                            "label": "Pick And Shovel, San Luis Obispo County, CA, USA",
                            "layer": "venue",
                        },
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "120",
            "mode": "oneway",
            "start": "",
            "start_query": "",
            "destination": "",
            "destination_query": "Pick and Shovel Newport VT",
        },
    )
    assert resp.status_code == 200
    assert "far beyond" in resp.text or "beyond" in resp.text
    assert "San Luis Obispo" in resp.text  # surfaces the bad match
    # Did NOT proceed to call ORS optimization with a 4400km destination.
    assert "Plan #" not in resp.text


@respx.mock
def test_plan_post_rejects_budget_smaller_than_direct_drive(client: TestClient) -> None:
    """Budget must at least cover the direct drive time."""
    from warroute.clients.ors import DIRECTIONS_PATH, ORS_API_BASE

    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {"summary": {"distance": 12000, "duration": 1800}, "geometry": None}
                ]
            },
        )
    )
    # User asks for 10 min total but destination is 30 min direct.
    resp = client.post(
        "/plan",
        data={
            "duration_min": "10",
            "mode": "oneway",
            "destination": "44.96,-72.20",
            "destination_query": "",
        },
    )
    assert resp.status_code == 200
    assert "min away by direct drive" in resp.text
    assert "Increase the budget" in resp.text


@respx.mock
def test_plan_post_falls_back_to_direct_route_when_no_cells_fit(
    client: TestClient,
) -> None:
    """When the planner can't fit any cells in the budget, oneway plans fall back
    to a direct-only route instead of erroring. User still gets a GMaps link."""
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    # No cells seeded -> planner will raise "no scored cells" PlannerError.
    # Directions mock provides the direct leg for the fallback.
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 5000, "duration": 600},
                        "geometry": "encoded_polyline",
                    }
                ]
            },
        )
    )
    # Optimization mock not strictly needed (planner fails before calling it)
    # but included so respx doesn't error if the planner does try.
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(return_value=httpx.Response(200, json={}))

    resp = client.post(
        "/plan",
        data={
            "duration_min": "20",
            "mode": "oneway",
            "destination": "44.96,-72.10",
            "destination_query": "",
        },
    )
    assert resp.status_code == 200
    # The plan_result page rendered (not the form with an error).
    assert "Plan #" in resp.text
    # And the user sees a clear notice explaining why there are no cells.
    assert "direct route" in resp.text.lower()
    assert "Could not fit" in resp.text


@respx.mock
def test_plan_post_loop_with_no_cells_falls_through_to_form(client: TestClient) -> None:
    """Loop with empty DB: no candidates -> exc.last_attempted_min is None ->
    auto-bump retry uses 2*budget=40 min. Still no cells -> form re-renders with
    a clear 'no viable plan' message."""
    resp = client.post(
        "/plan",
        data={"duration_min": "20", "mode": "loop", "start": "", "start_query": ""},
    )
    assert resp.status_code == 200
    # Either auto-bump-also-failed message OR the original "no scored cells" passes
    # through. Both are acceptable, both surface on the form (not a 500 / dead-end).
    text = resp.text.lower()
    assert "no scored cells" in text or "no viable plan" in text or "auto-bump" in text


@respx.mock
def test_plan_post_renders_friendly_error_on_ors_429(client: TestClient) -> None:
    """OrsQuotaError from the planner must not bubble up as a 500."""
    from warroute.clients.ors import OPTIMIZATION_PATH, ORS_API_BASE

    _seed_grid()  # planner gets past the no-cells check
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(return_value=httpx.Response(429))
    resp = client.post(
        "/plan",
        data={"duration_min": "60", "mode": "loop", "start": "", "start_query": ""},
    )
    assert resp.status_code == 200  # NOT 500 — friendly form re-render
    assert "ORS quota" in resp.text or "rate limit" in resp.text


def test_plan_invalid_mode(client: TestClient) -> None:
    resp = client.post("/plan", data={"duration_min": "60", "mode": "bogus"})
    assert resp.status_code == 200
    assert "Invalid mode" in resp.text


def test_plan_form_has_geocode_search_box(client: TestClient) -> None:
    """The lat/lon destination input was replaced with an HTMX type-ahead."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert 'hx-get="/plan/geocode"' in resp.text
    assert 'name="destination_query"' in resp.text
    assert 'id="destination-hits"' in resp.text


def test_geocode_short_query_returns_empty_body(client: TestClient) -> None:
    """Queries under 2 chars must not hit ORS (saves quota on every keystroke)."""
    resp = client.get("/plan/geocode", params={"q": "K"})
    assert resp.status_code == 200
    assert resp.text == ""


def test_geocode_empty_query_returns_empty_body(client: TestClient) -> None:
    resp = client.get("/plan/geocode", params={"q": ""})
    assert resp.status_code == 200
    assert resp.text == ""


@respx.mock
def test_geocode_returns_hits_as_html_partial(client: TestClient) -> None:
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"type": "Point", "coordinates": [-73.21, 44.47]},
                        "properties": {
                            "name": "Kohl's",
                            "label": "Kohl's, South Burlington, VT",
                            "layer": "venue",
                        },
                    }
                ]
            },
        )
    )
    resp = client.get("/plan/geocode", params={"q": "Kohls"})
    assert resp.status_code == 200
    # Jinja2 auto-escapes the apostrophe; match the rest of the label instead.
    assert "South Burlington" in resp.text
    assert 'data-lat="44.470000"' in resp.text
    assert 'data-lon="-73.210000"' in resp.text
    assert 'onclick="warrouteSelectGeocode' in resp.text


@respx.mock
def test_geocode_quota_renders_error_partial(client: TestClient) -> None:
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(return_value=httpx.Response(429))
    resp = client.get("/plan/geocode", params={"q": "anywhere"})
    assert resp.status_code == 200
    assert "quota" in resp.text.lower()


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
