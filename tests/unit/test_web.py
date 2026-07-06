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

# Stateless tier: the browser attaches keys as headers. Give the default test
# client a full set so existing tests exercise the "keys present" path. Tests for
# the "no key" empty states use client_nokeys.
_CRED_HEADERS = {
    "X-Wigle-Name": "AIDtest",
    "X-Wigle-Token": "test-wigle",
    "X-Wdgowars-Token": "test-wdg",
    "X-Ors-Key": "test-ors",
}


@pytest.fixture(autouse=True)
def _reset_rate() -> None:
    from warroute.web.routing_quota import reset_rate_state

    reset_rate_state()


@pytest.fixture
def client() -> Iterator[TestClient]:
    run_migrations()
    app = create_app()
    with TestClient(app, headers=_CRED_HEADERS) as c:
        yield c


@pytest.fixture
def client_nokeys() -> Iterator[TestClient]:
    """A client that sends NO credential headers, for the 'add your key' states."""
    run_migrations()
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def expose_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the run-data endpoints (/runs, observations.geojson), which are OFF by
    default (they serve exact AP coordinates). Trusted/gated deployments opt in via
    EXPOSE_RUN_DATA; tests that exercise the run views need it on."""
    from warroute.config import get_settings

    monkeypatch.setenv("EXPOSE_RUN_DATA", "true")
    get_settings.cache_clear()


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
    resp = client.get("/dashboard/player")
    assert resp.status_code == 200
    assert "darkhorse" in resp.text
    assert "61,819" in resp.text


def test_dashboard_shell_renders_without_creds(client_nokeys: TestClient) -> None:
    """The dashboard shell (cells + runs + player placeholder) needs no keys."""
    resp = client_nokeys.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    assert "Cells in radius" in resp.text
    assert "/dashboard/player" in resp.text  # player card loads via htmx partial


@respx.mock
def test_dashboard_player_renders_when_wdgowars_offline(client: TestClient) -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(500))
    resp = client.get("/dashboard/player")
    assert resp.status_code == 200
    assert "WDGoWars unreachable" in resp.text


def test_plan_form_renders(client: TestClient) -> None:
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert "Plan a drive" in resp.text
    assert 'name="duration_min"' in resp.text


@respx.mock
def test_plan_post_with_no_cells_auto_paints_and_renders_notice(client: TestClient) -> None:
    """Empty DB no longer fails - planner paints grid, ORS routes, UI shows notice."""
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1800,
                        "distance": 25000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 25000, "duration": 1800}, "geometry": None}]},
        )
    )

    resp = client.post("/plan", data={"duration_min": "60", "mode": "loop"})
    assert resp.status_code == 200
    assert "Plan #" in resp.text
    assert "No coverage data for this area yet" in resp.text or "unprobed" in resp.text.lower()


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
    from warroute.clients.ors import DIRECTIONS_PATH, GEOCODE_PATH, OPTIMIZATION_PATH, ORS_API_BASE

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
    # Oneway precheck-calls /directions to validate budget covers direct drive.
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 12000, "duration": 1200}, "geometry": None}]},
        )
    )
    # With auto-paint, the planner gets candidate cells in the corridor and calls
    # /optimization. Mock it so the plan succeeds.
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 2000,
                        "distance": 18000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    # No `destination` hidden value, only `destination_query` typed text.
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "oneway",
            # Stateless model: the browser supplies the start; give a VT start near
            # the geocoded destination so the reachability pre-check passes.
            "start": "44.9367,-72.2051",
            "destination": "",
            "destination_query": "Kohls Burlington VT",
        },
    )
    assert resp.status_code == 200
    # The geocoder was actually called by the fallback path.
    assert geocode_route.called
    # We did NOT bail with "needs a destination" - the fallback resolved it.
    assert "needs a destination" not in resp.text
    assert "Plan #" in resp.text


@respx.mock
def test_plan_post_oneway_explicit_destination_skips_geocoder(client: TestClient) -> None:
    """When the hidden destination field is set, don't call the geocoder."""
    from warroute.clients.ors import DIRECTIONS_PATH, GEOCODE_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 12000, "duration": 1200}, "geometry": None}]},
        )
    )
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 2000,
                        "distance": 15000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "60",
            "mode": "oneway",
            # Explicit VT start (stateless: browser supplies it) so the destination
            # pre-check passes and the test exercises the "hidden field beats query" path.
            "start": "44.9367,-72.2051",
            "destination": "44.96,-72.20",
            "destination_query": "noise that should be ignored",
        },
    )
    assert resp.status_code == 200
    assert not geocode_route.called
    # The hidden field was used (not the query) and the plan went through.
    assert "Plan #" in resp.text


