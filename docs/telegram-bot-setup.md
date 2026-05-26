# Telegram bot setup (per city)

The TWAG Telegram bot is city-aware. `NytwSubconsciousAgent.from_env()` resolves
the active city from `TWAG_CITY`, so running a bot for a new city is a matter
of starting the process under the right environment — not code changes.

## Prerequisites per city

1. **A Telegram bot of its own.** Create one with [@BotFather](https://t.me/BotFather)
   (e.g. `@TwagBostonBot`) and copy the token. Do not reuse an existing token
   while another bot process is running — both processes will fight over the
   same `getUpdates` long-poll and randomly handle each other's messages.
2. **The city's ClickHouse tables loaded.** For Boston, that's
   `TWAG_CITY=boston twag load-nytw --replace` after running the dataset
   pipeline in `data/bostontw-2026-for-agents/scripts/`.

## Picking the bot token

The bot resolves its token in this order:

1. City-specific token names. For NYC, both `NY_TELEGRAM_BOT_TOKEN` and
   `NYC_TELEGRAM_BOT_TOKEN` are supported. For Boston, use
   `BOSTON_TELEGRAM_BOT_TOKEN`. **Use this for any multi-city setup**: define
   both tokens in one env file and each bot process picks the right one.
2. `TELEGRAM_BOT_TOKEN` — legacy single-token fallback. Kept so existing
   single-city deployments don't need to change anything.

If neither is set the bot fails fast with both candidate names in the error.

## Local run

```bash
TWAG_CITY=boston \
BOSTON_TELEGRAM_BOT_TOKEN=<boston-bot-token> \
twag telegram-agent
```

That's it. The bot greets as **Boston Tech Week**, uses the Boston vibe line on
subjective questions, and queries the `bostw_*` tables.

To run NY and Boston bots side-by-side on the same machine, put both tokens
in your `.env` and just vary `TWAG_CITY` + the lock file per process:

```bash
# .env (one file holds both)
NY_TELEGRAM_BOT_TOKEN=<nyc-token>
# NYC_TELEGRAM_BOT_TOKEN=<nyc-token> also works
BOSTON_TELEGRAM_BOT_TOKEN=<boston-token>
```

```bash
# NYC
TWAG_CITY=nyc \
TELEGRAM_AGENT_LOCK_FILE=.telegram-agent-nyc.lock \
twag telegram-agent

# Boston (in another shell)
TWAG_CITY=boston \
TELEGRAM_AGENT_LOCK_FILE=.telegram-agent-boston.lock \
twag telegram-agent
```

The default lock file (`.telegram-agent.lock`, set at
`src/twag_clickhouse/telegram_agent.py:978`) prevents multiple bot processes
from starting in the same directory. Each city needs its own.

## Production (Ubuntu / systemd)

The shipped unit files (`deploy/ubuntu/twag-telegram-agent@.service`) all share
a single `EnvironmentFile=/etc/twag/twag.env`. Two ways to add Boston:

### Option A — one shared env file, separate unit per city (recommended)

Lets NYC and Boston run in parallel as independent services from the same
env file. Per-city Telegram bot tokens make this safe.

1. Add the Boston bot token to the shared env file:

   ```bash
   sudo $EDITOR /etc/twag/twag.env
   ```

   ```bash
   # /etc/twag/twag.env — keep existing NYC values, add:
   NY_TELEGRAM_BOT_TOKEN=<nyc-bot-token>       # NYC_TELEGRAM_BOT_TOKEN also works
   BOSTON_TELEGRAM_BOT_TOKEN=<boston-bot-token>
   ```

   You can keep `TELEGRAM_BOT_TOKEN=...` for back-compat — the NYC process
   will fall back to it if `NY_TELEGRAM_BOT_TOKEN` and
   `NYC_TELEGRAM_BOT_TOKEN` are unset. No NYC change
   required to enable the Boston bot.

2. Create a Boston copy of the unit file with `TWAG_CITY=boston` baked in:

   ```bash
   sudo cp /etc/systemd/system/twag-telegram-agent@.service \
           /etc/systemd/system/twag-telegram-agent-boston@.service
   sudo $EDITOR /etc/systemd/system/twag-telegram-agent-boston@.service
   ```

   Add to the `[Service]` block:

   ```ini
   Environment=TWAG_CITY=boston
   Environment=TELEGRAM_AGENT_LOCK_FILE=/run/twag/telegram-agent-boston.lock
   ```

   (The NYC unit can stay as-is — its `TWAG_CITY` is already `nyc` by default.)

3. Enable and start it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now twag-telegram-agent-boston@<linux-user>.service
   ```

4. Check it's healthy:

   ```bash
   sudo systemctl status twag-telegram-agent-boston@<linux-user>.service
   sudo journalctl -u twag-telegram-agent-boston@<linux-user>.service -f
   ```

### Option B — flip the existing service to Boston

If Boston is replacing NYC (no parallel run), just edit the existing env file:

```bash
sudo $EDITOR /etc/twag/twag.env
```

Set:

```bash
TWAG_CITY=boston
BOSTON_TELEGRAM_BOT_TOKEN=<boston-bot-token>
# (or leave TELEGRAM_BOT_TOKEN=<boston-bot-token> if you prefer the legacy var)
```

Then restart:

```bash
sudo systemctl restart twag-telegram-agent@<linux-user>.service
```

The NYC `nytw_*` tables remain in ClickHouse but nothing queries them.

## Nimble tool-server (only if using hosted Subconscious runs)

The local Telegram bot talks to ClickHouse directly, so it does **not** need
the Nimble tool-server. Only set up a Boston Nimble process if you use the
hosted `deploy-nytw-agent` flow (Subconscious runs hitting a public tool URL).

If you do, mirror Option A with `twag-nimble@.service`:

1. Boston env file (`/etc/twag/twag-boston.env`) additionally needs:

   ```bash
   NYTW_TOOL_PORT=8001   # different from NYC's 8000
   NYTW_TOOL_TOKEN=<boston-tool-token>
   ```

2. Copy the unit:

   ```bash
   sudo cp /etc/systemd/system/twag-nimble@.service \
           /etc/systemd/system/twag-nimble-boston@.service
   sudo sed -i 's|/etc/twag/twag.env|/etc/twag/twag-boston.env|' \
        /etc/systemd/system/twag-nimble-boston@.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now twag-nimble-boston@<linux-user>.service
   ```

3. Expose port 8001 (firewall + reverse proxy) and use that URL with
   `TWAG_CITY=boston twag deploy-nytw-agent --tool-url <boston-public-url>`.

## Verification checklist

After starting a city's bot:

- DM `/start` to the bot — greeting should name the right city
  ("TWAG Boston Tech Week Bot").
- Send an open-ended subjective question — refusal should use that city's
  vibe line (Boston: "This is Boston — give me wicked specifics.").
- Send a real query (e.g. "List AI events in Cambridge") — answer should
  contain real events from the city's dataset. Check `journalctl` for SQL
  references to the right prefix (`bostw_*` vs `nytw_*`).
