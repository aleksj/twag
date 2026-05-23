# TWAG NY Tech Week Bot

Use the system through Telegram: [https://t.me/Twagbot](https://t.me/Twagbot)

![QR code for https://t.me/Twagbot](docs/assets/twagbot-qr.png)

Send `/start` or `/help` to the bot. It replies:

```text
Hi, I'm a bot that will answer questions about TechWeek NY events!
```

Ask data-driven questions based on criteria, keywords, dates, locations, hosts,
capacity, RSVP status, or event counts:

```text
List AI events in SoHo
Which neighborhoods have the most events?
Show cybersecurity events with RSVP links
Find investor events on June 4
List events involving running
more
```

The bot intentionally refuses vague subjective prompts like "best event" or
"what should I do?" with a blunt NYC-style nudge to provide searchable criteria.

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

This workspace includes a local copy of the machine-readable NY Tech Week data from `Stage-11-Agentics/nytw-2026-for-agents`:

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

The `twag agent` command runs a Subconscious-backed agent. Senso is the default
knowledge path when `SENSO_API_KEY` is configured: ordinary company/product/
policy questions are answered from the Senso knowledge base with source
citations. NY Tech Week event questions still use the remote ClickHouse
`nytw_*` tables.

Required environment:

```bash
SUBCONSCIOUS_API_KEY=your-subconscious-key
SUBCONSCIOUS_MODEL=subconscious/tim-qwen3.6-27b

SENSO_API_KEY=your-senso-key
SENSO_BASE_URL=https://sdk.senso.ai/api/v1
SENSO_MAX_RESULTS=5

CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-clickhouse-password
CLICKHOUSE_DATABASE=default
```

Ask a Senso-backed knowledge question:

```bash
twag agent "What is our refund policy?"
```

Ask a ClickHouse-backed NY Tech Week question:

```bash
twag agent "Which neighborhoods have the most live AI events?"
```

Start a dialogue and page through event-list results:

```bash
twag agent
> list events involving running
> more
> more
> exit
```

The ClickHouse event tool accepts only single-statement read-only SQL
(`SELECT`, `WITH`, `SHOW`, `DESCRIBE`, or `EXPLAIN`) and requires NYTW queries
to reference one of the `nytw_*` tables.

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
```

Leave `TELEGRAM_ALLOWED_CHAT_IDS` empty to answer every Telegram user. To
restrict access, set comma-separated chat IDs:

```bash
TELEGRAM_ALLOWED_CHAT_IDS=1551872383,123456789
```

The bot also needs the normal `twag agent` environment:

```bash
SUBCONSCIOUS_API_KEY=your-subconscious-key
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=your-clickhouse-password
CLICKHOUSE_DATABASE=default
```

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
Hi, I'm a bot that will answer questions about TechWeek NY events!
```

Then ask questions such as:

```text
Which neighborhoods have the most events?
List AI events in SoHo
Show cybersecurity events with RSVP links
List events involving running
more
```

The bot will push back on subjective prompts such as "best event" or "what
should I do?" Ask with criteria instead.

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
```

The Nimble service defaults to:

```bash
TWAG_NIMBLE_COMMAND=.venv/bin/twag-nytw-tool-server
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