def test_plan_form_has_start_search_box(client: TestClient) -> None:
    """Starting location is a geocoder type-ahead; stops are added via #stops-list."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert 'name="start_query"' in resp.text
    assert 'id="start-hits"' in resp.text
    assert 'data-field="start"' in resp.text
    # Phase 6a: destination geocode-field replaced by a stops list + add-stop btn.
    assert 'id="stops-list"' in resp.text
    assert 'id="add-stop-btn"' in resp.text
    assert 'id="stop-row-template"' in resp.text


def _mock_ors_loop_optimization() -> None:
    """Helper: install respx mocks for the loop-plan ORS calls. Caller must be inside
    a @respx.mock-decorated test."""
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1800,
                        "distance": 25000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 25000, "duration": 1800}, "geometry": None}]},
        )
    )


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
                        "properties": {
                            "name": "907 Smart St",
                            "label": "907 Smart St, Newport, VT",
                        },
                    }
                ]
            },
        )
    )
    _mock_ors_loop_optimization()
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
    # The start path succeeded and the plan went through.
    assert "Plan #" in resp.text


@respx.mock
def test_plan_post_explicit_start_skips_geocoder(client: TestClient) -> None:
    """When the hidden start field has 'lat,lon', the geocoder is not called."""
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    geocode_route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    _mock_ors_loop_optimization()
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
    assert "Plan #" in resp.text


@respx.mock
def test_plan_post_blank_start_uses_settings_home(client: TestClient) -> None:
    """Empty start fields fall back to .env home."""
    _mock_ors_loop_optimization()
    resp = client.post(
        "/plan",
        data={"duration_min": "60", "mode": "loop", "start": "", "start_query": ""},
    )
    assert resp.status_code == 200
    assert "Plan #" in resp.text


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
            json={"routes": [{"summary": {"distance": 12000, "duration": 1800}, "geometry": None}]},
        )
    )
    # User asks for 10 min total but destination is 30 min direct.
    resp = client.post(
        "/plan",
        data={
            "duration_min": "10",
            "mode": "oneway",
            "start": "44.9367,-72.2051",
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
    to a direct-only route instead of erroring. User still gets a GMaps link.

    With auto-paint, "no cells" no longer triggers this path; the trigger is the
    planner exhausting back-off (ORS always returns over-budget durations).
    """
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

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
    # Always over-budget: 99999s >> any budget. Planner halves chosen list until
    # MIN_WAYPOINTS then raises -> oneway path catches and renders direct fallback.
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 99999,
                        "distance": 99999,
                        "steps": [{"type": "job", "job": 0}],
                    }
                ]
            },
        )
    )

    resp = client.post(
        "/plan",
        data={
            "duration_min": "20",
            "mode": "oneway",
            "start": "44.9367,-72.2051",
            "destination": "44.96,-72.10",
            "destination_query": "",
        },
    )
    assert resp.status_code == 200
    assert "Plan #" in resp.text
    assert "direct route" in resp.text.lower()
    assert "Could not fit" in resp.text


@respx.mock
def test_plan_post_oneway_no_budget_routes_direct(client: TestClient) -> None:
    """Oneway with a blank time budget = "just get me there": skip the planner and
    route the direct path to the destination. No OPTIMIZATION call is made."""
    from warroute.clients.ors import DIRECTIONS_PATH, ORS_API_BASE

    directions = respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 42000, "duration": 3000},
                        "geometry": "encoded_polyline",
                    }
                ]
            },
        )
    )

    resp = client.post(
        "/plan",
        data={
            "duration_min": "",  # blank budget
            "mode": "oneway",
            "destination": "44.96,-72.10",
            "destination_query": "",
        },
    )
    assert resp.status_code == 200
    assert "Plan #" in resp.text
    assert "No time budget set" in resp.text
    assert directions.called


def test_plan_post_loop_without_budget_errors(client: TestClient) -> None:
    """Loop mode needs a budget (it sets the loop's size). A blank one is a clear
    error, not a silent default, and never hits the routing service."""
    resp = client.post("/plan", data={"duration_min": "", "mode": "loop"})
    assert resp.status_code == 200
    assert "needs a time budget" in resp.text


