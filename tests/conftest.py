"""Shared fixtures. Keeps real .env out of unit tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from warroute.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force tests to use a tmp SQLite DB and dummy keys; never touch real .env."""
    monkeypatch.setenv("WIGLE_NAME", "test-name")
    monkeypatch.setenv("WIGLE_TOKEN", "test-token")
    monkeypatch.setenv("WDGOWARS_NAME", "test-user")
    monkeypatch.setenv("WDGOWARS_TOKEN", "test-token")
    monkeypatch.setenv("ORS_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("GPX_OUT_DIR", str(tmp_path / "gpx"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()
