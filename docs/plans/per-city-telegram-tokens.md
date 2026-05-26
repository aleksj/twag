# Per-city Telegram bot tokens

## Context

Hooking up a Boston Telegram bot must not break the running NYC bot. Today
`TelegramAgentConfig.from_env()` reads a single `TELEGRAM_BOT_TOKEN` env var,
so the NYC and Boston bots can't be run from the same `.env` file or the same
systemd `EnvironmentFile`. The user wants city-namespaced env vars
(`NYC_TELEGRAM_BOT_TOKEN`, `BOSTON_TELEGRAM_BOT_TOKEN`) so both can be defined
side-by-side and the bot process picks the right one based on `TWAG_CITY`.

Constraint: the NYC bot is already running in production with
`TELEGRAM_BOT_TOKEN` set. This change must be **backward-compatible** — the
NYC bot keeps working with no env change required.

Branch: `natea/per-city-telegram-tokens`, off `main` (the `nyc-event-gallery`
work has been merged in). The branch buys us the ability to test end-to-end
without touching the running NYC deployment until we're confident.

## Resolution order

`TelegramAgentConfig.from_env()` resolves the token by trying, in order:

1. `<CITY_SLUG_UPPER>_TELEGRAM_BOT_TOKEN` (e.g. `NYC_TELEGRAM_BOT_TOKEN`,
   `BOSTON_TELEGRAM_BOT_TOKEN`) — derived from `active_city().slug.upper()`.
2. `TELEGRAM_BOT_TOKEN` — legacy single-token env var, kept as the fallback.
3. Raise a clear error naming both candidates if neither is set.

The NYC slug is `"nyc"` → `NYC_TELEGRAM_BOT_TOKEN`. Confirmed with the user;
matches the existing `TWAG_CITY=nyc` convention.

## Files to modify

### Code

- **`src/twag_clickhouse/telegram_agent.py`** (`TelegramAgentConfig.from_env`):
  swap the single `os.getenv("TELEGRAM_BOT_TOKEN", ...)` call for a small
  resolver function that tries the city-prefixed var first, falls back to the
  generic one, and raises with both names listed on miss. ~10 lines.

### Tests

- **`tests/test_telegram_agent.py:564`**: the existing test sets
  `TELEGRAM_BOT_TOKEN` and asserts the config loads — keep that working
  (fallback path).
- **New test**: with `TWAG_CITY=boston` and only `BOSTON_TELEGRAM_BOT_TOKEN`
  set, the resolved `bot_token` is the Boston one. With both set, the
  city-specific var wins. With neither set, raise.

### Docs / env files

- **`.env.example`**: add `NYC_TELEGRAM_BOT_TOKEN=` and
  `BOSTON_TELEGRAM_BOT_TOKEN=` alongside the existing `TELEGRAM_BOT_TOKEN=`
  (which stays as the legacy fallback, with a note explaining the precedence).
- **`deploy/ubuntu/twag.env.example`**: same.
- **`docs/telegram-bot-setup.md`**: update the "parallel NYC + Boston" section
  to show one env file holding both tokens, instead of two env files. The
  per-process `TWAG_CITY` is still what selects which one gets used.

### Reused existing code

- `CityConfig.slug` (in `city.py`) gives us `"nyc"` / `"boston"`. Just call
  `.upper()`.
- `active_city()` is already imported in `telegram_agent.py`.

## Verification

1. **Unit tests** — `pytest tests/test_telegram_agent.py` covers all three
   resolution paths (city-specific only, legacy only, neither set).
2. **Local smoke test, fallback path** — with only `TELEGRAM_BOT_TOKEN=...` in
   `.env` and no city-specific tokens, run `TWAG_CITY=nyc twag telegram-agent`
   and confirm it still starts. (This is the running production setup; must
   not break.)
3. **Local smoke test, namespaced path** — set both
   `NYC_TELEGRAM_BOT_TOKEN=...` and `BOSTON_TELEGRAM_BOT_TOKEN=...` in `.env`,
   run `TWAG_CITY=boston twag telegram-agent`, confirm it uses the Boston
   token (e.g. send `/start` to the Boston bot — should reply; sending to the
   NYC bot should NOT reply because that process isn't running).
4. **Production rollout** — when ready, the NYC bot's env file can be updated
   from `TELEGRAM_BOT_TOKEN=...` to `NYC_TELEGRAM_BOT_TOKEN=...` (with the
   same token value), then we add `BOSTON_TELEGRAM_BOT_TOKEN=...` for the new
   process. Or just leave `TELEGRAM_BOT_TOKEN` set and add only the Boston
   var — either way works because of the fallback.

## Out of scope

- Renaming `TELEGRAM_BOT_TOKEN` to a city-prefixed name in the legacy code
  paths. We keep the generic name supported indefinitely as a fallback.
- Per-city `TELEGRAM_ALLOWED_CHAT_IDS` or `TELEGRAM_QUESTION_LOG_PATH`. Those
  are also shared today and could be city-namespaced in a follow-up if needed.
  Not required for the stated problem.
