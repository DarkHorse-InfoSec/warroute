"""Configuration loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Neutral map center for the stateless web tier (contiguous-US centroid). Web pages
# seat their Leaflet map here for an anonymous request; when data exists the map
# fits to it (coverage cells, plan route), and each user's own home comes from their
# browser. Deliberately NOT the operator's real home, so the public tier never
# renders anyone's home location. See DECISIONS.md 2026-07-05 (security-pass).
PUBLIC_MAP_DEFAULT_LAT = 39.8283
PUBLIC_MAP_DEFAULT_LON = -98.5795


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
    wdgowars_expected_version: str = Field(
        default="1.3.0",
        description="Pinned WDGoWars server version; precheck warns when the live"
        " /api/stats.version differs (may expose new endpoints worth re-probing)",
    )

    # Routing
    ors_api_key: str = Field(default="", description="OpenRouteService API key")
    mapbox_api_key: str = Field(default="", description="Mapbox fallback")
    # Stateless-tier shared-ORS carve-out guards (DECISIONS.md 2026-07-04). The
    # shared ORS key backs users who have no ORS key of their own; these bound the
    # operator's exposure. Free tier is 2000 ORS ops/day, so keep headroom.
    ors_shared_daily_cap: int = Field(
        default=600,
        description="Max shared-ORS user-actions per UTC day before web users must bring their own"
        " key. Conservative: each action may be 1-3 ORS ops, and the free tier is 2000 ops/day; the"
        " ORS 429 is the hard backstop, this is the soft pre-emptive limit",
    )
    ors_shared_rate_per_min: int = Field(
        default=8,
        description="Max shared-ORS ROUTING operations per minute per client IP",
    )
    ors_shared_geocode_rate_per_min: int = Field(
        default=40,
        description="Max shared-ORS GEOCODE (address search) requests per minute per client"
        " IP. Higher than routing because it's per-keystroke type-ahead; separate window;"
        " not counted against the routing daily cap (ORS geocoding is a separate quota)",
    )
    ors_shared_geocode_daily_cap: int = Field(
        default=1000,
        description="Max shared-ORS GEOCODE user-actions per UTC day before web users must bring"
        " their own key. ORS geocoding free tier is ~1000/day; this is the global backstop that"
        " the per-IP/min limit alone does not provide (mirrors ors_shared_daily_cap for routing)",
    )

    # Live per-user cell enrichment at plan time (DECISIONS.md 2026-07-05 enrich).
    # WiGLE is ~1 req/sec, so bound how many cells a single plan will density-query
    # live (nearest-first) and the total wall-clock spent doing it.
    live_density_cell_cap: int = Field(
        default=8,
        description="Max cells a single plan queries WiGLE for live density (nearest first)",
    )
    live_density_budget_s: float = Field(
        default=20.0,
        description="Wall-clock seconds cap on live WiGLE density queries per plan",
    )

    # Opt-in E2E config sync (DECISIONS.md 2026-07-04 sync entry). Bounds the sync
    # endpoint's storage/abuse surface. Config blobs are tiny (a few keys), so the
    # size cap is generous but firm.
    sync_max_bytes: int = Field(
        default=16384,
        description="Max ciphertext size (bytes) accepted by the /sync endpoint",
    )
    sync_rate_per_min: int = Field(
        default=20,
        description="Max /sync writes+reads per minute per client IP",
    )
    sync_max_rows: int = Field(
        default=10000,
        description="Global cap on stored sync blobs. When exceeded, the oldest (by updated_at)"
        " are evicted LRU-style, bounding total storage even under distributed abuse",
    )

    # Notifications (optional ntfy.sh push notifications)
    ntfy_topic: str = Field(
        default="", description="ntfy.sh topic name; empty disables notifications"
    )
    ntfy_base_url: str = Field(
        default="https://ntfy.sh", description="ntfy server base; override for self-hosted"
    )
    ntfy_auth_token: str = Field(
        default="", description="Bearer token for private ntfy servers; empty for public ntfy.sh"
    )
    ntfy_notify_run: bool = Field(
        default=True,
        description="Send a push when a CSV upload completes (Phase 5 v1 behavior).",
    )
    # Future toggles -- wired through settings but not yet emitting.
    # See DECISIONS.md 2026-05-11 ntfy.sh notifications entry.
    ntfy_notify_plan: bool = Field(
        default=False,
        description="(Future) Send a push when a route plan completes and GPX is ready.",
    )
    ntfy_notify_quota: bool = Field(
        default=False,
        description="(Future) Send a push when WDGoWars or ORS daily quota drops below 10%.",
    )
    ntfy_departure_lead_min: int = Field(
        default=5,
        description=(
            "Phase 6b.2: fire the 'time to leave' alarm this many minutes before"
            " a planned departure. Set to 0 to fire exactly at the departure time."
        ),
    )

    # Web (for click-through URLs in notifications and external links)
    web_base_url: str = Field(
        default="",
        description="Public WarRoute URL (e.g. https://warroute.darkhorseinfosec.com). Used to build ntfy click links.",
    )

    # Deployment
    hetzner_ip_addr: str = Field(default="")

    # Multi-tester access control (Phase tester-2). Comma-separated list of
    # usernames (X-Forwarded-User header values) treated as admin. Admins see
    # server-internal config on /settings (paths, fingerprints, .env table); a
    # plain tester sees only their own home + API credentials editor. Empty
    # list disables admin features.
    admin_users: str = Field(
        default="",
        description="Comma-separated usernames with admin access on /settings.",
    )

    @property
    def admin_user_set(self) -> set[str]:
        """Normalized lowercase set of admin usernames."""
        return {u.strip().lower() for u in self.admin_users.split(",") if u.strip()}

    # Home defaults. In the stateless model each user's real home lives in their
    # BROWSER (localStorage); the server never needs it for web rendering. These
    # fields are used ONLY by operator-side CLI coverage-grid painting. The default
    # is a neutral point (contiguous-US centroid), NOT a real address, so a fresh
    # checkout or the public web tier never discloses anyone's home. An operator who
    # runs `coverage refresh` sets their real home in their private .env.
    home_lat: float = Field(default=39.8283)
    home_lon: float = Field(default=-98.5795)
    home_radius_km: float = Field(default=50.0)
    # Run review (post-drive AP map) exposes exact scanned-AP coordinates, including
    # your home network's location. OFF by default so the public/stateless tier never
    # leaks it. Enable ONLY on a trusted deployment gated by auth (self-host, or the
    # basic_auth / Cloudflare Access Caddyfile), never with Caddyfile.public.
    expose_run_data: bool = Field(
        default=False,
        description="If true, /runs and /runs/{id}/observations.geojson serve scanned-AP"
        " coordinates. Leave false on any public, unauthenticated deployment.",
    )
    default_duration_min: int = Field(
        default=30, description="Default time budget (min). Short = quick trip + small detour."
    )

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