@respx.mock
def test_plan_post_loop_auto_bumps_when_ors_says_over_budget(client: TestClient) -> None:
    """Loop where the budget is too tight for what ORS computes -> auto-bump retry.

    With auto-paint, "empty DB" is no longer the trigger; the trigger is ORS
    returning over-budget durations. First call over, second call (at bumped
    budget) under -> result page renders with the loop_bumped_notice.
    """
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        side_effect=[
            # First plan @ requested budget: way over -> raises after back-off exhausts.
            httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "vehicle": 1,
                            "duration": 99999,
                            "distance": 99999,
                            "steps": [{"type": "job", "job": 0}],
                        }
                    ]
                },
            ),
            # Retry at bumped budget: fits.
            httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "vehicle": 1,
                            "duration": 1500,
                            "distance": 20000,
                            "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                        }
                    ]
                },
            ),
        ]
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 20000, "duration": 1500}, "geometry": None}]},
        )
    )
    resp = client.post(
        "/plan",
        data={"duration_min": "20", "mode": "loop", "start": "", "start_query": ""},
    )
    assert resp.status_code == 200
    text = resp.text.lower()
    # Either we got the bumped plan or - if ORS exhausts both attempts - the
    # form re-render. Both are acceptable, both end in a non-500 user-visible result.
    assert "plan #" in text or "no viable plan" in text or "auto-bump" in text


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
    assert resp.status_code == 200  # NOT 500 - friendly form re-render
    assert "ORS quota" in resp.text or "rate limit" in resp.text


def test_plan_invalid_mode(client: TestClient) -> None:
    resp = client.post("/plan", data={"duration_min": "60", "mode": "bogus"})
    assert resp.status_code == 200
    assert "Invalid mode" in resp.text


