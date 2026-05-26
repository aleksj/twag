from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CityConfig:
    slug: str
    display_name: str
    short_name: str
    calendar_url: str
    table_prefix: str
    dataset_path: str
    agent_name: str
    tool_name: str
    event_date_range: str
    time_zone: str
    vibe_line: str
    example_neighborhood: str
    example_date: str
    neighborhoods_regex: str
    # Map view config
    map_center_lat: float
    map_center_lon: float
    map_zoom: float
    map_html_filename: str
    default_map_date: str


NYC = CityConfig(
    slug="nyc",
    display_name="NY Tech Week 2026",
    short_name="NY Tech Week",
    calendar_url="https://www.tech-week.com/calendar/nyc",
    table_prefix="nytw",
    dataset_path="data/nytw-2026-for-agents",
    agent_name="NYTechWeek ClickHouse Agent",
    tool_name="query_nytw_clickhouse",
    event_date_range="June 1-7, 2026",
    time_zone="America/New_York",
    vibe_line=(
        "C'mon, this is NYC, not a vibes committee. I'm not here to crown the "
        "'best' event or tell you what to do with your afternoon. Give me criteria: "
        "topic keywords, date, neighborhood, host, capacity, RSVP status, or time. "
        "Try: 'List AI events in SoHo on June 3' or 'Show cybersecurity events with open RSVPs.'"
    ),
    example_neighborhood="SoHo",
    example_date="June 3",
    neighborhoods_regex=(
        r"soho|tribeca|brooklyn|manhattan|williamsburg|"
        r"upper\s+west\s+side|uws|upper\s+east\s+side|ues|"
        r"chelsea|flatiron|midtown|downtown|chinatown|"
        r"east\s+village|west\s+village|lower\s+east\s+side"
    ),
    map_center_lat=40.7549,
    map_center_lon=-73.9840,
    map_zoom=11.5,
    map_html_filename="events_map_nyc.html",
    default_map_date="2026-06-02",
)


BOSTON = CityConfig(
    slug="boston",
    display_name="Boston Tech Week 2026",
    short_name="Boston Tech Week",
    calendar_url="https://www.tech-week.com/calendar/boston",
    table_prefix="bostw",
    dataset_path="data/bostontw-2026-for-agents",
    agent_name="BostonTechWeek ClickHouse Agent",
    tool_name="query_bostw_clickhouse",
    event_date_range="May 24-31, 2026",
    time_zone="America/New_York",
    vibe_line=(
        "This is Boston — give me wicked specifics. I'm not here to crown the "
        "'best' event or tell you what to do with your afternoon. Give me criteria: "
        "topic keywords, date, neighborhood, host, capacity, RSVP status, or time. "
        "Try: 'List AI events in Cambridge on May 26' or 'Show cybersecurity events with open RSVPs.'"
    ),
    example_neighborhood="Cambridge",
    example_date="May 26",
    neighborhoods_regex=(
        r"cambridge|kendall|kendall\s+square|harvard\s+square|"
        r"back\s+bay|south\s+end|seaport|fenway|allston|brighton|"
        r"somerville|davis\s+square|union\s+square|"
        r"downtown|north\s+end|beacon\s+hill|financial\s+district|"
        r"east\s+boston|south\s+boston|jamaica\s+plain"
    ),
    map_center_lat=42.3601,
    map_center_lon=-71.0942,
    map_zoom=12.0,
    map_html_filename="events_map_boston.html",
    default_map_date="2026-05-26",
)


CITIES: dict[str, CityConfig] = {
    NYC.slug: NYC,
    BOSTON.slug: BOSTON,
}


DEFAULT_CITY_SLUG = "nyc"


def load_city(slug: str | None) -> CityConfig:
    key = (slug or DEFAULT_CITY_SLUG).strip().lower()
    if key not in CITIES:
        known = ", ".join(sorted(CITIES.keys()))
        raise ValueError(f"Unknown TWAG_CITY {key!r}. Known cities: {known}")
    return CITIES[key]


def active_city() -> CityConfig:
    return load_city(os.getenv("TWAG_CITY", DEFAULT_CITY_SLUG))
