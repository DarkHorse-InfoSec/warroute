"""Per-user prefs DAL + Caddy X-Forwarded-User header handling.

See DECISIONS.md 2026-05-14 (late evening) for the scope and threat model.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.types import Scope

from warroute.db import run_migrations
from warroute.web.app import create_app
from warroute.web.user_prefs import (
    DEFAULT_NAV_APP,
    current_username,
    effective_home,
    get_nav_app,
    get_prefs,
    set_credentials,
    set_nav_app,
    set_prefs,
)


def _make_request(user: str | None = None) -> Request:
    """Build a minimal Request with optional X-Forwarded-User header."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"test")]
    if user is not None:
        headers.append((b"x-forwarded-user", user.encode()))
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    req = Request(scope)
    # Some FastAPI versions store headers in scope only; force-set for safety.
    req._headers = Headers(scope=scope)  # type: ignore[attr-defined]
    return req


def test_current_username_returns_header_value() -> None:
    req = _make_request("alice")
    assert current_username(req) == "alice"


def test_current_username_lowercases() -> None:
    req = _make_request("Alice")
    assert current_username(req) == "alice"


def test_current_username_rejects_garbage() -> None:
    req = _make_request("alice; DROP TABLE users")
    assert current_username(req) is None


def test_current_username_rejects_empty() -> None:
    assert current_username(_make_request("")) is None
    assert current_username(_make_request(None)) is None
    assert current_username(_make_request("   ")) is None


def test_current_username_accepts_punctuation_in_allowlist() -> None:
    assert current_username(_make_request("alice.smith")) == "alice.smith"
    assert current_username(_make_request("alice_smith")) == "alice_smith"
    assert current_username(_make_request("alice-smith")) == "alice-smith"


def test_current_username_rejects_too_long() -> None:
    long_name = "a" * 255  # over the 254-char email maximum
    assert current_username(_make_request(long_name)) is None


def test_current_username_accepts_cloudflare_access_email() -> None:
    # Cloudflare Access forwards the authenticated user's email; it must pass the
    # allowlist so per-user prefs key off it (lowercased).
    assert current_username(_make_request("Alice.Smith@Example.com")) == "alice.smith@example.com"
    assert current_username(_make_request("bob+test@example.co.uk")) == "bob+test@example.co.uk"


def test_get_prefs_returns_none_for_missing_user() -> None:
    run_migrations()
    assert get_prefs(None) is None
    assert get_prefs("ghost-user") is None


def test_set_and_get_prefs_roundtrip() -> None:
    run_migrations()
    set_prefs("alice", 40.7128, -74.0060, "New York City")
    p = get_prefs("alice")
    assert p is not None
    assert p.username == "alice"
    assert p.home_lat == pytest.approx(40.7128)
    assert p.home_lon == pytest.approx(-74.0060)
    assert p.home_label == "New York City"


def test_set_prefs_upsert() -> None:
    run_migrations()
    set_prefs("alice", 40.7, -74.0, "NYC")
    set_prefs("alice", 41.0, -75.0, "Updated")
    p = get_prefs("alice")
    assert p is not None
    assert p.home_lat == pytest.approx(41.0)
    assert p.home_label == "Updated"


def test_set_prefs_rejects_bad_username() -> None:
    run_migrations()
    with pytest.raises(ValueError, match="Invalid username"):
        set_prefs("alice; DROP TABLE", 40.0, -74.0)


def test_set_prefs_rejects_out_of_range_coords() -> None:
    run_migrations()
    with pytest.raises(ValueError, match="out of range"):
        set_prefs("alice", 91.0, -74.0)
    with pytest.raises(ValueError, match="out of range"):
        set_prefs("alice", 40.0, -181.0)


