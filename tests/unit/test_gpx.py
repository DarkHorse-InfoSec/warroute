"""Tests for the GPX writer + Google Maps URL builder."""

from __future__ import annotations

import urllib.parse
from xml.etree import ElementTree as ET

import pytest

from warroute.clients.ors import Waypoint
from warroute.router.gpx import google_maps_url, write_gpx


def test_write_gpx_emits_valid_xml_with_waypoints() -> None:
    wps = [
        Waypoint(44.94, -72.21, label="Home"),
        Waypoint(44.96, -72.18, label="Cell A"),
        Waypoint(44.94, -72.21, label="Home"),
    ]
    xml = write_gpx(wps, name="test")
    root = ET.fromstring(xml)
    assert root.tag.endswith("gpx")
    wpt_elements = [el for el in root.iter() if el.tag.endswith("wpt")]
    assert len(wpt_elements) == 3
    assert wpt_elements[0].get("lat") == "44.940000"
    assert wpt_elements[0].get("lon") == "-72.210000"


def test_write_gpx_with_track_points() -> None:
    wps = [Waypoint(44.94, -72.21), Waypoint(44.96, -72.18)]
    track = [Waypoint(44.940 + i * 0.001, -72.210 + i * 0.001) for i in range(5)]
    xml = write_gpx(wps, track_points=track)
    root = ET.fromstring(xml)
    trkpt_elements = [el for el in root.iter() if el.tag.endswith("trkpt")]
    assert len(trkpt_elements) == 5


def test_google_maps_url_origin_and_destination() -> None:
    url = google_maps_url([Waypoint(44.94, -72.21), Waypoint(45.00, -72.00)])
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["origin"] == ["44.94,-72.21"]
    assert qs["destination"] == ["45.0,-72.0"]
    assert qs["travelmode"] == ["driving"]
    assert "waypoints" not in qs


def test_google_maps_url_with_intermediates() -> None:
    wps = [
        Waypoint(44.94, -72.21),
        Waypoint(44.96, -72.18),
        Waypoint(44.95, -72.19),
        Waypoint(45.00, -72.00),
    ]
    url = google_maps_url(wps)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["waypoints"] == ["44.96,-72.18|44.95,-72.19"]


def test_google_maps_url_caps_intermediates_at_9() -> None:
    home = Waypoint(44.94, -72.21)
    end = Waypoint(45.00, -72.00)
    middles = [Waypoint(44.94 + i * 0.001, -72.20 + i * 0.001) for i in range(15)]
    url = google_maps_url([home, *middles, end])
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert len(qs["waypoints"][0].split("|")) == 9


def test_google_maps_url_requires_two_points() -> None:
    with pytest.raises(ValueError):
        google_maps_url([Waypoint(44.94, -72.21)])
