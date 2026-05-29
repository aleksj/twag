# TWAG Tech Week Guide

TWAG is a data-backed event guide for NY Tech Week and Boston Tech
Week 2026. Ask it practical questions about events, hosts, neighborhoods,
topics, times, RSVP status, and capacity; or browse the public maps and
galleries.

No setup is needed to try it. Use the browser terminal or open the Telegram bot
for the city you care about.

Sponsored by [Data.Flowers](https://data.flowers/).

## Credits

- **[Atin Woodard from Stage11](https://github.com/Stage-11-Agentics/)** started the agent-friendly NY Tech Week database.
- **[Aleks Jakulin from Data.Flowers](https://github.com/aleksj)** started on the end-user product, wiring ClickHouse, Telegram, deployment, and production hardening pieces together.
- **[Nate Aune](https://github.com/natea)** ported the experience to Boston, rebuilt the static map/gallery navigation, and pushed the multi-city direction.

## Try It

|  | NY Tech Week | Boston Tech Week |
| --- | --- | --- |
| Bot: terminal | [Open terminal](https://data.flowers/tw/?city=nyc) | [Open terminal](https://data.flowers/tw/?city=boston) |
| Bot: Telegram | [@Twagbot](https://t.me/Twagbot) | [@TwagBostonBot](https://t.me/TwagBostonBot) |
| Bot: Telegram QR | <img src="docs/assets/twagbot-qr.png" alt="QR code for @Twagbot" width="140"> | <img src="docs/assets/twagbostonbot-qr.png" alt="QR code for @TwagBostonBot" width="140"> |
| Gallery | [Browse gallery](https://natea.github.io/twag/events_gallery_nyc.html) | [Browse gallery](https://natea.github.io/twag/events_gallery_boston.html) |
| Map | [Open map](https://natea.github.io/twag/events_map_nyc.html) | [Open map](https://natea.github.io/twag/events_map_boston.html) |

The browser terminal is the web alternative to Telegram. It supports `/city nyc`,
`/city boston`, `/map`, `/help`, and follow-up requests like `more`.

## How It Works

The repo keeps two Tech Week datasets in a shared shape: event markdown, manifests, hosts, users, venues, and generated static assets. `TWAG_CITY` selects which city is active. NYC uses `data/nytw-2026-for-agents` and ClickHouse tables prefixed with `nytw_*`; Boston uses `data/bostontw-2026-for-agents` and `bostw_*`.

The data pipeline normalizes event files, geocodes venues with OpenCage, exports GeoJSON for the map, downloads event images once, and creates committed thumbnails for the gallery. Full-resolution images stay ignored because they are large; generated thumbnails and gallery JSON are the deployable web assets.

The agent side loads city data into ClickHouse, mirrors the Senso knowledge base into `senso_*` tables, and exposes a guarded query path to Subconscious and Telegram. The Telegram workers run as independent long-polling processes, one token per city, so the NY and Boston bots can coexist on one host.

What it took: city-aware config, two event datasets, hundreds of geocoded venues, a static map/gallery build, image compression, ClickHouse loaders, a Senso sync cache that avoids re-downloading known files, read-only SQL guardrails, per-city Telegram token resolution, systemd deploy units, and tests for the multi-city behavior.

## Architecture

```mermaid
flowchart LR
  A["Tech Week calendars"] --> B["City event datasets<br/>markdown, manifests, users"]
  B --> C["Venue geocoding<br/>OpenCage"]
  B --> D["Image + thumbnail pipeline"]
  C --> E["GeoJSON map assets"]
  D --> F["Gallery JSON + thumbnails"]
  E --> G["GitHub Pages navs<br/>maps + galleries"]
  F --> G

  B --> H["ClickHouse city tables<br/>nytw_* / bostw_*"]
  I["Senso knowledge base"] --> J["Sync agent<br/>skip unchanged downloads"]
  J --> K["ClickHouse senso_* tables"]
  A --> P["Sync agent<br/>direct Playwright + Partiful"]
  P --> H
  P --> Q["ClickHouse sync logs<br/>*_sync_runs / *_sync_changes"]

  H --> L["TWAG agent<br/>event search + guarded SQL"]
  K --> L
  L --> M["Telegram bots<br/>@Twagbot / @TwagBostonBot"]
  L --> N["Subconscious hosted runs"]
  O["ClickHouse tool server"] --> H
  N --> O
```

## Setup And Config

Create a local environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

City selection:

| City slug | Event range | Dataset | ClickHouse prefix |
| --- | --- | --- | --- |
| `nyc` | June 1-7, 2026 | `data/nytw-2026-for-agents` | `nytw` |
| `boston` | May 24-31, 2026 | `data/bostontw-2026-for-agents` | `bostw` |

Use `TWAG_CITY=nyc` or `TWAG_CITY=boston` in `.env`, or pass `--city` to CLI commands when you want to override the environment:

```bash
twag --city boston inspect-nytw --limit 5
```

### Required Services

ClickHouse:

```bash
CLICKHOUSE_HOST=
CLICKHOUSE_PORT=8443
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_DATABASE=default
CLICKHOUSE_SECURE=true
```

Subconscious agent endpoint:

```bash
SUBCONSCIOUS_API_KEY=
SUBCONSCIOUS_BASE_URL=https://api.subconscious.dev/v1
SUBCONSCIOUS_MODEL=subconscious/tim-qwen3.6-27b
SUBCONSCIOUS_RUN_ENGINE=tim-gpt
```

Telegram bots:

```bash
NYC_TELEGRAM_BOT_TOKEN=
BOSTON_TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_REQUEST_TIMEOUT=45
```

Define one token per city. `TWAG_CITY=nyc` reads `NYC_TELEGRAM_BOT_TOKEN`; `TWAG_CITY=boston` reads `BOSTON_TELEGRAM_BOT_TOKEN`. In production, run only one polling process per Telegram token.

Optional Senso and sync-agent settings:

```bash
SENSO_API_KEY=
SENSO_SYNC_ENABLED=true
SENSO_SYNC_INTERVAL_SECONDS=3600
SENSO_SYNC_REPLACE=false

TECHWEEK_CALENDAR_SYNC_ENABLED=true
TECHWEEK_CALENDAR_SYNC_CITIES=nyc,boston
TECHWEEK_CALENDAR_SYNC_INTERVAL_SECONDS=21600
TECHWEEK_EVENT_FETCH_CONCURRENCY=3
TECHWEEK_CALENDAR_MAX_SCROLL_TICKS=260
TECHWEEK_CALENDAR_PAGE_DELAY_SECONDS=0.1

NYTW_TOOL_URL=
NYTW_TOOL_TOKEN=
NYTW_TOOL_HOST=localhost
NYTW_TOOL_PORT=8000
TWAG_SYNC_AGENT_COMMAND=.venv/bin/twag-sync-agent
```

### Load And Inspect Data

```bash
twag --city nyc load-nytw --replace
twag --city boston load-nytw --replace

twag --city nyc inspect-nytw --limit 5
twag --city boston inspect-nytw --limit 5

twag sync-senso
twag sync-senso-log --limit 5 --item-limit 50
twag sync-calendars --cities nyc,boston
twag sync-calendar-log --limit 5 --item-limit 50
```

The Senso sync stores remote metadata and content hashes so repeated runs can skip files that have not changed.
The calendar sync uses the public Tech Week calendar API plus gentle Partiful
detail fetches, not Nimble. It writes append-only snapshots into
`nytw_calendar_events` / `bostw_calendar_events`, exposes the latest complete
run through `nytw_current_events` / `bostw_current_events`, and records
row-level changes in `nytw_sync_changes` and `bostw_sync_changes`.

### Build Maps And Galleries

```bash
TWAG_CITY=boston twag geocode-venues
TWAG_CITY=boston twag build-geojson
TWAG_CITY=boston twag build-thumbnails
TWAG_CITY=boston twag build-gallery
```

The same commands work for `TWAG_CITY=nyc`. The generated map HTML reads `docs/<city>.geojson`; the gallery reads `docs/<city>_gallery.json` and thumbnails under `docs/<city>/thumbs/`.

To preview locally:

```bash
cd docs
python3 -m http.server 8085
```

Then open `http://localhost:8085/events_map_boston.html`, `http://localhost:8085/events_gallery_boston.html`, `http://localhost:8085/events_map_nyc.html`, or `http://localhost:8085/events_gallery_nyc.html`.

### Run The Bots

For local development, run each city in its own shell:

```bash
TWAG_CITY=nyc TELEGRAM_AGENT_LOCK_FILE=.telegram-agent-nyc.lock twag telegram-agent
TWAG_CITY=boston TELEGRAM_AGENT_LOCK_FILE=.telegram-agent-boston.lock twag telegram-agent
```

On Ubuntu, the deploy scripts install separate systemd units for both bots,
the sync agent, and the browser terminal backend:

```bash
RUN_REMOTE_INSTALL=true deploy/ubuntu/rsync.privileged.sh

sudo systemctl enable --now twag-telegram-agent@$USER.service
sudo systemctl enable --now twag-telegram-agent-boston@$USER.service
sudo systemctl enable --now twag-sync-agent@$USER.service
sudo systemctl enable --now twag-terminal@$USER.service

deploy/ubuntu/control.sh status
```

The browser terminal keeps lightweight session snapshots in
`TWAG_TERMINAL_SESSION_DIR` so follow-up commands such as `more` can survive a
backend restart. The default deployed path is
`/var/log/twag/terminal-sessions`.

Useful logs:

```bash
journalctl -u twag-telegram-agent@$USER.service -f
journalctl -u twag-telegram-agent-boston@$USER.service -f
journalctl -u twag-sync-agent@$USER.service -f
journalctl -u twag-terminal@$USER.service -f
```

The helper script wraps the same logs and can show bounded output:

```bash
deploy/ubuntu/control.sh terminal-logs
LOG_SINCE="2 hours ago" LOG_FOLLOW=false deploy/ubuntu/control.sh terminal-logs
deploy/ubuntu/control.sh nginx-diagnose
```

Telegram read timeouts during long polling can happen occasionally. The agent logs them as transient and retries with backoff; investigate only if they cluster, exceed `TELEGRAM_REQUEST_TIMEOUT`, or coincide with duplicate polling processes for the same token.
