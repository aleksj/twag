# TWAG Search Roadmap

This roadmap moves TWAG search from prompt-led query guessing toward a typed,
measurable retrieval system. The goal is to make browser, Telegram, and CLI
answers cheaper, more complete, and easier to debug.

## 1. Query Understanding

- Introduce a typed `SearchPlan` before SQL generation.
- Capture city, requested result count, date range, time window, neighborhood,
  landmark/place, topic terms, availability filters, and sort intent.
- Route common event-search intents deterministically before invoking the model.
- Keep the model for ambiguity resolution and synthesis, not for deciding basic
  filters such as neighborhood, weekday, or morning/evening.

## 2. Places And Geo

- Replace hardcoded landmark examples with a city-scoped gazetteer.
- Store aliases, canonical names, neighborhood, optional coordinates, and radius.
- Resolve "near Columbia University", "around Harvard", and similar phrases to
  places first, then apply neighborhood or distance filters.
- Log unresolved place phrases so the gazetteer improves from real queries.

## 3. Retrieval And Ranking

- Use exact structured filters for date, time, RSVP status, capacity, city, and
  neighborhood before text ranking.
- Move topic matching from ad hoc `ILIKE` scoring toward fielded retrieval with
  explicit weights for title, host, venue, badges, description, and body text.
- Keep ranked pages intact: if the query requests a list with no explicit N,
  return the default page in rank order and support `more`.
- Add curated synonym expansion for high-value topics while keeping expansions
  visible in logs and tests.
- Evaluate hybrid lexical/vector retrieval only after deterministic filters and
  fielded lexical search are stable.

## 4. Grounded Rendering

- Render event result rows from returned data, keyed by `event_id`.
- Prevent invented URLs, dates, venues, or placeholder event text structurally,
  not only with prompt warnings.
- For model-written summaries, require references to retrieved row IDs and reject
  answers that cite rows outside the result set.
- Keep maps and result handoff keyed by the same event IDs shown in the answer.

## 5. Evaluation And Operations

- Build an eval set from real failures: "events near Columbia University",
  "events in East Village on Monday morning", "hacker", and "what changed since
  yesterday".
- Track planner correctness, retrieved result relevance, row grounding, latency,
  and token usage.
- Add CI checks for deterministic query SQL shape and answer formatting.
- Keep user-facing status concise; put diagnostics, SQL, and exceptions in logs.