def test_plan_form_has_geocode_search_box(client: TestClient) -> None:
    """Geocoder type-ahead is wired on the start field + each stop row via the
    data-geocode-input marker (dispatched by app.js delegation, not an inline
    handler - the nonce-based CSP forbids inline on* attributes)."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    assert "data-geocode-input" in resp.text
    # Stops list is the new home for destination input (added via JS).
    assert 'class="stop-query"' in resp.text  # in the <template>


@respx.mock
def test_plan_post_accepts_stops_form_list(client: TestClient) -> None:
    """The form posts name='stops' as a list of 'lat,lon[:dwell]' strings.

    Two stops -> multistop planner: 2 segments, each calls /optimization + the
    full-chain /directions. All ORS calls use the same mocked response.
    """
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1200,
                        "distance": 15000,
                        "steps": [{"type": "job", "job": 0}],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 15000, "duration": 1200},
                        "geometry": None,
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/plan",
        data={
            "duration_min": "90",
            "mode": "oneway",
            "start": "44.9367,-72.2051",
            "start_query": "",
            "stops": ["44.95,-72.20:5", "44.96,-72.19"],
        },
    )
    assert resp.status_code == 200
    assert "Plan #" in resp.text


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
    assert 'data-action="geocode-select"' in resp.text


@respx.mock
def test_geocode_quota_renders_error_partial(client: TestClient) -> None:
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(return_value=httpx.Response(429))
    resp = client.get("/plan/geocode", params={"q": "anywhere"})
    assert resp.status_code == 200
    assert "quota" in resp.text.lower()


def test_geocode_coordinates_return_an_exact_pin(client: TestClient) -> None:
    """Typing 'lat,lon' drops an exact pin (no geocoder needed)."""
    resp = client.get("/plan/geocode", params={"q": "44.94324, -72.00880"})
    assert resp.status_code == 200
    assert "Dropped pin" in resp.text
    assert 'data-lat="44.943240"' in resp.text
    assert 'data-lon="-72.008800"' in resp.text


@respx.mock
def test_geocode_numbered_address_uses_census_first(client: TestClient) -> None:
    """A leading house number tries the US Census geocoder for precision."""
    from warroute.clients.census import CENSUS_API_BASE, ONELINE_PATH

    census = respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "1414 MEAD HILL RD, DERBY, VT, 05829",
                            "coordinates": {"x": -72.00880, "y": 44.94324},
                        }
                    ]
                }
            },
        )
    )
    resp = client.get("/plan/geocode", params={"q": "1414 Mead Hill Rd Derby VT"})
    assert resp.status_code == 200
    assert census.called
    assert "MEAD HILL" in resp.text


@respx.mock
def test_geocode_census_miss_falls_back_to_ors(client: TestClient) -> None:
    """If Census has no match, fall back to ORS (worldwide)."""
    from warroute.clients.census import CENSUS_API_BASE, ONELINE_PATH
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": []}})
    )
    ors = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"type": "Point", "coordinates": [-72.05, 44.98]},
                        "properties": {"name": "Mead Hill Road", "label": "Mead Hill Road, VT"},
                    }
                ]
            },
        )
    )
    resp = client.get("/plan/geocode", params={"q": "1414 Mead Hill Rd Holland VT"})
    assert resp.status_code == 200
    assert ors.called  # fell back to ORS
    assert "Mead Hill Road" in resp.text


def test_us_state_extraction() -> None:
    from warroute.web.routes.plan import _us_state_from

    assert _us_state_from("131 Palin Farm Rd, DERBY, VT, 05829") == "VT"
    assert _us_state_from("1160 Howell Mill Rd NW, Atlanta, GA 30318") == "GA"
    assert _us_state_from("just a road name") is None


@respx.mock
def test_geocode_bare_street_retries_census_with_home_state(client: TestClient) -> None:
    """A bare street (no town) is retried against Census with the home state
    appended, so it resolves to the exact house."""
    from warroute.clients.census import CENSUS_API_BASE, ONELINE_PATH

    def _side(request: httpx.Request) -> httpx.Response:
        addr = request.url.params.get("address", "")
        if addr.endswith(", VT"):  # the retry with home state
            return httpx.Response(
                200,
                json={
                    "result": {
                        "addressMatches": [
                            {
                                "matchedAddress": "1414 MEAD HILL RD, DERBY, VT, 05829",
                                "coordinates": {"x": -72.00880, "y": 44.94324},
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"result": {"addressMatches": []}})

    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(side_effect=_side)
    resp = client.get(
        "/plan/geocode",
        params={"q": "1414 Mead Hill Road", "near": "131 Palin Farm Rd, DERBY, VT, 05829"},
    )
    assert resp.status_code == 200
    assert "MEAD HILL" in resp.text


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


@respx.mock
def test_coverage_gangs_geojson_projects_hulls(client: TestClient) -> None:
    from warroute.clients.wdgowars import TERRITORIES_PATH

    respx.get(WDGOWARS_API_BASE + TERRITORIES_PATH).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Biscuits",
                    "gang_id": 16,
                    "color": "#22d3ee",
                    "rank": 3,
                    "hull": [[44.9, -72.2], [45.0, -72.1], [44.8, -72.0]],
                },
                {"name": "TooSmall", "gang_id": 7, "hull": [[1.0, 2.0]]},
            ],
        )
    )
    resp = client.get("/coverage/gangs.geojson")
    assert resp.status_code == 200
    payload = resp.json()
    # The degenerate 1-point hull is dropped; only Biscuits survives.
    assert len(payload["features"]) == 1
    feat = payload["features"][0]
    assert feat["properties"]["is_ours"] is True
    ring = feat["geometry"]["coordinates"][0]
    # [lat, lon] input -> [lon, lat] GeoJSON, and the ring is closed.
    assert ring[0] == [-72.2, 44.9]
    assert ring[0] == ring[-1]


@respx.mock
def test_coverage_gangs_geojson_degrades_on_error(client: TestClient) -> None:
    from warroute.clients.wdgowars import TERRITORIES_PATH

    respx.get(WDGOWARS_API_BASE + TERRITORIES_PATH).mock(return_value=httpx.Response(500))
    resp = client.get("/coverage/gangs.geojson")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["features"] == []
    assert "error" in payload


# --- Stateless tier: no-key states + shared-ORS carve-out (S1b) --------------


def test_dashboard_player_without_key_shows_add_key_prompt(client_nokeys: TestClient) -> None:
    resp = client_nokeys.get("/dashboard/player")
    assert resp.status_code == 200
    assert "Add your WDGoWars key" in resp.text


def test_gangs_geojson_without_key_is_empty(client_nokeys: TestClient) -> None:
    resp = client_nokeys.get("/coverage/gangs.geojson")
    assert resp.status_code == 200
    assert resp.json()["features"] == []


def test_plan_without_any_ors_key_prompts_for_one(
    client_nokeys: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user ORS key AND no shared key configured -> friendly 'add your key'."""
    monkeypatch.setenv("ORS_API_KEY", "")
    from warroute.config import get_settings

    get_settings.cache_clear()
    resp = client_nokeys.post("/plan", data={"duration_min": "60", "mode": "loop"})
    assert resp.status_code == 200
    assert "OpenRouteService" in resp.text


