"""Stateless web tier: header-based credentials + shared-ORS guard.

See DECISIONS.md 2026-07-04 (design). These cover the primitives; route wiring +
frontend land in a later phase.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from starlette.datastructures import Headers
from starlette.types import Scope

from warroute.config import get_settings
from warroute.db import run_migrations, transaction
from warroute.web.creds import web_credentials
from warroute.web.routing_quota import (
    OrsSource,
    reset_rate_state,
    resolve_geocode_ors_key,
    resolve_ors_key,
)


def _req(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    raw.append((b"host", b"test"))
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "query_string": b"",
    }
    req = Request(scope)
    req._headers = Headers(scope=scope)  # type: ignore[attr-defined]
    return req


# --- web_credentials: headers only, no fallback -----------------------------


def test_web_credentials_reads_all_headers() -> None:
    creds = web_credentials(
        _req(
            {
                "X-Wigle-Name": "AID123",
                "X-Wigle-Token": "wtok",
                "X-Wdgowars-Name": "biscuit",
                "X-Wdgowars-Token": "dtok",
                "X-Ors-Key": "orskey",
                "X-Mapbox-Key": "mbkey",
                "X-Ntfy-Topic": "my-topic",
            }
        )
    )
    assert creds.wigle_name == "AID123"
    assert creds.wigle_token == "wtok"
    assert creds.wdgowars_name == "biscuit"
    assert creds.wdgowars_token == "dtok"
    assert creds.ors_api_key == "orskey"
    assert creds.mapbox_api_key == "mbkey"
    assert creds.ntfy_topic == "my-topic"


def test_web_credentials_absent_headers_are_none() -> None:
    creds = web_credentials(_req({}))
    assert creds.wigle_token is None
    assert creds.wdgowars_token is None
    assert creds.ors_api_key is None


def test_web_credentials_whitespace_is_none() -> None:
    creds = web_credentials(_req({"X-Wigle-Token": "   ", "X-Ors-Key": "\t"}))
    assert creds.wigle_token is None
    assert creds.ors_api_key is None


def test_web_credentials_never_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with a system WDGoWars token in env, absent header stays None: no fallback.
    monkeypatch.setenv("WDGOWARS_TOKEN", "system-should-not-leak")
    get_settings.cache_clear()
    creds = web_credentials(_req({}))
    assert creds.wdgowars_token is None


# --- resolve_ors_key: the shared-ORS carve-out guard ------------------------


@pytest.fixture
def _shared_ors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORS_API_KEY", "SHARED-ORS")
    monkeypatch.setenv("ORS_SHARED_DAILY_CAP", "3")
    monkeypatch.setenv("ORS_SHARED_RATE_PER_MIN", "2")
    get_settings.cache_clear()
    run_migrations()
    reset_rate_state()


def test_user_key_always_wins_and_is_not_counted(_shared_ors: None) -> None:
    res = resolve_ors_key("MY-OWN-KEY", "1.1.1.1", day="2026-07-04", now=100.0)
    assert res.source == OrsSource.USER
    assert res.key == "MY-OWN-KEY"
    with transaction() as conn:
        row = conn.execute("SELECT count FROM shared_routing_usage").fetchone()
    assert row is None  # user key never touches the shared counter


def test_no_shared_key_configured_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORS_API_KEY", "")
    get_settings.cache_clear()
    run_migrations()
    reset_rate_state()
    res = resolve_ors_key(None, "1.1.1.1", day="2026-07-04", now=100.0)
    assert res.source == OrsSource.NONE
    assert res.key is None


def test_shared_key_granted_and_counted(_shared_ors: None) -> None:
    res = resolve_ors_key(None, "2.2.2.2", day="2026-07-04", now=100.0)
    assert res.source == OrsSource.SHARED
    assert res.key == "SHARED-ORS"
    with transaction() as conn:
        count = conn.execute(
            "SELECT count FROM shared_routing_usage WHERE day = ?", ("2026-07-04",)
        ).fetchone()["count"]
    assert count == 1


def test_per_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # High daily cap so this test isolates the per-IP rate limit, not the cap.
    monkeypatch.setenv("ORS_API_KEY", "SHARED-ORS")
    monkeypatch.setenv("ORS_SHARED_DAILY_CAP", "100")
    monkeypatch.setenv("ORS_SHARED_RATE_PER_MIN", "2")
    get_settings.cache_clear()
    run_migrations()
    reset_rate_state()
    # cap rate = 2/min. Same ip, same instant -> 3rd is rate-limited.
    assert resolve_ors_key(None, "3.3.3.3", day="d", now=100.0).source == OrsSource.SHARED
    assert resolve_ors_key(None, "3.3.3.3", day="d", now=100.0).source == OrsSource.SHARED
    assert resolve_ors_key(None, "3.3.3.3", day="d", now=100.0).source == OrsSource.RATE_LIMITED
    # a different ip is unaffected
    assert resolve_ors_key(None, "4.4.4.4", day="d", now=100.0).source == OrsSource.SHARED
    # and the window slides: >60s later the first ip is allowed again
    assert resolve_ors_key(None, "3.3.3.3", day="d", now=200.0).source == OrsSource.SHARED


def test_daily_cap_exhausted(_shared_ors: None) -> None:
    # cap = 3. Pre-seed the counter at the cap.
    with transaction() as conn:
        conn.execute(
            "INSERT INTO shared_routing_usage (day, count) VALUES (?, ?)", ("2026-07-04", 3)
        )
    res = resolve_ors_key(None, "5.5.5.5", day="2026-07-04", now=100.0)
    assert res.source == OrsSource.QUOTA_EXHAUSTED
    assert res.key is None


def test_daily_cap_is_per_day(_shared_ors: None) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO shared_routing_usage (day, count) VALUES (?, ?)", ("2026-07-04", 3)
        )
    # a different day has its own fresh budget
    res = resolve_ors_key(None, "6.6.6.6", day="2026-07-05", now=100.0)
    assert res.source == OrsSource.SHARED


def test_geocode_key_has_own_window_and_own_daily_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geocode (address search) has a SEPARATE, more generous per-IP rate window
    from routing, never touches the ROUTING daily cap, and has its OWN daily cap
    (shared_geocode_usage) as a global backstop (security-pass 2026-07-05)."""
    monkeypatch.setenv("ORS_API_KEY", "SHARED-ORS")
    monkeypatch.setenv("ORS_SHARED_GEOCODE_RATE_PER_MIN", "3")
    monkeypatch.setenv("ORS_SHARED_RATE_PER_MIN", "2")
    get_settings.cache_clear()
    run_migrations()
    reset_rate_state()
    # geocode window allows 3/min for this IP
    for _ in range(3):
        assert (
            resolve_geocode_ors_key(None, "1.1.1.1", day="d", now=100.0).source == OrsSource.SHARED
        )
    assert (
        resolve_geocode_ors_key(None, "1.1.1.1", day="d", now=100.0).source
        == OrsSource.RATE_LIMITED
    )
    # routing has its OWN window (limit 2), unaffected by the geocode calls above
    assert resolve_ors_key(None, "1.1.1.1", day="d", now=100.0).source == OrsSource.SHARED
    # geocode did NOT increment the ROUTING daily cap (only the one routing grant did)
    with transaction() as conn:
        routing = conn.execute(
            "SELECT count FROM shared_routing_usage WHERE day = ?", ("d",)
        ).fetchone()
        geocode = conn.execute(
            "SELECT count FROM shared_geocode_usage WHERE day = ?", ("d",)
        ).fetchone()
    assert routing is not None and routing["count"] == 1
    # geocode increments its OWN counter (3 grants before the rate limit kicked in)
    assert geocode is not None and geocode["count"] == 3
    # a user's own key always wins, no rate limit
    assert resolve_geocode_ors_key("mine", "1.1.1.1", day="d", now=100.0).source == OrsSource.USER
