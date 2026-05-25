/* TWAG event map — shared logic for per-city HTML pages.
 *
 * Each city HTML inlines its own MAP_CONFIG (token, center, zoom,
 * geojsonUrl, dateRange) and then calls initEventMap(MAP_CONFIG).
 */

function pad2(n) {
  return n < 10 ? "0" + n : "" + n;
}

function parseDateFromHash() {
  const raw = (window.location.hash || "").replace(/^#/, "");
  const match = raw.match(/date=(\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : null;
}

function setDateInHash(date) {
  window.location.hash = "date=" + date;
}

function formatHumanDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const opts = { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" };
  return date.toLocaleDateString(undefined, opts);
}

function buildDatePicker(container, dateRange, activeDate, onChange) {
  container.innerHTML = "";
  for (const date of dateRange) {
    const btn = document.createElement("button");
    btn.className = "date-btn" + (date === activeDate ? " active" : "");
    btn.textContent = formatHumanDate(date);
    btn.addEventListener("click", () => onChange(date));
    container.appendChild(btn);
  }
}

function filterFeaturesByDate(geojson, date) {
  return {
    type: "FeatureCollection",
    features: geojson.features.filter(f => f.properties.event_date === date),
  };
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function popupHtml(props) {
  const title = escapeHtml(props.title);
  const time = [props.start_time, props.end_time].filter(Boolean).join("–");
  const where = [props.venue_name, props.neighborhood].filter(Boolean).join(" · ");
  const host = props.host ? `<div class="popup-host">${escapeHtml(props.host)}</div>` : "";
  const rsvp = props.rsvp_url
    ? `<a class="popup-rsvp" href="${escapeHtml(props.rsvp_url)}" target="_blank" rel="noopener">RSVP →</a>`
    : "";
  const cap = props.at_capacity ? `<span class="popup-cap">at capacity</span>` : "";
  return `
    <div class="popup">
      <div class="popup-title">${title}</div>
      <div class="popup-meta">${escapeHtml(time)} · ${escapeHtml(where)} ${cap}</div>
      ${host}
      ${rsvp}
    </div>
  `;
}

async function initEventMap(config) {
  mapboxgl.accessToken = config.token;
  const map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/streets-v12",
    center: [config.centerLon, config.centerLat],
    zoom: config.zoom,
  });
  map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");

  const response = await fetch(config.geojsonUrl);
  if (!response.ok) {
    document.getElementById("error").textContent =
      `Failed to load ${config.geojsonUrl}: ${response.status}`;
    return;
  }
  const fullGeoJson = await response.json();

  const initialDate = parseDateFromHash() || config.defaultDate;
  let activeDate = initialDate;

  const datePicker = document.getElementById("date-picker");
  buildDatePicker(datePicker, config.dateRange, activeDate, (date) => {
    activeDate = date;
    setDateInHash(date);
    refresh();
  });

  function refresh() {
    buildDatePicker(datePicker, config.dateRange, activeDate, (date) => {
      activeDate = date;
      setDateInHash(date);
      refresh();
    });
    const filtered = filterFeaturesByDate(fullGeoJson, activeDate);
    document.getElementById("count").textContent =
      `${filtered.features.length} events on ${formatHumanDate(activeDate)}`;
    const source = map.getSource("events");
    if (source) {
      source.setData(filtered);
    }
  }

  map.on("load", () => {
    map.addSource("events", {
      type: "geojson",
      data: filterFeaturesByDate(fullGeoJson, activeDate),
      cluster: true,
      clusterMaxZoom: 14,
      clusterRadius: 50,
    });

    map.addLayer({
      id: "clusters",
      type: "circle",
      source: "events",
      filter: ["has", "point_count"],
      paint: {
        "circle-color": [
          "step", ["get", "point_count"],
          "#51bbd6", 5,
          "#f1f075", 15,
          "#f28cb1",
        ],
        "circle-radius": [
          "step", ["get", "point_count"],
          18, 5, 24, 15, 32,
        ],
        "circle-stroke-width": 2,
        "circle-stroke-color": "#ffffff",
      },
    });

    map.addLayer({
      id: "cluster-count",
      type: "symbol",
      source: "events",
      filter: ["has", "point_count"],
      layout: {
        "text-field": "{point_count_abbreviated}",
        "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
        "text-size": 13,
      },
    });

    map.addLayer({
      id: "unclustered-point",
      type: "circle",
      source: "events",
      filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-color": "#e8543e",
        "circle-radius": 8,
        "circle-stroke-width": 2,
        "circle-stroke-color": "#ffffff",
      },
    });

    map.on("click", "clusters", (e) => {
      const features = map.queryRenderedFeatures(e.point, { layers: ["clusters"] });
      const clusterId = features[0].properties.cluster_id;
      map.getSource("events").getClusterExpansionZoom(clusterId, (err, zoom) => {
        if (err) return;
        map.easeTo({ center: features[0].geometry.coordinates, zoom });
      });
    });

    map.on("click", "unclustered-point", (e) => {
      const feature = e.features[0];
      const [lon, lat] = feature.geometry.coordinates;
      new mapboxgl.Popup({ maxWidth: "300px" })
        .setLngLat([lon, lat])
        .setHTML(popupHtml(feature.properties))
        .addTo(map);
    });

    for (const layerId of ["clusters", "unclustered-point"]) {
      map.on("mouseenter", layerId, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", layerId, () => (map.getCanvas().style.cursor = ""));
    }

    refresh();
  });

  window.addEventListener("hashchange", () => {
    const hashDate = parseDateFromHash();
    if (hashDate && hashDate !== activeDate) {
      activeDate = hashDate;
      refresh();
    }
  });
}
