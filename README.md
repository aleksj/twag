# TWAG Tech Week Bot (NY + Boston)

Use the NY Tech Week bot through Telegram: [https://t.me/Twagbot](https://t.me/Twagbot)

## Boston port by Nate Aune ([@natea](https://twitter.com/natea))

I forked the upstream NY Tech Week bot and repurposed it for Boston Tech Week
2026 by parameterizing the entire stack around a `TWAG_CITY` env var (NYC and
Boston live side-by-side; adding a new city is one entry in `city.py`). I also
re-crawled the Boston events dataset from `tech-week.com/calendar/boston` and
added two new views served from GitHub Pages:

- **Event map** — clustered map of events per day, powered by Mapbox GL JS
  with venues geocoded via OpenCage:
  [Boston](https://natea.github.io/twag/events_map_boston.html) ·
  [NYC](https://natea.github.io/twag/events_map_nyc.html)
- **Image gallery** — scrollable grid of event hero images; tap a tile to RSVP
  on Partiful:
  [Boston](https://natea.github.io/twag/events_gallery_boston.html) ·
  [NYC](https://natea.github.io/twag/events_gallery_nyc.html)

The Telegram bot now includes a `/map [date]` command and appends a "🗺 View on
map" link to every event-list answer.

## Cities

TWAG is parameterized by the `TWAG_CITY` environment variable. Two cities are registered:

| Slug     | Display name           | Dataset                              | Table prefix |
|----------|------------------------|--------------------------------------|--------------|
| `nyc`    | NY Tech Week 2026      | `data/nytw-2026-for-agents`          | `nytw_*`     |
| `boston` | Boston Tech Week 2026  | `data/bostontw-2026-for-agents`      | `bostw_*`    |

Set `TWAG_CITY=boston` (in `.env` or the process environment) to point the CLI,
tool server, and Telegram bot at Boston. Each CLI subcommand also accepts a
top-level `--city` override (e.g. `twag --city boston load-nytw`). Tables for
each city live side-by-side in ClickHouse; the bot loaded with `TWAG_CITY=nyc`
will only see `nytw_*` rows, and vice versa.

To add a new city, register a `CityConfig` in `src/twag_clickhouse/city.py` and
copy `data/nytw-2026-for-agents/scripts/` into a new dataset directory with the
city's `calendar_url` swapped in `crawl_manifest.py`.

## Building the event map and gallery

The static map and gallery pages under `docs/` are generated from the dataset
under `data/<city>-for-agents/`. Boston ships pre-built; for a new city or to
refresh, run the pipeline below. All commands honor `TWAG_CITY` (or pass
`--city <slug>`).

### One-time setup

Copy `.env.example` to `.env` and fill in:

- `OPENCAGE_API_KEY` — get a free key at <https://opencagedata.com/>. Free
  tier: 2,500 requests/day, 1 req/sec, permanent storage allowed.
- `MAPBOX_PUBLIC_TOKEN` — get a public token at
  <https://account.mapbox.com/access-tokens/>. **Restrict it by referrer**
  (e.g. `https://natea.github.io/twag/*` and `http://localhost:8085/*`) in the
  Mapbox dashboard since this token ships to the browser in `docs/config.js`.
- `TWAG_PUBLIC_MAP_BASE_URL` — the public URL of the deployed map (used by the
  Telegram bot to link back to it).

Then copy `docs/config.example.js` to `docs/config.js` and paste the same
Mapbox public token in there. `docs/config.js` is committed (the token is
referrer-restricted, not secret).

### Geocode venues → `venues.json`

```bash
TWAG_CITY=boston twag geocode-venues
```

Reads `data/<city>-for-agents/events/*.md`, calls OpenCage at 1 req/sec, and
caches results to `data/<city>-for-agents/venues.json`. Idempotent — only
geocodes addresses missing from the cache. Pass `--refresh` to re-geocode
everything; `--limit N` to test on a small batch first.

For Boston that's ~10 minutes for 456 venues; for NYC ~16 minutes for 981.

### Build the map GeoJSON → `docs/<city>.geojson`

```bash
TWAG_CITY=boston twag build-geojson
```

Joins events + venues, filters canceled/no-coords/stub events, writes
`docs/<city>.geojson` for `events_map_<city>.html` to fetch client-side.

### Fetch event images → `data/<city>-for-agents/images/`

The dataset's `scripts/enrich.py` downloads each event's hero image from
Partiful's Firebase CDN into `images/<event_id>.png`. This is part of the
crawl pipeline (see `data/<city>-for-agents/scripts/`):

```bash
cd data/bostontw-2026-for-agents
mkdir -p images
python3 scripts/enrich.py --events events/ --images-dir images/
```

Idempotent — skips already-downloaded images. Boston produces ~770 MB of
full-resolution PNG/JPG across 589 events; the `images/` directory is **not**
committed (matches the NYC convention). Re-run only when refreshing the
dataset.

### Generate gallery thumbnails → `docs/<city>/thumbs/`

```bash
TWAG_CITY=boston twag build-thumbnails
```

Resizes every local image to ~400 px JPEG at quality 80 and writes to
`docs/<city>/thumbs/<event_id>.jpg`. Boston: 582 thumbs, ~15 MB total (vs.
770 MB at full res). These **are** committed so GitHub Pages can serve them.
Idempotent unless `--refresh`.

### Build the gallery JSON → `docs/<city>_gallery.json`

```bash
TWAG_CITY=boston twag build-gallery
```

Emits the gallery payload (title, time, host, neighborhood, RSVP URL,
description excerpt, capacity). The path under each entry's `image` field
points at the local thumbnail when one exists, else the Firebase URL from the
event frontmatter — so this command must run *after* `build-thumbnails` to
pick up the local paths.

### Preview locally

```bash
cd docs && python3 -m http.server 8085
```

Open `http://localhost:8085/events_map_boston.html` or
`http://localhost:8085/events_gallery_boston.html`.

### Deploy

Push to `main`, then in repo settings enable **Pages → Deploy from a branch
→ main → /docs**. Sites publish at
`https://<user>.github.io/<repo>/events_map_<city>.html` and
`...events_gallery_<city>.html`.


![QR code for https://t.me/Twagbot](docs/assets/twagbot-qr.png)

Send `/start` or `/help` to the bot. It replies:

```text
**TWAG NY Tech Week Bot**
Ask me data-backed questions about TechWeek NY events.

**Sponsored by data.flowers** - the data excellence company.
Want to sponsor TechWeek AI search? Contact info@data.flowers

**Try**
- List AI events in SoHo
- Show cybersecurity events with open RSVPs
- Which neighborhoods have the most events?
- more

**Commands**
`/help` - show this guide
`/verbose` - show the agent thinking stream
`/quiet` - show only result updates and final answers

Use concrete criteria like topic, date, neighborhood, host, capacity, RSVP status, or time.
```

Telegram commands:

```text
/help
/verbose
/quiet
```

Use `/quiet` for streamed results only. Use `/verbose` when you want to see the
agent's thinking stream while it constructs and executes data queries.

Ask data-driven questions based on criteria, keywords, dates, locations, hosts,
capacity, RSVP status, or event counts:

```text
List AI events in SoHo
Which neighborhoods have the most events?
Show cybersecurity events with open RSVPs
Find investor events on June 4
List events involving running
more
```

The bot intentionally refuses vague subjective prompts like "best event" or
"what should I do?" with a blunt NYC-style nudge to provide searchable criteria.

## Project Lineage

TWAG was inspired by and builds on
[Stage-11-Agentics/nytw-2026-for-agents](https://github.com/Stage-11-Agentics/nytw-2026-for-agents),
an agent-friendly NY Tech Week 2026 event mirror. You can think of this repo as
an application-layer fork of that idea: the upstream project made the event
landscape readable for agents, while TWAG adds ClickHouse loading, guarded
natural-language querying, Senso knowledge sync, Nimble deployment, and a
Telegram interface.

## What This Repo Contains

A Python integration for ClickHouse with:

- environment-based configuration
- a reusable client wrapper
- CLI health checks and ad hoc queries
- an example event table bootstrap command

## Deploy It Yourself

Use this section if you want to run your own copy of the Telegram bot or query
the NY Tech Week data from the command line.

### Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` with your ClickHouse host and credentials. Put the key you provided in `CLICKHOUSE_PASSWORD` or `CLICKHOUSE_API_KEY`.

Required for a real connection:

```bash
CLICKHOUSE_SERVICE_ID=your-clickhouse-service-id
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-key
CLICKHOUSE_DATABASE=default
CLICKHOUSE_SECURE=true
```

If you have ClickHouse Cloud API credentials, resolve the remote service endpoint first:

```bash
export CLICKHOUSE_CLOUD_KEY_ID='your-cloud-api-key-id'
export CLICKHOUSE_CLOUD_KEY_SECRET='your-cloud-api-key-secret'
export CLICKHOUSE_SERVICE_ID='your-clickhouse-service-id'
twag-clickhouse resolve-cloud-service
```

Use the returned `host`, `port`, and `username` as your SQL connection settings.

The service id alone is not enough to connect to ClickHouse SQL. It identifies the
Cloud service, but the loader needs either:

- `CLICKHOUSE_HOST` plus SQL credentials, or
- `CLICKHOUSE_CLOUD_KEY_ID` and `CLICKHOUSE_CLOUD_KEY_SECRET` to resolve the host
  through ClickHouse Cloud, plus SQL credentials for the actual load.

### CLI

Check connectivity:

```bash
twag-clickhouse health
```

Run a query:

```bash
twag-clickhouse query "SELECT now()"
```

Create a simple demo table:

```bash
twag-clickhouse init-demo
```

Insert a demo event:

```bash
twag-clickhouse insert-event page_view '{"path":"/"}'
```

### NY Tech Week 2026 Dataset

This workspace includes a local copy of the machine-readable NY Tech Week data
from
[Stage-11-Agentics/nytw-2026-for-agents](https://github.com/Stage-11-Agentics/nytw-2026-for-agents):

```text
data/nytw-2026-for-agents/
├── events/        # 1 markdown file per event
├── manifest.json  # original calendar extraction
├── users.json     # Partiful host/user profiles
└── scripts/       # source helper scripts from the dataset repo
```

Validate the local dataset:

```bash
twag-clickhouse inspect-nytw
```

Load it into ClickHouse:

```bash
twag-clickhouse load-nytw --replace
```

The loader creates these tables:

- `nytw_events`
- `nytw_hosts`
- `nytw_event_hosts`
- `nytw_manifest`

### Subconscious Agent

The `twag agent` command runs a Subconscious-backed ClickHouse agent. Event
queries are the primary use case and prefer the remote ClickHouse `nytw_*`
tables. Senso is not queried directly by the agent. Instead, the Nimble tool
server mirrors Senso knowledge-base content into ClickHouse `senso_*` tables,
which the agent can use for general Tech Week context when the event rows are
not enough.

Required environment:

```bash
SUBCONSCIOUS_API_KEY=your-subconscious-key
SUBCONSCIOUS_MODEL=subconscious/tim-qwen3.6-27b

SENSO_API_KEY=your-senso-key
SENSO_BASE_URL=https://apiv2.senso.ai/api/v1
SENSO_ORG_ID=8a58ca64-adea-4935-8479-41319cd332b1
SENSO_ORG_SLUG=techweek
SENSO_SYNC_ENABLED=true
SENSO_SYNC_INTERVAL_SECONDS=3600
SENSO_SYNC_REPLACE=false

CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-clickhouse-password
CLICKHOUSE_DATABASE=default
CLICKHOUSE_QUERY_RETRIES=3
CLICKHOUSE_RETRY_INITIAL_SECONDS=1
CLICKHOUSE_RETRY_MAX_SECONDS=8
```

Ask a ClickHouse-backed NY Tech Week question:

```bash
twag agent "Which neighborhoods have the most live AI events?"
```

Stream the raw Subconscious response in the terminal, including thinking tags
when the model emits them:

```bash
twag agent --verbose "How many events are in SoHo?"
```

Start a dialogue and page through event-list results:

```bash
twag agent
> list events involving running
> more
> more
> exit
```

The ClickHouse tool accepts only single-statement read-only SQL
(`SELECT`, `WITH`, `SHOW`, `DESCRIBE`, or `EXPLAIN`) and requires queries to
reference one of the `nytw_*` or synced `senso_*` tables.

Manually sync Senso into ClickHouse:

```bash
twag sync-senso
```

The Nimble tool server also starts the same sync loop automatically when
`SENSO_API_KEY` and `SENSO_SYNC_ENABLED=true` are set.

### Telegram Bot

The Telegram bot runs the same logic as `twag agent` for every Telegram user
who messages the bot. It uses Telegram long polling, not a webhook, so run only
one bot process per Telegram bot token.

### Configure

Create the bot with Telegram's `@BotFather`, then add these values to `.env`:

```bash
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_ALLOWED_CHAT_IDS=
TELEGRAM_CLEAR_WEBHOOK_ON_POLL=true
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_REQUEST_TIMEOUT=45
TELEGRAM_RETRY_INITIAL_SECONDS=2
TELEGRAM_RETRY_MAX_SECONDS=60
TELEGRAM_STATUS_HEARTBEAT_SECONDS=8
TELEGRAM_STREAM_DRAFTS=true
TELEGRAM_STREAM_DRAFT_INTERVAL_SECONDS=1
TELEGRAM_QUESTION_LOG_PATH=logs/twag-telegram-questions.jsonl
```

Leave `TELEGRAM_ALLOWED_CHAT_IDS` empty to answer every Telegram user. To
restrict access, set comma-separated chat IDs:

```bash
TELEGRAM_ALLOWED_CHAT_IDS=1551872383,123456789
```

The bot also needs the normal `twag agent` environment:

```bash
SUBCONSCIOUS_API_KEY=your-subconscious-key
SUBCONSCIOUS_ENABLE_THINKING=true
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-clickhouse-password
CLICKHOUSE_DATABASE=default
CLICKHOUSE_QUERY_RETRIES=3
CLICKHOUSE_RETRY_INITIAL_SECONDS=1
CLICKHOUSE_RETRY_MAX_SECONDS=8
```

`TELEGRAM_QUESTION_LOG_PATH` writes one JSON object per handled Telegram
message. Each line includes the Telegram chat/user metadata, question text,
route, answer status, duration, and aggregated Subconscious token usage. Set it
to `false` to disable the server-side question log.

### Run

From the repository root:

```bash
source .venv/bin/activate
twag telegram-agent
```

Equivalent direct command:

```bash
.venv/bin/twag-telegram-agent
```

When it starts, it prints:

```text
TWAG Telegram agent is polling. Press Ctrl+C to stop.
```

Send `/start` or `/help` to the bot in Telegram. It should reply:

```text
**TWAG NY Tech Week Bot**
Ask me data-backed questions about TechWeek NY events.

**Sponsored by data.flowers** - the data excellence company.
Want to sponsor TechWeek AI search? Contact info@data.flowers

**Try**
- List AI events in SoHo
- Show cybersecurity events with open RSVPs
- Which neighborhoods have the most events?
- more

**Commands**
`/help` - show this guide
`/verbose` - show the agent thinking stream
`/quiet` - show only result updates and final answers

Use concrete criteria like topic, date, neighborhood, host, capacity, RSVP status, or time.
```

Then ask questions such as:

```text
Which neighborhoods have the most events?
List AI events in SoHo
Show cybersecurity events with open RSVPs
List events involving running
more
```

The bot will push back on subjective prompts such as "best event" or "what
should I do?" Ask with criteria instead.

In `/quiet` mode, the bot streams only visible answer text by sending a message
and editing it as the answer develops. In `/verbose` mode, it also shows the
agent thinking stream and progress heartbeats so you can see what the backend is
doing. Thinking mode is enabled with `SUBCONSCIOUS_ENABLE_THINKING=true`; quiet
mode strips hidden thinking before anything is shown to the user.

Event-list responses stay short. When more matching events exist, the bot says
so at the bottom of the answer; send `more` to page through the next result set
for the same search.

ClickHouse event search uses hybrid lexical retrieval: exact keyword matches,
term-overlap scoring across title, description, markdown body, host,
neighborhood, venue, address, and badges, plus phrase boosts. This gives
RAG-like candidate retrieval without requiring a separate embedding pipeline.

Telegram network timeouts are treated as transient. The bot retries with
exponential backoff using `TELEGRAM_RETRY_INITIAL_SECONDS` and
`TELEGRAM_RETRY_MAX_SECONDS`. Keep `TELEGRAM_REQUEST_TIMEOUT` higher than
`TELEGRAM_POLL_TIMEOUT`.

### Troubleshooting

If you see this error:

```text
Conflict: terminated by other getUpdates request
```

another process is already polling the same Telegram bot token. Stop the other
process before starting a new one. On this machine, check for local copies with:

```bash
ps -axo pid,ppid,command | rg 'twag telegram-agent|twag-telegram-agent'
```

Then stop the duplicate PID:

```bash
kill <pid>
```

The bot creates `.telegram-agent.lock` while running to catch duplicate local
starts. If the process crashed and left a stale lock file, remove it:

```bash
rm .telegram-agent.lock
```

For polling mode, any old Telegram webhook is cleared automatically on startup
when `TELEGRAM_CLEAR_WEBHOOK_ON_POLL=true`.

### Hosted Subconscious Runs

For the agent to run inside Subconscious and call remote ClickHouse directly,
Subconscious needs a public HTTPS tool endpoint. This package includes one:

```bash
NYTW_TOOL_TOKEN=choose-a-long-random-token
twag-nytw-tool-server
```

Deploy that server on a public host with the same `CLICKHOUSE_*` environment
variables used by the CLI. The server exposes:

- `GET /health`
- `POST /query` with `{ "sql": "SELECT ... FROM nytw_events ..." }`

Create a Subconscious hosted run that calls the public tool:

```bash
NYTW_TOOL_URL=https://your-public-tool.example.com
twag deploy-nytw-agent "Which neighborhoods have the most live AI events?"
```

To inspect the payload before sending it:

```bash
twag deploy-nytw-agent \
  --print-payload \
  "Which neighborhoods have the most live AI events?"
```

### Ubuntu Systemd Deployment

Use `deploy/ubuntu/` to run the Telegram bot and a separate Nimble process on a
remote Ubuntu host.

There are two supported deployment flows:

- clone the GitHub repo on the Ubuntu box
- rsync the working tree from your laptop to the Ubuntu box

#### Option A: Clone On Ubuntu

On the Ubuntu box:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/your-org-or-user/your-repo.git twag
cd twag
deploy/ubuntu/install.sh
```

#### Option B: Rsync From This Machine

Use the tracked template for a safe example:

```bash
cp deploy/ubuntu/rsync.example.sh deploy/ubuntu/rsync.privileged.sh
```

Edit the local-only privileged copy:

```bash
$EDITOR deploy/ubuntu/rsync.privileged.sh
```

Set:

```bash
REMOTE_USER=ubuntu
REMOTE_HOST=your-private-or-public-ubuntu-ip
REMOTE_DIR=/home/ubuntu/twag
SSH_PORT=22
```

By default, rsync also uploads the local `.env` to the systemd environment file
on the Ubuntu host:

```bash
LOCAL_ENV_FILE=.env
REMOTE_ENV_FILE=/etc/twag/twag.env
SYNC_ENV_FILE=true
```

The repository copy still excludes `.env`; the script sends it separately and
installs it as `/etc/twag/twag.env` with `0640` permissions. To skip syncing
secrets, run:

```bash
SYNC_ENV_FILE=false deploy/ubuntu/rsync.privileged.sh
```

Use a persistent directory such as `/home/ubuntu/twag` or `/opt/twag`. Do not
use `/tmp/twag` for systemd services; `/tmp` can be cleaned by the OS and
systemd will fail before the Python process starts.

Then sync:

```bash
deploy/ubuntu/rsync.privileged.sh
```

To sync and run the installer in one step:

```bash
RUN_REMOTE_INSTALL=true deploy/ubuntu/rsync.privileged.sh
```

The privileged rsync copy is ignored by Git so a private IP address does not get
pushed.

After a plain rsync, run this on the Ubuntu box:

```bash
cd /home/ubuntu/twag
SERVICE_USER=ubuntu deploy/ubuntu/install-after-rsync.sh
```

#### What The Installer Does

The installer:

- creates `.venv`
- installs the package in editable mode
- creates `/etc/twag/twag.env` from `deploy/ubuntu/twag.env.example` if missing
- installs `twag-telegram-agent@.service`
- installs `twag-nimble@.service`

Edit the remote env file:

```bash
sudoedit /etc/twag/twag.env
```

Required values for the Telegram process:

```bash
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
SUBCONSCIOUS_API_KEY=your-subconscious-key
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-clickhouse-password
CLICKHOUSE_DATABASE=default
TELEGRAM_QUESTION_LOG_PATH=/var/log/twag/questions.jsonl
```

The Nimble service defaults to:

```bash
TWAG_NIMBLE_COMMAND=.venv/bin/twag-nytw-tool-server
```

When `SENSO_API_KEY` is set, the Nimble service also mirrors Senso into
ClickHouse on startup and every `SENSO_SYNC_INTERVAL_SECONDS` seconds:

```bash
SENSO_API_KEY=your-senso-key
SENSO_BASE_URL=https://apiv2.senso.ai/api/v1
SENSO_ORG_ID=8a58ca64-adea-4935-8479-41319cd332b1
SENSO_ORG_SLUG=techweek
SENSO_SYNC_ENABLED=true
SENSO_SYNC_INTERVAL_SECONDS=3600
```

Override `TWAG_NIMBLE_COMMAND` in `/etc/twag/twag.env` if your Nimble process is
different.

Start both services, replacing `$USER` if you installed under another account:

```bash
sudo systemctl enable --now twag-telegram-agent@$USER.service
sudo systemctl enable --now twag-nimble@$USER.service
```

Operate both services:

```bash
deploy/ubuntu/control.sh status
deploy/ubuntu/control.sh restart
deploy/ubuntu/control.sh logs
```

Follow one service:

```bash
journalctl -u twag-telegram-agent@$USER.service -f
journalctl -u twag-nimble@$USER.service -f
```

If you see `Failed to set up mount namespacing: /tmp/twag: No such file or
directory`, the service was installed from a temporary app directory. Resync to
a persistent directory and rerun the installer:

```bash
REMOTE_DIR=/opt/twag RUN_REMOTE_INSTALL=true deploy/ubuntu/rsync.privileged.sh
ssh root@your-private-or-public-ubuntu-ip 'systemctl daemon-reload && systemctl restart twag-telegram-agent@root.service twag-nimble@root.service'
```

Useful ClickHouse queries:

```sql
SELECT event_date, count()
FROM nytw_events
WHERE NOT canceled AND fetch_status = 'ok'
GROUP BY event_date
ORDER BY event_date;
```

```sql
SELECT h.name, count() AS events
FROM nytw_event_hosts eh
JOIN nytw_hosts h ON h.user_id = eh.user_id
WHERE NOT eh.is_platform_admin
GROUP BY h.name
ORDER BY events DESC
LIMIT 20;
```

## Notes

The key is intentionally not hardcoded in source. Keep it in `.env`, your shell environment, or your deployment secret manager.