def test_effective_home_ignores_saved_prefs_returns_fallback() -> None:
    """Security-pass 2026-07-05: effective_home no longer derives home from the
    spoofable X-Forwarded-User identity, even when a saved row exists. It always
    returns the caller-supplied (neutral) fallback; the real home comes from the
    browser client-side. This closes the 'spoof the header, read a user's home'
    disclosure on the public tier."""
    run_migrations()
    set_prefs("bob", 41.0, -75.0, "Bob's home")
    lat, lon, label = effective_home(_make_request("bob"), 44.94, -72.21)
    assert lat == pytest.approx(44.94)
    assert lon == pytest.approx(-72.21)
    assert label is None


def test_effective_home_falls_back_when_no_user() -> None:
    run_migrations()
    lat, lon, label = effective_home(_make_request(None), 44.94, -72.21)
    assert lat == pytest.approx(44.94)
    assert lon == pytest.approx(-72.21)
    assert label is None


def test_effective_home_falls_back_when_user_has_no_prefs() -> None:
    """Auth header present but no row -> env fallback. Local dev common case."""
    run_migrations()
    lat, lon, label = effective_home(_make_request("never-saved"), 44.94, -72.21)
    assert lat == pytest.approx(44.94)
    assert lon == pytest.approx(-72.21)
    assert label is None


# ---------------------------------------------------------------------------
# /settings HTTP integration
# ---------------------------------------------------------------------------


@pytest.fixture
def client():  # type: ignore[no-untyped-def]
    run_migrations()
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /plan integration: per-user home flows into the form + geocoder focus
# ---------------------------------------------------------------------------


def test_plan_form_get_falls_back_to_env_without_user(client: TestClient) -> None:
    """No header -> defaults section uses env home_lat/lon, no label hint."""
    resp = client.get("/plan")
    assert resp.status_code == 200
    # No user label shown when there's no per-user home.
    # The "(blank = home)" hint is the bare form.
    assert "(blank = home)" in resp.text


def test_user_prefs_isolated_per_user() -> None:
    """Different usernames have independent rows; no cross-contamination."""
    run_migrations()
    set_prefs("alice", 40.7, -74.0, "NYC")
    set_prefs("bob", 51.5, -0.1, "London")
    a = get_prefs("alice")
    b = get_prefs("bob")
    assert a is not None and a.home_label == "NYC"
    assert b is not None and b.home_label == "London"
    assert a.home_lat != b.home_lat


# ----------------------------------------------------------------------------
# Per-user API credentials (Phase tester-2 / DECISIONS.md 2026-05-14 very late)
# ----------------------------------------------------------------------------


def test_is_admin_false_for_unknown_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERS", "alice,bob")
    from warroute.config import get_settings
    from warroute.web.user_prefs import is_admin

    get_settings.cache_clear()
    assert is_admin("carol") is False
    assert is_admin(None) is False


def test_is_admin_true_for_listed_user_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERS", "alice, Bob , carol")
    from warroute.config import get_settings
    from warroute.web.user_prefs import is_admin

    get_settings.cache_clear()
    assert is_admin("alice") is True
    assert is_admin("BOB") is True
    assert is_admin("Carol") is True


def test_is_admin_empty_list_means_no_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERS", "")
    from warroute.config import get_settings
    from warroute.web.user_prefs import is_admin

    get_settings.cache_clear()
    assert is_admin("alice") is False


def test_credentials_default_to_empty_for_unknown_user() -> None:
    run_migrations()
    from warroute.web.user_prefs import UserCredentials, get_credentials

    creds = get_credentials("never-saved")
    assert creds == UserCredentials()
    assert creds.ors_api_key is None


def test_credentials_roundtrip() -> None:
    run_migrations()
    from warroute.web.user_prefs import get_credentials, set_credentials

    set_credentials(
        "alice",
        {
            "wigle_name": "AID12345",
            "wigle_token": "secret-wigle",
            "ors_api_key": "secret-ors",
            "ntfy_topic": "alice-topic",
        },
    )
    creds = get_credentials("alice")
    assert creds.wigle_name == "AID12345"
    assert creds.wigle_token == "secret-wigle"
    assert creds.ors_api_key == "secret-ors"
    assert creds.ntfy_topic == "alice-topic"
    # Unset fields stay None.
    assert creds.wdgowars_token is None


