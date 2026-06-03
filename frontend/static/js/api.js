/**
 * API helper — all fetch calls to the Flask backend.
 */
const API_BASE = "";  // same origin

async function apiFetch(path, options = {}) {
  const res = await fetch(API_BASE + path, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

const api = {
  status:         ()          => apiFetch("/api/status"),
  syncQV:         (body)      => apiFetch("/api/sync/quantum-view", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  syncTracking:   (tn)        => apiFetch(`/api/sync/tracking/${tn}`, { method: "POST" }),
  syncLogs:       ()          => apiFetch("/api/sync/logs"),
  shipments:      (params)    => apiFetch("/api/shipments?" + new URLSearchParams(params)),
  shipment:       (tn)        => apiFetch(`/api/shipments/${tn}`),
  kpiSummary:     ()          => apiFetch("/api/kpis/summary"),
  kpiByService:   ()          => apiFetch("/api/kpis/by-service"),
  kpiByState:     ()          => apiFetch("/api/kpis/by-state"),
  kpiDailyVolume: (days)      => apiFetch(`/api/kpis/daily-volume?days=${days}`),
  kpiExceptions:  ()          => apiFetch("/api/kpis/exceptions"),
  kpiTransitTime: ()          => apiFetch("/api/kpis/transit-time"),
};

function showLoading(show) {
  const el = document.getElementById("loading-overlay");
  if (el) el.classList.toggle("show", show);
}

function formatDate(d) {
  if (!d || d.length < 8) return d || "—";
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function onTimeBadge(on_time, is_delivered) {
  if (!is_delivered) return `<span class="badge badge-in-transit">In Transit</span>`;
  if (on_time === true)  return `<span class="badge badge-on-time">On Time</span>`;
  if (on_time === false) return `<span class="badge badge-late">Late</span>`;
  return `<span class="badge badge-unknown">Delivered</span>`;
}
