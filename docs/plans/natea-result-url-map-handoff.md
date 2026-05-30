# Handoff for natea/twag: terminal result maps

## Suggested PR Title

Support `result_url` on static event maps

## Context

The browser terminal can produce a filtered GeoJSON result set for a search,
for example events returned by a TWAG query. The preferred architecture is for
`natea/twag` to remain the canonical owner of the static map UI:

- TWAG terminal backend stores the filtered result GeoJSON.
- Terminal answers link to the canonical static map page, for example
  `https://natea.github.io/twag/events_map_boston.html`.
- The map URL carries `result_url=<absolute filtered GeoJSON URL>` in the hash.
- The static map fetches that GeoJSON instead of the full city GeoJSON when the
  parameter is present.

Until this lands upstream, filtered terminal result maps are disabled in the
terminal deployment with `TWAG_TERMINAL_RESULT_MAPS_ENABLED=false`. Plain date
map links such as `/map 2026-06-03` still work.

## URL Contract

Example:

```text
https://natea.github.io/twag/events_map_boston.html#date=2026-06-03&result_url=https%3A%2F%2Fdata.flowers%2Ftw%2Fterminal%2Fmap%2FSESSION%2FMAP.geojson
```

Rules:

- `date` keeps the existing behavior.
- `result_url` is optional.
- If `result_url` is present, fetch it instead of `config.geojsonUrl`.
- The fetched payload is still a GeoJSON `FeatureCollection` with normal event
  properties, so existing popup/sidebar/search logic can stay mostly unchanged.
- The terminal backend sends permissive CORS headers on these result GeoJSON
  responses so GitHub Pages can fetch them.

## Minimal Patch Shape

This is the local change that should be applied to Nate's current
`docs/events_map.js` rather than carried as a fork in `aleksj/twag`.

```diff
diff --git a/docs/events_map.js b/docs/events_map.js
@@
 function parseDateFromHash() {
   const raw = (window.location.hash || "").replace(/^#/, "");
   const match = raw.match(/date=(\d{4}-\d{2}-\d{2})/);
   return match ? match[1] : null;
 }
+
+function parseResultUrlFromUrl() {
+  for (const source of [
+    window.location.search.replace(/^\?/, ""),
+    window.location.hash.replace(/^#/, ""),
+  ]) {
+    const params = new URLSearchParams(source);
+    const raw = params.get("result_url");
+    if (!raw) continue;
+    try {
+      const url = new URL(raw, window.location.href);
+      if (url.protocol === "http:" || url.protocol === "https:") {
+        return url.href;
+      }
+    } catch (_error) {
+      return null;
+    }
+  }
+  return null;
+}
@@
-  const response = await fetch(config.geojsonUrl);
+  const resultUrl = parseResultUrlFromUrl();
+  const geojsonUrl = resultUrl || config.geojsonUrl;
+  const response = await fetch(geojsonUrl);
   if (!response.ok) {
     document.getElementById("error").textContent =
-      `Failed to load ${config.geojsonUrl}: ${response.status}`;
+      `Failed to load ${geojsonUrl}: ${response.status}`;
     return;
   }
   const fullGeoJson = await response.json();
@@
     const filtered = filterByDateAndSearch();
@@
     document.getElementById("count").textContent = query
       ? `${filtered.features.length} events matching "${query}" ${scopeLabel}`
-      : `${filtered.features.length} events on ${dateLabel}`;
+      : resultUrl
+        ? `${filtered.features.length} results on ${dateLabel}`
+        : `${filtered.features.length} events on ${dateLabel}`;
```

## Notes for Review

- The current upstream map already has search/sidebar integration. This patch
  intentionally keeps that logic intact and only changes the source GeoJSON.
- If the result GeoJSON contains events across multiple dates, the existing date
  chips still control which subset appears.
- A follow-up could add a clearer heading such as "Terminal search results", but
  that is not required for the first integration.

## Suggested Message to Nate

Hi Nate - we want to avoid maintaining a separate fork of the static map code
in the terminal/backend repo. The terminal can now expose filtered search
results as GeoJSON, and the clean integration point seems to be a small
`result_url` parameter on the canonical GitHub Pages map.

The desired flow is:

1. Terminal answer links to `https://natea.github.io/twag/events_map_<city>.html#date=...&result_url=...`.
2. The static map fetches `result_url` when present, otherwise it keeps loading
   the normal city GeoJSON.
3. Existing date/search/sidebar behavior continues to work against whichever
   GeoJSON was loaded.

I included the minimal patch shape above. Once this is live on
`natea.github.io/twag`, we can turn `TWAG_TERMINAL_RESULT_MAPS_ENABLED=true` in
the terminal backend and filtered result links should work without a separate
terminal map UI.
