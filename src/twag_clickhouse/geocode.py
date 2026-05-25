"""OpenCage venue geocoder for Tech Week event datasets.

Reads every events/*.md in the active city's dataset, extracts
venue_address, calls the OpenCage Geocoding API at 1 req/sec (free
tier policy), and writes the lat/lon results to
data/<city>-for-agents/venues.json. Idempotent: re-runs only geocode
addresses missing from the cache (unless --refresh).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .city import CityConfig, active_city
from .nytw import NytwDataset, parse_event_file


OPENCAGE_ENDPOINT = "https://api.opencagedata.com/geocode/v1/json"
RATE_LIMIT_DELAY_SECONDS = 1.05  # safety over the 1 req/sec free-tier limit
USER_AGENT = "twag-tech-week-bot (https://github.com/natea/twag)"


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class VenueRecord:
    event_id: str
    address: str
    venue_name: str
    lat: float | None
    lon: float | None
    formatted: str
    confidence: int
    source: str  # "opencage" | "cache" | "skipped"


def _api_key() -> str:
    key = os.getenv("OPENCAGE_API_KEY", "").strip()
    if not key:
        raise GeocodeError(
            "OPENCAGE_API_KEY is required. Get a free key at https://opencagedata.com/."
        )
    return key


def _venues_path(city: CityConfig) -> Path:
    return Path(city.dataset_path) / "venues.json"


def _load_cache(city: CityConfig) -> dict[str, dict[str, Any]]:
    path = _venues_path(city)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_cache(city: CityConfig, cache: dict[str, dict[str, Any]]) -> None:
    path = _venues_path(city)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalize_address(address: str) -> str:
    # OpenCage tolerates loose formatting; we just trim and collapse whitespace.
    return re.sub(r"\s+", " ", address).strip()


def _opencage_request(address: str, api_key: str) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "q": address,
            "key": api_key,
            "no_annotations": "1",
            "limit": "1",
            "countrycode": "us",
        }
    )
    url = f"{OPENCAGE_ENDPOINT}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GeocodeError(f"OpenCage HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GeocodeError(f"OpenCage network error: {exc}") from exc


def _result_to_record(
    event_id: str,
    address: str,
    venue_name: str,
    payload: dict[str, Any],
) -> VenueRecord:
    results = payload.get("results") or []
    if not results:
        return VenueRecord(
            event_id=event_id,
            address=address,
            venue_name=venue_name,
            lat=None,
            lon=None,
            formatted="",
            confidence=0,
            source="opencage",
        )
    top = results[0]
    geometry = top.get("geometry") or {}
    return VenueRecord(
        event_id=event_id,
        address=address,
        venue_name=venue_name,
        lat=float(geometry.get("lat")) if geometry.get("lat") is not None else None,
        lon=float(geometry.get("lng")) if geometry.get("lng") is not None else None,
        formatted=str(top.get("formatted") or ""),
        confidence=int(top.get("confidence") or 0),
        source="opencage",
    )


def _record_as_dict(record: VenueRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "address": record.address,
        "venue_name": record.venue_name,
        "lat": record.lat,
        "lon": record.lon,
        "formatted": record.formatted,
        "confidence": record.confidence,
    }


def geocode_city(
    *,
    city: CityConfig | None = None,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Geocode every event venue in the active city's dataset.

    Returns counts and the path to the venues cache.
    """
    city = city or active_city()
    dataset = NytwDataset.from_path(city.dataset_path)
    dataset.validate()

    cache = {} if refresh else _load_cache(city)
    api_key = _api_key()

    counts = {"total": 0, "cached": 0, "geocoded": 0, "failed": 0, "skipped": 0}
    processed = 0

    for path in sorted(dataset.events_dir.glob("*.md")):
        if limit is not None and processed >= limit:
            break
        event = parse_event_file(path, dataset.source_dir)
        event_id = event["event_id"]
        venue_name = event.get("venue_name") or ""
        address = _normalize_address(event.get("venue_address") or "")
        counts["total"] += 1

        if not address:
            counts["skipped"] += 1
            continue

        if event_id in cache and cache[event_id].get("address") == address and cache[event_id].get("lat") is not None:
            counts["cached"] += 1
            continue

        time.sleep(RATE_LIMIT_DELAY_SECONDS)
        payload = _opencage_request(address, api_key)
        record = _result_to_record(event_id, address, venue_name, payload)
        cache[event_id] = _record_as_dict(record)

        if record.lat is None:
            counts["failed"] += 1
        else:
            counts["geocoded"] += 1
        processed += 1

        # Persist after every successful call so we never lose progress on Ctrl-C.
        _save_cache(city, cache)

    _save_cache(city, cache)
    return {
        "city": city.slug,
        "venues_path": str(_venues_path(city)),
        "counts": counts,
    }
