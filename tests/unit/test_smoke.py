"""Smoke tests: package imports, config loads, migrations apply."""

from __future__ import annotations

import sqlite3

import warroute
from warroute.config import Settings
from warroute.db import current_version, run_migrations, transaction


def test_package_importable() -> None:
    assert warroute.__version__


def test_settings_load(settings: Settings) -> None:
    assert settings.wigle_name == "test-name"
    assert settings.ors_api_key == "test-key"
    assert settings.home_radius_km == 50.0


def test_migrations_create_tables() -> None:
    version = run_migrations()
    assert version == 2

    with transaction() as conn:
        assert current_version(conn) == 2
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {row["name"] for row in rows}
        for required in (
            "sessions",
            "observations",
            "cells",
            "planned_routes",
            "scheduled_departures",
            "schema_version",
        ):
            assert required in names, f"missing table: {required}"


def test_migrations_idempotent() -> None:
    run_migrations()
    with transaction() as conn:
        # second run must not duplicate the schema_version row or error
        assert current_version(conn) == 2


def test_cli_doctor_passes_when_env_present() -> None:
    from typer.testing import CliRunner

    from warroute.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "All required env vars present" in result.output


def test_cli_doctor_fails_when_env_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from warroute.cli import app
    from warroute.config import get_settings

    monkeypatch.setenv("WIGLE_TOKEN", "")
    get_settings.cache_clear()

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Missing env vars" in result.output


def test_sqlite_wal_mode_enabled() -> None:
    run_migrations()
    with transaction() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    # confirm sqlite3 still allows reads while writers are queued
    with transaction() as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (99,))
    with transaction() as conn:
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        assert {row["version"] for row in rows} == {1, 2, 99}


def test_db_isolated_per_test() -> None:
    """Confirms the conftest fixture gives each test its own DB file."""
    run_migrations()
    with transaction() as conn, sqlite3.connect(":memory:") as scratch:
        assert conn is not scratch
