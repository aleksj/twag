"""Build a GeoJSON FeatureCollection from a city's events + venues cache.

Joins events/*.md (frontmatter) with venues.json (lat/lon from
geocode.py), filters out events without coordinates and cancelled
events, and writes a single events.geojson under the dataset dir.
The static map page fetches that file directly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .city import CityConfig, active_city
from .nytw import NytwDataset, parse_event_file


def _venues_path(city: CityConfig) -> Path:
    return Path(city.dataset_path) / "venues.json"


def _geojson_path(city: CityConfig) -> Path:
    return Path("docs") / f"{city.slug}.geojson"


def _load_venues(city: CityConfig) -> dict[str, dict[str, Any]]:
    path = _venues_path(city)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {path}. Run `twag geocode-venues` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _format_iso_date(value: Any) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def build_geojson(city: CityConfig | None = None) -> dict[str, Any]:
    city = city or active_city()
    dataset = NytwDataset.from_path(city.dataset_path)
    dataset.validate()
    venues = _load_venues(city)

    features: list[dict[str, Any]] = []
    counts = {"total": 0, "mapped": 0, "no_coords": 0, "canceled": 0, "stub": 0}

    for path in sorted(dataset.events_dir.glob("*.md")):
        event = parse_event_file(path, dataset.source_dir)
        counts["total"] += 1
        if event.get("canceled"):
            counts["canceled"] += 1
            continue
        if event.get("fetch_status") != "ok":
            counts["stub"] += 1
            continue

        venue = venues.get(event["event_id"])
        if not venue or venue.get("lat") is None or venue.get("lon") is None:
            counts["no_coords"] += 1
            continue

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [venue["lon"], venue["lat"]],
                },
                "properties": {
                    "event_id": event["event_id"],
                    "title": event.get("title") or "",
                    "event_date": _format_iso_date(event.get("event_date")),
                    "start_time": event.get("start_time") or "",
                    "end_time": event.get("end_time") or "",
                    "host": event.get("host") or "",
                    "neighborhood": event.get("neighborhood") or "",
                    "venue_name": event.get("venue_name") or "",
                    "venue_address": event.get("venue_address") or "",
                    "rsvp_url": (
                        event.get("rsvp_url")
                        or event.get("public_short_url")
                        or ""
                    ),
                    "at_capacity": bool(event.get("at_capacity")),
                    "confidence": int(venue.get("confidence") or 0),
                },
            }
        )
        counts["mapped"] += 1

    collection = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "city": city.slug,
            "display_name": city.display_name,
            "counts": counts,
        },
    }

    out_path = _geojson_path(city)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(collection, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "city": city.slug,
        "geojson_path": str(out_path),
        "counts": counts,
    }
