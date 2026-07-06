"""Tests for the WigleWifi-1.6 CSV parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from warroute.uploader.parser import CsvParseError, parse

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_wiglewifi.csv"


def test_parse_fixture_returns_unique_wifi_observations() -> None:
    result = parse(FIXTURE)
    bssids = {o.bssid for o in result.observations}
    # 5 unique WiFi MACs in fixture; the BT row is excluded; the duplicate AA:BB:CC:00:00:01 collapses.
    assert bssids == {
        "AA:BB:CC:00:00:01",
        "AA:BB:CC:00:00:02",
        "AA:BB:CC:00:00:03",
        "AA:BB:CC:00:00:04",
        "AA:BB:CC:00:00:05",
    }


def test_parse_dedup_keeps_strongest_signal() -> None:
    result = parse(FIXTURE)
    home = next(o for o in result.observations if o.bssid == "AA:BB:CC:00:00:01")
    # The fixture has -45 (first) and -43 (second). -43 is stronger, should win.
    assert home.rssi == -43


def test_parse_extracts_session_window() -> None:
    result = parse(FIXTURE)
    assert result.started_at.isoformat() == "2026-05-10T18:42:01"
    assert result.ended_at.isoformat() == "2026-05-10T18:46:42"


def test_parse_sha256_is_hex_64() -> None:
    result = parse(FIXTURE)
    assert len(result.csv_sha256) == 64
    assert all(c in "0123456789abcdef" for c in result.csv_sha256)


def test_parse_sha256_is_stable() -> None:
    a = parse(FIXTURE)
    b = parse(FIXTURE)
    assert a.csv_sha256 == b.csv_sha256


def test_parse_rejects_missing_header(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("not a wiglewifi file\nMAC,SSID,...\n", encoding="utf-8")
    with pytest.raises(CsvParseError, match=r"WigleWifi-1\.6"):
        parse(bad)


def test_parse_rejects_missing_columns(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "WigleWifi-1.6,appRelease=x\nMAC,SSID\nAA,SomeNet\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvParseError, match="missing expected columns"):
        parse(bad)


def test_parse_rejects_empty_data(tmp_path: Path) -> None:
    bad = tmp_path / "empty.csv"
    bad.write_text(
        "WigleWifi-1.6,appRelease=x\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n",
        encoding="utf-8",
    )
    with pytest.raises(CsvParseError, match="no WiFi observations"):
        parse(bad)


def test_parse_handles_utf8_bom(tmp_path: Path) -> None:
    """Some Android exports include a BOM."""
    fixture_bytes = FIXTURE.read_bytes()
    with_bom = b"\xef\xbb\xbf" + fixture_bytes
    bom_path = tmp_path / "bom.csv"
    bom_path.write_bytes(with_bom)
    result = parse(bom_path)
    assert result.total_aps > 0
