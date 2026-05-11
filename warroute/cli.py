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
def plan(
    duration_min: int = typer.Option(90, "--duration", "-d", help="Time budget in minutes"),
    mode: str = typer.Option("loop", "--mode", "-m", help="loop or oneway"),
    home_lat: float = typer.Option(None, help="Override HOME_LAT"),
    home_lon: float = typer.Option(None, help="Override HOME_LON"),
    destination: str = typer.Option(None, "--destination", help="LAT,LON for oneway mode"),
    out: str = typer.Option("drive.gpx", "--out", "-o", help="GPX output path"),
) -> None:
    """Plan a wardriving route from home, optimized for new-AP yield within the time budget."""
    from pathlib import Path

    from warroute.router.gpx import google_maps_url, write_gpx
    from warroute.router.planner import PlannerError, PlanRequest
    from warroute.router.planner import plan as run_plan

    settings = get_settings()
    if mode not in ("loop", "oneway"):
        console.print(f"[red]Invalid --mode '{mode}'; use 'loop' or 'oneway'.[/red]")
        raise typer.Exit(code=2)

    dest_lat: float | None = None
    dest_lon: float | None = None
    if mode == "oneway":
        if not destination:
            console.print("[red]oneway mode requires --destination LAT,LON[/red]")
            raise typer.Exit(code=2)
        try:
            dest_lat_s, dest_lon_s = destination.split(",")
            dest_lat = float(dest_lat_s)
            dest_lon = float(dest_lon_s)
        except ValueError as exc:
            console.print(f"[red]Invalid --destination: {exc}[/red]")
            raise typer.Exit(code=2) from exc

    request = PlanRequest(
        home_lat=home_lat if home_lat is not None else settings.home_lat,
        home_lon=home_lon if home_lon is not None else settings.home_lon,
        duration_min=duration_min,
        mode=mode,
        destination_lat=dest_lat,
        destination_lon=dest_lon,
    )

    run_migrations()
    try:
        result = asyncio.run(run_plan(request))
    except PlannerError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gpx_xml = write_gpx(
        result.ordered_waypoints,
        track_points=None,
        name=f"WarRoute {request.duration_min}min {request.mode}",
        description=f"{len(result.chosen_cells)} cells, ~{result.estimated_new_aps} new APs",
    )
    out_path.write_text(gpx_xml, encoding="utf-8")

    console.print(f"[green]Plan {result.planned_route_id}[/green]: "
                  f"{len(result.chosen_cells)} cells, "
                  f"~{result.estimated_new_aps} new APs, "
                  f"{result.estimated_drive_min:.1f} min, "
                  f"{result.leg.distance_km:.1f} km")
    if result.drops_for_slack:
        console.print(f"  Dropped to fit budget: {len(result.drops_for_slack)} cells")
    console.print(f"  GPX: {out_path}")
    console.print(f"  Maps: {google_maps_url(result.ordered_waypoints)}")


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
