"""End-to-end ingest tests for the orchestrator. External calls mocked."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.wdgowars import ME_PATH, WDGOWARS_API_BASE
from warroute.clients.wdgowars import UPLOAD_PATH as WDG_UPLOAD
from warroute.clients.wigle import WIGLE_API_BASE
from warroute.config import get_settings
from warroute.db import run_migrations, transaction
from warroute.uploader.orchestrator import ingest
from warroute.uploader.wdgowars_upload import WdgowarsUploadResult
from warroute.uploader.wigle_upload import UPLOAD_PATH as WIGLE_UPLOAD
from warroute.uploader.wigle_upload import WigleUploadResult

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_wiglewifi.csv"


def _mock_happy_path() -> None:
    respx.post(WIGLE_API_BASE + WIGLE_UPLOAD).mock(
        return_value=httpx.Response(200, json={"success": True, "transid": "w-1"})
    )
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "testuser", "points": 0, "daily_quota_remaining": 10000}
        )
    )
    respx.post(WDGOWARS_API_BASE + WDG_UPLOAD).mock(
        return_value=httpx.Response(200, json={"run_id": "r-1", "new_aps": 5})
    )


@respx.mock
async def test_ingest_records_session_and_observations() -> None:
    run_migrations()
    _mock_happy_path()

    result = await ingest(FIXTURE)
    assert result.session_id is not None and result.session_id > 0
    assert result.already_seen is False
    assert result.total_aps == 5  # fixture has 5 unique WiFi BSSIDs
    assert result.new_aps == 5  # first run, all are new
    assert isinstance(result.wigle, WigleUploadResult)
    assert isinstance(result.wdgowars, WdgowarsUploadResult)

    with transaction() as conn:
        sess = conn.execute(
            "SELECT new_aps, total_aps, uploaded_wigle_at, uploaded_wdgowars_at FROM sessions WHERE id = ?",
            (result.session_id,),
        ).fetchone()
        obs_count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
    assert sess["new_aps"] == 5
    assert sess["total_aps"] == 5
    assert sess["uploaded_wigle_at"] is not None
    assert sess["uploaded_wdgowars_at"] is not None
    assert obs_count == 5


@respx.mock
async def test_ingest_idempotent_on_same_sha256() -> None:
    run_migrations()
    _mock_happy_path()

    first = await ingest(FIXTURE)
    second = await ingest(FIXTURE)
    assert first.session_id == second.session_id
    assert second.already_seen is True
    assert second.new_aps == 0


@respx.mock
async def test_ingest_continues_when_wigle_fails() -> None:
    run_migrations()
    respx.post(WIGLE_API_BASE + WIGLE_UPLOAD).mock(return_value=httpx.Response(500))
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "points": 0, "daily_quota_remaining": 10000}
        )
    )
    respx.post(WDGOWARS_API_BASE + WDG_UPLOAD).mock(
        return_value=httpx.Response(200, json={"run_id": "r-1"})
    )

    result = await ingest(FIXTURE)
    assert result.session_id is not None
    assert isinstance(result.wigle, str)  # error message
    assert "failed" in result.wigle
    assert isinstance(result.wdgowars, WdgowarsUploadResult)

    with transaction() as conn:
        sess = conn.execute(
            "SELECT uploaded_wigle_at, uploaded_wdgowars_at FROM sessions WHERE id = ?",
            (result.session_id,),
        ).fetchone()
    assert sess["uploaded_wigle_at"] is None
    assert sess["uploaded_wdgowars_at"] is not None


@respx.mock
async def test_ingest_continues_when_wdgowars_quota_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    run_migrations()
    respx.post(WIGLE_API_BASE + WIGLE_UPLOAD).mock(
        return_value=httpx.Response(200, json={"success": True, "transid": "w-1"})
    )
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "points": 0, "daily_quota_remaining": 0}
        )
    )

    result = await ingest(FIXTURE)
    assert result.session_id is not None
    assert isinstance(result.wigle, WigleUploadResult)
    assert isinstance(result.wdgowars, str)
    assert "skipped" in result.wdgowars


@respx.mock
async def test_ingest_observations_dedup_across_runs() -> None:
    run_migrations()
    _mock_happy_path()

    # First run inserts 5 observations. Second run is no-op (sha dedup).
    # Modify the file to a fresh sha and re-ingest; should not create duplicate observations.
    await ingest(FIXTURE)

    with transaction() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
    assert n == 5


@respx.mock
async def test_ingest_fires_ntfy_when_topic_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "warroute-run")
    monkeypatch.setenv("NTFY_NOTIFY_RUN", "true")
    get_settings.cache_clear()
    run_migrations()
    _mock_happy_path()
    ntfy_route = respx.post("https://ntfy.sh/warroute-run").mock(
        return_value=httpx.Response(200, json={"id": "n1"})
    )

    result = await ingest(FIXTURE)
    assert result.session_id is not None
    assert ntfy_route.called
    sent = ntfy_route.calls.last.request
    body = sent.content.decode()
    assert "5 new APs" in body
    assert "WiGLE: ok" in body
    assert "WDGoWars: ok" in body
    assert sent.headers["Title"] == f"WarRoute: Run #{result.session_id}"
    assert sent.headers["Priority"] == "3"
    assert "car" in sent.headers["Tags"]


@respx.mock
async def test_ingest_skips_ntfy_when_topic_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "")
    get_settings.cache_clear()
    run_migrations()
    _mock_happy_path()
    ntfy_route = respx.post("https://ntfy.sh/whatever").mock(return_value=httpx.Response(200))

    await ingest(FIXTURE)
    assert ntfy_route.called is False


@respx.mock
async def test_ingest_skips_ntfy_when_toggle_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "warroute-run")
    monkeypatch.setenv("NTFY_NOTIFY_RUN", "false")
    get_settings.cache_clear()
    run_migrations()
    _mock_happy_path()
    ntfy_route = respx.post("https://ntfy.sh/warroute-run").mock(return_value=httpx.Response(200))

    await ingest(FIXTURE)
    assert ntfy_route.called is False


@respx.mock
async def test_ingest_succeeds_when_ntfy_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "warroute-run")
    monkeypatch.setenv("NTFY_NOTIFY_RUN", "true")
    get_settings.cache_clear()
    run_migrations()
    _mock_happy_path()
    respx.post("https://ntfy.sh/warroute-run").mock(side_effect=httpx.ConnectError("boom"))

    result = await ingest(FIXTURE)
    assert result.session_id is not None  # ingest succeeded despite ntfy failure
    assert isinstance(result.wigle, WigleUploadResult)
    assert isinstance(result.wdgowars, WdgowarsUploadResult)


@respx.mock
async def test_ingest_ntfy_click_url_uses_web_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "warroute-run")
    monkeypatch.setenv("WEB_BASE_URL", "https://warroute.example.com")
    get_settings.cache_clear()
    run_migrations()
    _mock_happy_path()
    ntfy_route = respx.post("https://ntfy.sh/warroute-run").mock(return_value=httpx.Response(200))

    result = await ingest(FIXTURE)
    sent = ntfy_route.calls.last.request
    assert sent.headers["Click"] == f"https://warroute.example.com/runs/{result.session_id}"