@respx.mock
def test_plan_uses_shared_ors_key_when_user_has_none(
    client_nokeys: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user ORS key but a shared key is configured -> routing works via shared,
    and the shared daily counter increments."""
    monkeypatch.setenv("ORS_API_KEY", "shared-ors-key")
    from warroute.clients.ors import DIRECTIONS_PATH, OPTIMIZATION_PATH, ORS_API_BASE
    from warroute.config import get_settings

    get_settings.cache_clear()
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1800,
                        "distance": 25000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    directions = respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 25000, "duration": 1800}, "geometry": None}]},
        )
    )
    resp = client_nokeys.post("/plan", data={"duration_min": "60", "mode": "loop"})
    assert resp.status_code == 200
    assert "Plan #" in resp.text
    # The shared key was the one sent to ORS.
    assert directions.calls.last.request.headers.get("Authorization") == "shared-ors-key"
    with transaction() as conn:
        used = conn.execute("SELECT count FROM shared_routing_usage").fetchone()
    assert used is not None and used["count"] >= 1


def test_settings_renders_client_side_editor(client_nokeys: TestClient) -> None:
    """Stateless /settings is a client-side editor: no server-side prefs, and it
    tells the user their keys stay in the browser."""
    resp = client_nokeys.get("/settings")
    assert resp.status_code == 200
    assert "only in this browser" in resp.text
    assert 'data-cfg="wigle_token"' in resp.text
    assert 'data-cfg="ors_key"' in resp.text
    assert "Preferred navigation app" in resp.text
    # Units toggle (metric/imperial), converted client-side via app.js.
    assert 'data-cfg="units"' in resp.text
    assert 'value="imperial"' in resp.text


def test_distances_carry_data_dist_km_for_unit_conversion(client: TestClient) -> None:
    """Coverage radius + plan speed note carry data-* so app.js can convert units."""
    cov = client.get("/coverage")
    assert "data-dist-km=" in cov.text
    plan = client.get("/plan")
    assert "data-speed-kmh=" in plan.text


def test_settings_post_routes_are_gone(client: TestClient) -> None:
    """The old server-side settings POST endpoints no longer exist."""
    assert client.post("/settings/home", data={}).status_code == 404
    assert client.post("/settings/credentials", data={}).status_code == 404
    assert client.post("/settings/nav-app", data={}).status_code == 404


def test_runs_404_for_missing_id(client: TestClient, expose_runs: None) -> None:
    resp = client.get("/runs/9999")
    assert resp.status_code == 404


def test_runs_gated_404_when_not_exposed(client: TestClient) -> None:
    """Default deployment (expose_run_data off): the run-data endpoints 404 even
    for a real run, so a public instance never leaks scanned-AP coordinates."""
    run_id = _seed_run_with_observations()
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.get(f"/runs/{run_id}/observations.geojson").status_code == 404


def test_runs_renders_existing_session(client: TestClient, expose_runs: None) -> None:
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


def _seed_run_with_observations(new_aps: int = 3) -> int:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions (source, csv_path, csv_sha256, started_at, ended_at,
                                  total_aps, new_aps)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wigle-android",
                "/tmp/y.csv",
                "beadfeed" * 8,
                "2026-05-11T10:00:00",
                "2026-05-11T10:30:00",
                new_aps,
                new_aps,
            ),
        )
        run_id = int(
            conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]
        )
        rows = [
            ("aa:bb:cc:dd:ee:01", "HomeNet", "[WPA2-PSK-CCMP][ESS]", 44.95, -72.20),
            ("aa:bb:cc:dd:ee:02", "OpenGuest", "[ESS]", 44.96, -72.21),
            # attacker-controlled SSID: must never be inlined raw into page HTML
            ("aa:bb:cc:dd:ee:03", "<img src=x onerror=alert(1)>", "[WEP][ESS]", 44.97, -72.22),
        ]
        for bssid, ssid, enc, lat, lon in rows:
            conn.execute(
                """
                INSERT INTO observations (bssid, ssid, encryption, first_seen_session,
                                          first_seen_lat, first_seen_lon, last_seen_at, times_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (bssid, ssid, enc, run_id, lat, lon, "2026-05-11T10:20:00", 2),
            )
    return run_id


def test_runs_observations_geojson(client: TestClient, expose_runs: None) -> None:
    run_id = _seed_run_with_observations()
    gj = client.get(f"/runs/{run_id}/observations.geojson")
    assert gj.status_code == 200
    data = gj.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 3
    for f in data["features"]:
        lon, lat = f["geometry"]["coordinates"]  # GeoJSON is lon,lat
        assert -73 < lon < -72 and 44 < lat < 45
        assert "bssid" in f["properties"] and "encryption" in f["properties"]


def test_runs_page_renders_map_and_escapes_ssid(client: TestClient, expose_runs: None) -> None:
    run_id = _seed_run_with_observations()
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert 'id="map"' in page.text
    assert "3 mapped" in page.text
    # The map is fetched async as JSON and escaped client-side; the raw malicious
    # SSID must not be inlined into the server-rendered HTML.
    assert "<img src=x onerror" not in page.text


def test_runs_page_no_observations_shows_note(client: TestClient, expose_runs: None) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (source, csv_path, csv_sha256, started_at, ended_at,"
            " total_aps, new_aps) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "wigle-android",
                "/tmp/z.csv",
                "cafef00d" * 8,
                "2026-05-11T10:00:00",
                "2026-05-11T10:30:00",
                0,
                0,
            ),
        )
        run_id = int(
            conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]
        )
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert 'id="map"' not in page.text
    assert "No mapped access points" in page.text


