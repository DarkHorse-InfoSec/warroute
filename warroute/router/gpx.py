"""GPX 1.1 writer + Google Maps multi-stop deep-link.

GPX is consumed by phone navigation apps (OSMAnd, Google Maps GPX import,
GuidiGo, etc.). The Google Maps URL is the simplest "tap to navigate" option
on Android; it caps at 9 waypoints + destination.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from warroute.clients.ors import Waypoint

if TYPE_CHECKING:
    from warroute.router.planner import DaySegment

GPX_NS = "http://www.topografix.com/GPX/1/1"
ET.register_namespace("", GPX_NS)


def write_gpx(
    waypoints: list[Waypoint],
    track_points: list[Waypoint] | None = None,
    name: str = "WarRoute",
    description: str = "",
) -> str:
    """Render a GPX 1.1 document as a string.

    `waypoints` are POIs (start, stops, end). `track_points` are the actual
    polyline returned by ORS directions. If track_points is None, only the
    waypoint list is emitted.
    """
    gpx = ET.Element(
        f"{{{GPX_NS}}}gpx",
        attrib={
            "version": "1.1",
            "creator": "warroute",
        },
    )
    metadata = ET.SubElement(gpx, f"{{{GPX_NS}}}metadata")
    ET.SubElement(metadata, f"{{{GPX_NS}}}name").text = name
    if description:
        ET.SubElement(metadata, f"{{{GPX_NS}}}desc").text = description
    ET.SubElement(metadata, f"{{{GPX_NS}}}time").text = datetime.now(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    for wp in waypoints:
        wpt = ET.SubElement(
            gpx,
            f"{{{GPX_NS}}}wpt",
            attrib={"lat": f"{wp.lat:.6f}", "lon": f"{wp.lon:.6f}"},
        )
        if wp.label:
            ET.SubElement(wpt, f"{{{GPX_NS}}}name").text = wp.label

    if track_points:
        trk = ET.SubElement(gpx, f"{{{GPX_NS}}}trk")
        ET.SubElement(trk, f"{{{GPX_NS}}}name").text = name
        seg = ET.SubElement(trk, f"{{{GPX_NS}}}trkseg")
        for pt in track_points:
            ET.SubElement(
                seg,
                f"{{{GPX_NS}}}trkpt",
                attrib={"lat": f"{pt.lat:.6f}", "lon": f"{pt.lon:.6f}"},
            )

    return ET.tostring(gpx, encoding="unicode", xml_declaration=True)


def write_gpx_per_day(
    ordered_waypoints: list[Waypoint],
    days: list[DaySegment],
    name_prefix: str = "WarRoute",
) -> dict[int, str]:
    """Phase 6c.2: render one GPX per DaySegment.

    Returns {day_number: gpx_xml}. Phone navigation apps expect one GPX per trip,
    so a 3-day roadtrip ships as three files instead of one combined polyline.
    Empty input returns {}.
    """
    if not days:
        return {}
    per_day: dict[int, str] = {}
    for day in days:
        # end_idx is inclusive in DaySegment; slice up to end_idx + 1.
        chunk = ordered_waypoints[day.start_idx : day.end_idx + 1]
        if len(chunk) < 2:
            continue
        per_day[day.day_number] = write_gpx(
            chunk,
            track_points=None,
            name=f"{name_prefix} Day {day.day_number}",
            description=(
                f"Day {day.day_number}: {day.drive_min:.0f} min drive"
                f"{f' + {day.dwell_min} min dwell' if day.dwell_min else ''}"
            ),
        )
    return per_day


def google_maps_url(waypoints: list[Waypoint]) -> str:
    """Build a multi-stop Google Maps directions URL.

    Format: https://www.google.com/maps/dir/?api=1&origin=LAT,LON&destination=LAT,LON&waypoints=LAT,LON|LAT,LON
    Google Maps caps at 9 intermediate waypoints + origin + destination.
    """
    if len(waypoints) < 2:
        raise ValueError("google_maps_url needs at least origin and destination")
    origin = waypoints[0]
    destination = waypoints[-1]
    intermediates = waypoints[1:-1][:9]  # cap at 9 intermediates

    params = {
        "api": "1",
        "origin": f"{origin.lat},{origin.lon}",
        "destination": f"{destination.lat},{destination.lon}",
        "travelmode": "driving",
    }
    if intermediates:
        params["waypoints"] = "|".join(f"{w.lat},{w.lon}" for w in intermediates)
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params)
