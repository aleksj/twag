/* TWAG event gallery — shared logic for per-city HTML pages.
 *
 * Each city HTML inlines GALLERY_CONFIG (galleryUrl, dateRange,
 * defaultDate) and calls initEventGallery(GALLERY_CONFIG).
 */

function pad2g(n) {
  return n < 10 ? "0" + n : "" + n;
}

function parseDateFromHashG() {
  const raw = (window.location.hash || "").replace(/^#/, "");
  const match = raw.match(/date=(\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : null;
}

function setDateInHashG(date) {
  window.location.hash = "date=" + date;
}

function formatHumanDateG(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const opts = { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" };
  return date.toLocaleDateString(undefined, opts);
}

function buildDatePickerG(container, dateRange, activeDate, onChange) {
  container.innerHTML = "";
  for (const date of dateRange) {
    const btn = document.createElement("button");
    btn.className = "date-btn" + (date === activeDate ? " active" : "");
    btn.textContent = formatHumanDateG(date);
    btn.addEventListener("click", () => onChange(date));
    container.appendChild(btn);
  }
}

function escapeHtmlG(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderTile(event) {
  const time = [event.start_time, event.end_time].filter(Boolean).join("–");
  const where = [event.venue_name, event.neighborhood].filter(Boolean).join(" · ");
  const cap = event.at_capacity ? `<span class="tile-cap">at capacity</span>` : "";
  const href = event.rsvp_url || "#";
  return `
    <a class="tile" href="${escapeHtmlG(href)}" target="_blank" rel="noopener">
      <div class="tile-img-wrap">
        <img class="tile-img" loading="lazy" src="${escapeHtmlG(event.image)}" alt="${escapeHtmlG(event.title)}">
      </div>
      <div class="tile-body">
        <div class="tile-title">${escapeHtmlG(event.title)} ${cap}</div>
        <div class="tile-meta">${escapeHtmlG(time)}</div>
        <div class="tile-meta">${escapeHtmlG(where)}</div>
        ${event.host ? `<div class="tile-host">${escapeHtmlG(event.host)}</div>` : ""}
      </div>
    </a>
  `;
}

async function initEventGallery(config) {
  const response = await fetch(config.galleryUrl);
  if (!response.ok) {
    document.getElementById("error").textContent =
      `Failed to load ${config.galleryUrl}: ${response.status}`;
    return;
  }
  const payload = await response.json();
  const allEvents = payload.events || [];

  const initialDate = parseDateFromHashG() || config.defaultDate;
  let activeDate = initialDate;

  const datePicker = document.getElementById("date-picker");
  const grid = document.getElementById("gallery-grid");
  const countEl = document.getElementById("count");

  function refresh() {
    buildDatePickerG(datePicker, config.dateRange, activeDate, (date) => {
      activeDate = date;
      setDateInHashG(date);
      refresh();
    });
    const filtered = allEvents.filter(e => e.event_date === activeDate);
    countEl.textContent =
      `${filtered.length} events on ${formatHumanDateG(activeDate)}`;
    grid.innerHTML = filtered.length
      ? filtered.map(renderTile).join("")
      : `<div class="empty">No events with images on this day.</div>`;
  }

  refresh();

  window.addEventListener("hashchange", () => {
    const hashDate = parseDateFromHashG();
    if (hashDate && hashDate !== activeDate) {
      activeDate = hashDate;
      refresh();
    }
  });
}