def test_static_app_css_serves(client: TestClient) -> None:
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "WarRoute" in resp.text


def test_no_swagger_or_openapi(client: TestClient) -> None:
    """Single-tenant: docs/openapi disabled to keep the surface tight."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- Security headers (Eng #36 pentest remediation, findings #4 + #6) --------


def test_csp_is_nonce_based_no_unsafe_inline(client_nokeys: TestClient) -> None:
    """script-src must be 'self' + a per-request nonce, with NO 'unsafe-inline'
    and NO CDN hosts (htmx + leaflet are self-hosted). Finding #4."""
    resp = client_nokeys.get("/")
    csp = resp.headers["content-security-policy"]
    # Isolate the script-src directive.
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'self'" in script_src
    assert "'nonce-" in script_src
    assert "'unsafe-inline'" not in script_src
    assert "unpkg.com" not in csp
    assert "jsdelivr" not in csp
    # style-src keeps 'unsafe-inline' on purpose (Leaflet + inline style attrs).
    style_src = next(d for d in csp.split(";") if d.strip().startswith("style-src"))
    assert "'unsafe-inline'" in style_src


def test_csp_nonce_matches_inline_script_tag(client_nokeys: TestClient) -> None:
    """The nonce in the CSP header must match the nonce rendered into the page's
    inline <script> tags, or the browser blocks them."""
    resp = client_nokeys.get("/plan")
    csp = resp.headers["content-security-policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    nonce = script_src.split("'nonce-", 1)[1].split("'", 1)[0]
    assert nonce  # non-empty
    assert f'<script nonce="{nonce}">' in resp.text


def test_csp_nonce_is_fresh_per_request(client_nokeys: TestClient) -> None:
    """A nonce must never be reused across responses."""
    n1 = client_nokeys.get("/").headers["content-security-policy"]
    n2 = client_nokeys.get("/").headers["content-security-policy"]
    assert n1 != n2


def test_permissions_policy_denies_sensitive_features(client_nokeys: TestClient) -> None:
    """Permissions-Policy present and denies geolocation/camera/mic. Finding #6."""
    resp = client_nokeys.get("/")
    pp = resp.headers["permissions-policy"]
    assert "geolocation=()" in pp
    assert "camera=()" in pp
    assert "microphone=()" in pp


def test_vendored_libs_served_locally(client: TestClient) -> None:
    """htmx + Leaflet are self-hosted under /static/vendor (Finding #3), removing
    the third-party CDN dependency entirely."""
    assert client.get("/static/vendor/htmx-1.9.12.min.js").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.css").status_code == 200
    assert client.get("/static/vendor/leaflet/images/marker-icon.png").status_code == 200
    # base template references the local paths, not the CDN.
    page = client.get("/plan")
    assert "/static/vendor/htmx-1.9.12.min.js" in page.text
    assert "unpkg.com" not in page.text
