"""WigleWifi-1.6 CSV parser.

The format (per https://wiki.wigle.net/index.php/File_format_for_uploads):
  Line 1: `WigleWifi-1.6,appRelease=...,model=...,...` -- producer metadata
  Line 2: column header: MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,
          CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
  Line 3+: data rows

We extract:
  - producer metadata (preserved as-is for upload)
  - parsed observation rows
  - dedup-by-BSSID key (one row per AP, keep best RSSI)
  - session start/end timestamps from FirstSeen min/max
  - SHA256 of the file bytes (for idempotent dedup against the sessions table)
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WIGLEWIFI_HEADER_PREFIX = "WigleWifi-1.6"
EXPECTED_COLUMNS = (
    "MAC",
    "SSID",
    "AuthMode",
    "FirstSeen",
    "Channel",
    "RSSI",
    "CurrentLatitude",
    "CurrentLongitude",
    "AltitudeMeters",
    "AccuracyMeters",
    "Type",
)


class CsvParseError(ValueError):
    """The file is missing the WigleWifi-1.6 header or is malformed."""


@dataclass(frozen=True)
class Observation:
    bssid: str
    ssid: str
    auth_mode: str
    first_seen: datetime
    channel: int | None
    rssi: int | None
    lat: float
    lon: float
    altitude_m: float | None
    accuracy_m: float | None
    type: str  # 'WIFI' or 'BT' (we only care about WIFI for v1)


@dataclass
class ParseResult:
    csv_path: Path
    csv_sha256: str
    producer_header: str
    observations: list[Observation]
    started_at: datetime
    ended_at: datetime

    @property
    def total_aps(self) -> int:
        return len(self.observations)


def parse(path: Path) -> ParseResult:
    """Parse a WigleWifi-1.6 CSV. Raises CsvParseError on a bad header."""
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines or not lines[0].startswith(WIGLEWIFI_HEADER_PREFIX):
        raise CsvParseError(f"{path}: missing 'WigleWifi-1.6' header")
    if len(lines) < 2:
        raise CsvParseError(f"{path}: file has no column-header line")

    producer_header = lines[0]
    csv_body = "\n".join(lines[1:])  # column header + data rows
    reader = csv.DictReader(io.StringIO(csv_body))
    if reader.fieldnames is None:
        raise CsvParseError(f"{path}: column header line empty")

    missing = [c for c in EXPECTED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise CsvParseError(f"{path}: missing expected columns: {missing}")

    by_bssid: dict[str, Observation] = {}
    all_timestamps: list[datetime] = []
    for row_number, row in enumerate(reader, start=3):
        try:
            obs = _row_to_observation(row)
        except (KeyError, ValueError) as exc:
            raise CsvParseError(f"{path}:{row_number}: {exc}") from exc
        if obs is None or obs.type.upper() != "WIFI":
            continue
        all_timestamps.append(obs.first_seen)
        existing = by_bssid.get(obs.bssid)
        if existing is None or _better_signal(obs, existing):
            by_bssid[obs.bssid] = obs

    observations = list(by_bssid.values())
    if not observations:
        raise CsvParseError(f"{path}: no WiFi observations found")

    return ParseResult(
        csv_path=path,
        csv_sha256=sha256,
        producer_header=producer_header,
        observations=observations,
        started_at=min(all_timestamps),
        ended_at=max(all_timestamps),
    )


def _row_to_observation(row: dict[str, str]) -> Observation | None:
    bssid = (row.get("MAC") or "").strip().upper()
    if not bssid:
        return None
    return Observation(
        bssid=bssid,
        ssid=(row.get("SSID") or "").strip(),
        auth_mode=(row.get("AuthMode") or "").strip(),
        first_seen=_parse_timestamp(row["FirstSeen"]),
        channel=_int_or_none(row.get("Channel")),
        rssi=_int_or_none(row.get("RSSI")),
        lat=float(row["CurrentLatitude"]),
        lon=float(row["CurrentLongitude"]),
        altitude_m=_float_or_none(row.get("AltitudeMeters")),
        accuracy_m=_float_or_none(row.get("AccuracyMeters")),
        type=(row.get("Type") or "WIFI").strip(),
    )


def _better_signal(a: Observation, b: Observation) -> bool:
    """RSSI is negative dBm; closer to 0 is stronger."""
    if a.rssi is None:
        return False
    if b.rssi is None:
        return True
    return a.rssi > b.rssi


def _parse_timestamp(value: str) -> datetime:
    """WiGLE timestamps look like '2026-05-10 18:42:31'. Be tolerant of T separators."""
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable FirstSeen: {value!r}")


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
