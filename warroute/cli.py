"""Typer CLI entrypoint. Subcommands grow per phase."""

from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich.console import Console

from warroute import __version__
from warroute.config import get_settings
from warroute.db import run_migrations

app = typer.Typer(
    name="warroute",
    help="Wardriving route planner and dual-uploader.",
    no_args_is_help=True,
)
coverage_app = typer.Typer(name="coverage", help="Cell ownership + AP density.", no_args_is_help=True)
app.add_typer(coverage_app, name="coverage")
console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG logging")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@app.command()
def version() -> None:
    """Print the WarRoute version."""
    console.print(f"warroute {__version__}")


@app.command()
def doctor() -> None:
    """Verify environment: required env vars present, DB reachable."""
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("WIGLE_NAME", settings.wigle_name),
            ("WIGLE_TOKEN", settings.wigle_token),
            ("WDGOWARS_NAME", settings.wdgowars_name),
            ("WDGOWARS_TOKEN", settings.wdgowars_token),
            ("ORS_API_KEY", settings.ors_api_key),
        )
        if not value
    ]
    if missing:
        console.print(f"[red]Missing env vars:[/red] {', '.join(missing)}")
        raise typer.Exit(code=1)
    console.print("[green]All required env vars present.[/green]")
    console.print(f"DB target: {settings.sqlite_path}")


@app.command()
def migrate() -> None:
    """Apply SQL migrations. Idempotent."""
    new_version = run_migrations()
    console.print(f"[green]Schema at version {new_version}.[/green]")


@coverage_app.command("refresh")
def coverage_refresh(
    home_lat: float = typer.Option(None, help="Override HOME_LAT from .env"),
    home_lon: float = typer.Option(None, help="Override HOME_LON from .env"),
    radius_km: float = typer.Option(None, help="Override HOME_RADIUS_KM from .env"),
) -> None:
    """Paint the cell grid for the home radius, sync WDGoWars ownership, refresh WiGLE density."""
    from warroute.coverage.sync import refresh

    run_migrations()
    summary = asyncio.run(
        refresh(home_lat=home_lat, home_lon=home_lon, radius_km=radius_km)
    )
    console.print(f"Cells in radius:     {summary.cells_total}")
    console.print(f"  newly painted:     {summary.cells_inserted}")
    console.print(f"  density refreshed: {summary.cells_density_refreshed}")
    console.print(f"  density failed:    {summary.cells_density_failed}")
    if summary.wdgowars_synced:
        console.print(f"WDGoWars: [green]synced[/green]  owned-by-me cells: {summary.cells_owned_by_me}")
    else:
        console.print(f"WDGoWars: [yellow]skipped[/yellow]  ({summary.wdgowars_error})")


@coverage_app.command("report")
def coverage_report(
    home_lat: float = typer.Option(None, help="Override HOME_LAT"),
    home_lon: float = typer.Option(None, help="Override HOME_LON"),
    radius_km: float = typer.Option(None, help="Override HOME_RADIUS_KM"),
    top: int = typer.Option(5, help="How many top unexplored cells to list"),
) -> None:
    """Print a text summary of coverage state. Run `refresh` first."""
    from warroute.coverage.report import build_summary, format_summary

    settings = get_settings()
    home_lat = home_lat if home_lat is not None else settings.home_lat
    home_lon = home_lon if home_lon is not None else settings.home_lon
    radius_km = radius_km if radius_km is not None else settings.home_radius_km
    run_migrations()
    summary = build_summary(home_lat, home_lon, radius_km, top_n=top)
    console.print(format_summary(summary))


@app.command()
def upload(
    csv_file: str = typer.Argument(..., help="Path to a WigleWifi-1.6 CSV"),
    source: str = typer.Option("wigle-android", help="Producer label for the sessions row"),
) -> None:
    """One-shot ingest: parse, dual-upload to WiGLE + WDGoWars, record session."""
    from pathlib import Path

    from warroute.uploader.orchestrator import ingest

    path = Path(csv_file)
    if not path.exists():
        console.print(f"[red]No such file: {path}[/red]")
        raise typer.Exit(code=1)

    run_migrations()
    result = asyncio.run(ingest(path, source=source))

    if result.already_seen:
        console.print(
            f"[yellow]Already ingested[/yellow] (session {result.session_id}). "
            f"sha256={result.csv_sha256[:12]}"
        )
        return

    wigle_label = "ok" if hasattr(result.wigle, "success") else result.wigle
    wdg_label = "ok" if hasattr(result.wdgowars, "success") else result.wdgowars
    console.print(
        f"[green]Session {result.session_id}[/green]: "
        f"{result.total_aps} APs ({result.new_aps} new). "
        f"WiGLE: {wigle_label}. WDGoWars: {wdg_label}."
    )


@app.command()
def watch(
    spool_dir: str = typer.Option(None, help="Override SPOOL_DIR from .env"),
    source: str = typer.Option("wigle-android", help="Producer label for ingested CSVs"),
) -> None:
    """Daemon: watch SPOOL_DIR for new CSVs and ingest them as they arrive."""
    from pathlib import Path

    from warroute.uploader.watcher import watch as run_watcher

    run_migrations()
    target = Path(spool_dir) if spool_dir else None
    console.print("[green]Watching for CSVs[/green]. Ctrl-C to stop.")
    run_watcher(spool_dir=target, source=source)


@coverage_app.command("probe-wdgowars")
def probe_wdgowars(
    path: str = typer.Argument("/api/me", help="WDGoWars API path to GET"),
) -> None:
    """Hit a WDGoWars endpoint with the real token and dump the JSON response.

    Use this to discover the shape of undocumented endpoints. See DECISIONS.md.
    """
    from warroute.clients.wdgowars import WdgowarsClient, WdgowarsError

    async def _run() -> dict:  # type: ignore[type-arg]
        async with WdgowarsClient() as wdg:
            return await wdg.probe(path)

    try:
        body = asyncio.run(_run())
    except WdgowarsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(json.dumps(body, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