def test_set_credentials_blank_clears_field() -> None:
    """Blanking a field reverts that service to the system fallback."""
    run_migrations()
    from warroute.web.user_prefs import get_credentials, set_credentials

    set_credentials("alice", {"ors_api_key": "user-key"})
    assert get_credentials("alice").ors_api_key == "user-key"
    # Clear it.
    set_credentials("alice", {"ors_api_key": ""})
    assert get_credentials("alice").ors_api_key is None


def test_set_credentials_creates_row_when_home_not_yet_saved() -> None:
    """A user who sets creds before setting home should still get a user_prefs row."""
    run_migrations()
    from warroute.web.user_prefs import get_credentials, get_prefs, set_credentials

    set_credentials("newbie", {"ors_api_key": "abc123"})
    # Credentials saved.
    assert get_credentials("newbie").ors_api_key == "abc123"
    # And a prefs row exists (with env-default home).
    prefs = get_prefs("newbie")
    assert prefs is not None


def test_set_credentials_rejects_bad_username() -> None:
    run_migrations()
    from warroute.web.user_prefs import set_credentials

    with pytest.raises(ValueError):
        set_credentials("alice; DROP TABLE", {"ors_api_key": "x"})


def test_with_fallbacks_user_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """User value wins; env value backfills None fields."""
    monkeypatch.setenv("ORS_API_KEY", "system-ors")
    monkeypatch.setenv("WIGLE_TOKEN", "system-wigle")
    from warroute.config import get_settings
    from warroute.web.user_prefs import UserCredentials

    get_settings.cache_clear()
    eff = UserCredentials(ors_api_key="my-ors").with_fallbacks()
    assert eff.ors_api_key == "my-ors"  # user wins
    assert eff.wigle_token == "system-wigle"  # env fills


def test_with_fallbacks_empty_env_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither user nor env is set for a service, the effective value is None."""
    monkeypatch.setenv("MAPBOX_API_KEY", "")
    from warroute.config import get_settings
    from warroute.web.user_prefs import UserCredentials

    get_settings.cache_clear()
    eff = UserCredentials().with_fallbacks()
    assert eff.mapbox_api_key is None


def test_credential_fingerprints_source_labels() -> None:
    from warroute.web.user_prefs import UserCredentials, credential_fingerprints

    user = UserCredentials(wigle_token="abc12345", wdgowars_token=None)
    effective = UserCredentials(wigle_token="abc12345", wdgowars_token="env12345")
    rows = credential_fingerprints(user, effective)
    by_label = {r["label"]: r for r in rows}
    # WiGLE token: user has saved value -> "your saved value"
    assert by_label["WiGLE token"]["source"] == "your saved value"
    # WDGoWars token: user has none but env has one -> "system default"
    assert by_label["WDGoWars token"]["source"] == "system default"
    # ORS key: neither -> "not configured"
    assert by_label["ORS API key"]["source"] == "not configured"


# ---------------------------------------------------------------------------
# /settings + /dashboard HTTP integration with credentials
# ---------------------------------------------------------------------------


@respx.mock
def test_plan_geocode_uses_header_ors_key(client: TestClient) -> None:
    """Stateless tier: type-ahead geocode uses the ORS key the browser attaches."""
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE

    route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    resp = client.get("/plan/geocode", params={"q": "coffee"}, headers={"X-Ors-Key": "hdr-ors-key"})
    assert resp.status_code == 200
    assert route.called
    # OrsClient sets the raw key as the Authorization header (no Bearer prefix).
    assert route.calls.last.request.headers.get("Authorization") == "hdr-ors-key"


@respx.mock
def test_plan_geocode_without_any_key_returns_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user ORS key AND no shared key configured -> type-ahead is disabled
    (empty body, no ORS call). With a shared key it would geocode; that path is
    covered elsewhere."""
    monkeypatch.setenv("ORS_API_KEY", "")
    from warroute.clients.ors import GEOCODE_PATH, ORS_API_BASE
    from warroute.config import get_settings

    get_settings.cache_clear()
    route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    resp = client.get("/plan/geocode", params={"q": "coffee"})
    assert resp.status_code == 200
    assert resp.text.strip() == ""
    assert not route.called


