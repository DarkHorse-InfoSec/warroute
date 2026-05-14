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
coverage_app = typer.Typer(
    name="coverage", help="Cell ownership + AP density.", no_args_is_help=True
)
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
    summary = asyncio.run(refresh(home_lat=home_lat, home_lon=home_lon, radius_km=radius_km))
    console.print(f"Cells in radius:     {summary.cells_total}")
    console.print(f"  newly painted:     {summary.cells_inserted}")
    console.print(f"  density refreshed: {summary.cells_density_refreshed}")
    console.print(f"  density failed:    {summary.cells_density_failed}")
    if summary.wdgowars_synced:
        console.print(
            f"WDGoWars: [green]synced[/green]  owned-by-me cells: {summary.cells_owned_by_me}"
        )
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


@app.command()
def plan(
    duration_min: int = typer.Option(90, "--duration", "-d", help="Time budget in minutes"),
    mode: str = typer.Option("loop", "--mode", "-m", help="loop or oneway"),
    home_lat: float = typer.Option(None, help="Override HOME_LAT"),
    home_lon: float = typer.Option(None, help="Override HOME_LON"),
    destination: str = typer.Option(None, "--destination", help="LAT,LON for oneway mode"),
    stop: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--stop",
        help="Multi-stop: LAT,LON or LAT,LON:DWELL_MIN. Repeatable. Implies oneway.",
    ),
    out: str = typer.Option("drive.gpx", "--out", "-o", help="GPX output path"),
) -> None:
    """Plan a wardriving route from home, optimized for new-AP yield within the time budget."""
    from pathlib import Path

    from warroute.router.gpx import google_maps_url, write_gpx
    from warroute.router.planner import PlannerError, PlanRequest, Stop
    from warroute.router.planner import plan as run_plan

    settings = get_settings()
    if mode not in ("loop", "oneway"):
        console.print(f"[red]Invalid --mode '{mode}'; use 'loop' or 'oneway'.[/red]")
        raise typer.Exit(code=2)

    stops: list[Stop] = []
    if stop:
        for raw in stop:
            try:
                # Format: "lat,lon" | "lat,lon:dwell" | "lat,lon:dwell:overnight"
                parts = raw.split(":")
                lat_s, lon_s = parts[0].split(",")
                dwell = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                overnight = len(parts) > 2 and parts[2].lower() == "overnight"
                stops.append(
                    Stop(
                        lat=float(lat_s),
                        lon=float(lon_s),
                        dwell_min=dwell,
                        overnight_after=overnight,
                    )
                )
            except ValueError as exc:
                console.print(f"[red]Invalid --stop {raw!r}: {exc}[/red]")
                raise typer.Exit(code=2) from exc
        mode = "oneway"  # any --stop overrides loop
    elif mode == "oneway":
        if not destination:
            console.print(
                "[red]oneway mode requires --destination LAT,LON or one or more --stop[/red]"
            )
            raise typer.Exit(code=2)
        try:
            dest_lat_s, dest_lon_s = destination.split(",")
            stops.append(Stop(lat=float(dest_lat_s), lon=float(dest_lon_s)))
        except ValueError as exc:
            console.print(f"[red]Invalid --destination: {exc}[/red]")
            raise typer.Exit(code=2) from exc

    request = PlanRequest(
        home_lat=home_lat if home_lat is not None else settings.home_lat,
        home_lon=home_lon if home_lon is not None else settings.home_lon,
        duration_min=duration_min,
        mode=mode,
        stops=stops,
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

    console.print(
        f"[green]Plan {result.planned_route_id}[/green]: "
        f"{len(result.chosen_cells)} cells, "
        f"~{result.estimated_new_aps} new APs, "
        f"{result.estimated_drive_min:.1f} min, "
        f"{result.leg.distance_km:.1f} km"
    )
    if result.drops_for_slack:
        console.print(f"  Dropped to fit budget: {len(result.drops_for_slack)} cells")
    console.print(f"  GPX: {out_path}")
    console.print(f"  Maps: {google_maps_url(result.ordered_waypoints)}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address. Use 0.0.0.0 to expose on LAN."),
    port: int = typer.Option(8000, help="Port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)"),
) -> None:
    """Start the FastAPI web UI."""
    import uvicorn

    run_migrations()
    console.print(f"[green]WarRoute serving on http://{host}:{port}[/green]")
    uvicorn.run("warroute.web.app:app", host=host, port=port, reload=reload, log_level="info")


@app.command()
def precheck() -> None:
    """Pre-drive sanity check: verify external APIs + filesystem are ready.

    Hits WIGLE, WDGoWars, and ORS with auth-check calls (concurrently) plus
    writability checks on SPOOL_DIR and GPX_OUT_DIR. Reports per-check status
    and an overall verdict. Exits 0 on PASS, 1 on WARN, 2 on FAIL so you can
    chain it into shell scripts or a pre-flight phone shortcut.

    Do NOT run from a network with TLS interception (e.g. school PC on NCSUVT
    Fortinet network); tokens transit through the inspection device. See
    DECISIONS.md 2026-05-11 entry.
    """
    from warroute.precheck import Status, run_all, verdict

    results = asyncio.run(run_all())
    for r in results:
        color = {
            Status.OK: "green",
            Status.WARN: "yellow",
            Status.FAIL: "red",
        }[r.status]
        label = f"[{color}]{r.status.value.upper():4}[/{color}]"
        console.print(f"{label}  {r.name:12} {r.detail}")
        if r.hint:
            console.print(f"        [dim]hint:[/dim] {r.hint}")
    overall = verdict(results)
    color = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red"}[overall]
    console.print(f"\n[{color}]Overall: {overall.value.upper()}[/{color}]")
    if overall == Status.FAIL:
        raise typer.Exit(code=2)
    if overall == Status.WARN:
        raise typer.Exit(code=1)


@app.command("notify-due")
def notify_due_cmd(
    lead_min: int = typer.Option(
        None,
        "--lead-min",
        help="Minutes ahead of departure to fire. Defaults to NTFY_DEPARTURE_LEAD_MIN.",
    ),
) -> None:
    """Phase 6b.2: scan scheduled_departures, fire ntfy push for any due rows.

    Idempotent and fast (single SQLite query + best-effort ntfy POSTs). Intended
    to run from a systemd timer once per minute. Marks notified_at as the dedup
    key, so a re-run within the same window is a no-op.
    """
    from warroute.scheduler import notify_due

    run_migrations()
    count = asyncio.run(notify_due(lead_min=lead_min))
    console.print(f"[green]Notified {count} departure(s).[/green]")


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
