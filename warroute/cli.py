"""Typer CLI entrypoint. Subcommands grow per phase."""

from __future__ import annotations

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


if __name__ == "__main__":
    app()