@respx.mock
def test_dashboard_uses_header_wdgowars_token(client: TestClient) -> None:
    """Stateless tier: /dashboard uses the WDGoWars token the browser attaches."""
    from warroute.clients.wdgowars import ME_PATH, WDGOWARS_API_BASE

    route = respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "username": "alice",
                "total": 1,
                "wifi": 1,
                "ble": 0,
                "recent_today": 0,
            },
        )
    )
    resp = client.get("/dashboard/player", headers={"X-Wdgowars-Token": "hdr-wdg-token"})
    assert resp.status_code == 200
    assert route.called
    assert route.calls.last.request.headers.get("X-API-Key") == "hdr-wdg-token"


def test_user_prefs_with_planner_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security-pass 2026-07-05: a planner request built from effective_home uses
    the neutral fallback, NOT a saved per-user row. Real start coordinates come from
    the browser (the plan form's start/home fields), not a server-side identity."""
    from warroute.router.planner import PlanRequest

    run_migrations()
    set_prefs("alice", 41.0, -75.0, "Alice Apartment")
    req = _make_request("alice")
    lat, lon, _label = effective_home(req, 44.94, -72.21)
    plan_req = PlanRequest(home_lat=lat, home_lon=lon, duration_min=60, mode="loop")
    assert plan_req.home_lat == pytest.approx(44.94)
    assert plan_req.home_lon == pytest.approx(-72.21)


# ----------------------------------------------------------------------------
# Preferred navigation app (schema v5 / ACTIVE PLAN 2026-07-04)
# ----------------------------------------------------------------------------


def test_get_nav_app_defaults_when_no_row() -> None:
    run_migrations()
    assert get_nav_app("alice") == DEFAULT_NAV_APP


def test_get_nav_app_defaults_for_no_user() -> None:
    run_migrations()
    assert get_nav_app(None) == DEFAULT_NAV_APP


def test_set_and_get_nav_app_roundtrip() -> None:
    run_migrations()
    set_nav_app("alice", "waze")
    assert get_nav_app("alice") == "waze"
    set_nav_app("alice", "geo")
    assert get_nav_app("alice") == "geo"


def test_set_nav_app_creates_row_without_prior_home() -> None:
    """A user who sets a nav app but never saved a home still gets a row."""
    run_migrations()
    set_nav_app("carol", "apple")
    assert get_nav_app("carol") == "apple"
    prefs = get_prefs("carol")
    assert prefs is not None
    assert prefs.preferred_nav_app == "apple"


def test_set_nav_app_rejects_unknown_app() -> None:
    run_migrations()
    with pytest.raises(ValueError, match="Unknown nav app"):
        set_nav_app("alice", "bing_maps")


def test_set_nav_app_rejects_bad_username() -> None:
    run_migrations()
    with pytest.raises(ValueError, match="Invalid username"):
        set_nav_app("alice; DROP TABLE", "google")


def test_get_nav_app_falls_back_on_stale_stored_value() -> None:
    """A value no longer in VALID_NAV_APPS resolves to the default, not an error."""
    from warroute.db import transaction

    run_migrations()
    set_nav_app("alice", "waze")
    with transaction() as conn:
        conn.execute(
            "UPDATE user_prefs SET preferred_nav_app = ? WHERE username = ?",
            ("retired_app", "alice"),
        )
    assert get_nav_app("alice") == DEFAULT_NAV_APP


def test_nav_app_and_credentials_coexist() -> None:
    """Setting creds then a nav app (or vice versa) keeps both on one row."""
    run_migrations()
    set_credentials("dave", {"ors_api_key": "dave-key"})
    set_nav_app("dave", "geo")
    assert get_nav_app("dave") == "geo"
    prefs = get_prefs("dave")
    assert prefs is not None and prefs.preferred_nav_app == "geo"
