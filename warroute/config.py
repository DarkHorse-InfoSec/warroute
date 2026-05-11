"""Configuration loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Reads .env in the project root."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # WiGLE.net
    wigle_name: str = Field(default="", description="WiGLE API name (AID...)")
    wigle_token: str = Field(default="", description="WiGLE API token")

    # WDGoWars
    wdgowars_name: str = Field(default="", description="WDGoWars username")
    wdgowars_token: str = Field(default="", description="WDGoWars API token")

    # Routing
    ors_api_key: str = Field(default="", description="OpenRouteService API key")
    mapbox_api_key: str = Field(default="", description="Mapbox fallback")

    # Notifications (optional)
    ntfy_topic: str = Field(default="")

    # Deployment
    hetzner_ip_addr: str = Field(default="")

    # Home defaults
    home_lat: float = Field(default=44.9367)
    home_lon: float = Field(default=-72.2051)
    home_radius_km: float = Field(default=50.0)
    default_duration_min: int = Field(default=90)

    # Paths
    database_url: str = Field(default="sqlite:///warroute.db")
    spool_dir: Path = Field(default=Path("./spool/in"))
    gpx_out_dir: Path = Field(default=Path("./gpx-out"))

    @property
    def sqlite_path(self) -> Path:
        """Extract the filesystem path from the sqlite:// URL."""
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError(f"Only sqlite:/// URLs supported in v1: {self.database_url}")
        return Path(self.database_url.removeprefix("sqlite:///"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Avoids re-parsing .env on every call."""
    return Settings()
