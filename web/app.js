const $ = (selector) => document.querySelector(selector);
const state = { data: null, installPrompt: null, busy: false, toastTimer: null };

function accessKey() { return localStorage.getItem("busForecastApiKey") || ""; }
function apiBase() { return (localStorage.getItem("busForecastApiUrl") || window.location.origin).replace(/\/+$/, ""); }
function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (accessKey()) headers.set("Authorization", `Bearer ${accessKey()}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  return fetch(`${apiBase()}${path}`, { ...options, headers, cache: "no-store" }).then(async (response) => {
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) throw new Error(payload?.detail || payload || `Request failed (${response.status})`);
    return payload;
  });
}
async function downloadFile(path, filename) {
  const headers = new Headers();
  if (accessKey()) headers.set("Authorization", `Bearer ${accessKey()}`);
  const response = await fetch(`${apiBase()}${path}`, { headers, cache: "no-store" });
  if (!response.ok) {
    let message = `Download failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove("show"), 4400);
}
function setBusy(busy, label = "Working…") {
  state.busy = busy;
  [$("#csvFile"), $("#trainButton"), $("#refreshForecast"), $("#demoButton")].forEach((node) => { if (node) node.disabled = busy; });
  $("#trainButton").innerHTML = busy ? `${label} <span>···</span>` : "Train / retrain model <span>→</span>";
}
function prettyDate(value) {
  if (!value) return "—";
  const date = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(date);
}
function percent(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : "—"; }
function setConnection(connected) {
  const node = $("#connection");
  node.classList.toggle("connected", connected);
  node.classList.toggle("failed", !connected);
  node.lastElementChild.textContent = connected ? "Service online" : "Service offline";
}
function renderTable(rows) {
  const target = $("#forecastRows");
  $("#downloadForecast").classList.toggle("disabled-link", !rows?.length);
  if (!rows?.length) {
    target.innerHTML = '<tr class="empty-row"><td colspan="4">Forecast rows will appear after model training.</td></tr>';
    return;
  }
  target.innerHTML = rows.slice(0, 250).map((row) => `<tr><td>${prettyDate(row.service_date)}</td><td><span class="route-pill">${escapeText(row.route_id)}</span></td><td class="numeric passenger-count">${Math.round(Number(row.predicted_passengers)).toLocaleString()}</td><td class="numeric">${Math.round(Number(row.lower_90_approx)).toLocaleString()} – ${Math.round(Number(row.upper_90_approx)).toLocaleString()}</td></tr>`).join("");
}
function escapeText(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function renderChart(rows) {
  const svg = $("#forecastChart");
  const empty = $("#emptyChart");
  if (!rows?.length) { svg.classList.add("hidden"); empty.classList.remove("hidden"); $("#chartRange").textContent = "—"; return; }
  svg.classList.remove("hidden"); empty.classList.add("hidden");
  const grouped = new Map();
  for (const row of rows) {
    const group = grouped.get(row.service_date) || { date: row.service_date, value: 0, low: 0, high: 0 };
    group.value += Number(row.predicted_passengers) || 0;
    group.low += Number(row.lower_90_approx) || 0;
    group.high += Number(row.upper_90_approx) || 0;
    grouped.set(row.service_date, group);
  }
  const points = [...grouped.values()].sort((a, b) => a.date.localeCompare(b.date));
  const width = Math.max(560, svg.parentElement.clientWidth || 560), height = 238;
  const pad = { left: 44, right: 15, top: 20, bottom: 33 };
  const max = Math.max(...points.map((p) => p.high), 1) * 1.12;
  const x = (index) => pad.left + (points.length === 1 ? 0 : index * (width - pad.left - pad.right) / (points.length - 1));
  const y = (value) => height - pad.bottom - (value / max) * (height - pad.top - pad.bottom);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const bandTop = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.high).toFixed(1)}`).join(" ");
  const bandBottom = points.slice().reverse().map((p, j) => { const i = points.length - 1 - j; return `L${x(i).toFixed(1)},${y(p.low).toFixed(1)}`; }).join(" ");
  const grid = [0, .33, .66, 1].map((part) => {
    const value = max * part, yy = y(value);
    return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#e9eef5"/><text x="${pad.left - 9}" y="${yy + 3}" text-anchor="end" fill="#9aa6b7" font-size="9" font-family="DM Mono,monospace">${Math.round(value / 1000)}k</text>`;
  }).join("");
  const xLabels = points.map((p, i) => (points.length <= 7 || i % Math.ceil(points.length / 7) === 0 || i === points.length - 1) ? `<text x="${x(i)}" y="${height - 8}" text-anchor="middle" fill="#98a4b4" font-size="9" font-family="DM Mono,monospace">${escapeText(p.date.slice(5))}</text>` : "").join("");
  const dots = points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.value)}" r="3.2" fill="#fff" stroke="#3479ee" stroke-width="2"><title>${escapeText(p.date)} · ${Math.round(p.value).toLocaleString()} boardings</title></circle>`).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `${grid}<path d="${bandTop} ${bandBottom} Z" fill="#b7cef4" opacity=".42"/><path d="${line}" fill="none" stroke="#3479ee" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>${dots}${xLabels}`;
  const total = points.reduce((sum, point) => sum + point.value, 0);
  $("#chartRange").textContent = `${points.length} days · ${Math.round(total).toLocaleString()} predicted boardings`;
}
function render(data) {
  state.data = data;
  $("#routeCount").textContent = data.data_loaded ? Number(data.route_count).toLocaleString() : "—";
  $("#rowCount").textContent = data.data_loaded ? `${Number(data.rows).toLocaleString()} route day records` : "Upload route counts to begin";
  $("#latestDate").textContent = prettyDate(data.latest_observed_date);
  $("#modelWape").textContent = percent(data.metrics?.model?.wape);
  const ml = data.metrics?.model?.wape == null ? Number.NaN : Number(data.metrics.model.wape), baseline = data.metrics?.seasonal_naive?.wape == null ? Number.NaN : Number(data.metrics.seasonal_naive.wape);
  $("#modelCompare").textContent = Number.isFinite(ml) && Number.isFinite(baseline) ? `Baseline ${percent(baseline)} · ${ml <= baseline ? "model leads" : "baseline leads"}` : "Rolling time based validation";
  $("#horizonLabel").textContent = data.model_trained ? `${data.horizon_days} days` : "—";
  $("#modelState").textContent = data.model_trained ? `Trained through ${prettyDate(data.trained_through)}` : "No model trained";
  const source = data.source === "synthetic demo data" ? "Synthetic sample data" : data.source === "company upload" ? "Operator data uploaded" : "Waiting for data";
  $("#sourceLabel").textContent = source;
  $("#updatedLabel").textContent = data.forecast_updated_at ? `Updated ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(data.forecast_updated_at))}` : "No forecast yet";
  $("#workflowNote").textContent = data.source === "synthetic demo data" ? "Demo numbers are synthetic. Replace them with approved company data before operational use." : "Forecasts refresh when daily counts arrive. Upload only data approved for this environment.";
  renderChart(data.forecasts);
  renderTable(data.forecasts);
}
async function refreshStatus() {
  try { render(await api("/api/status")); setConnection(true); }
  catch (error) { setConnection(false); if (error.message.includes("access key")) toast(error.message, true); }
}
async function withBusy(label, operation) {
  if (state.busy) return;
  setBusy(true, label);
  try { const result = await operation(); if (result.message) toast(result.message); if (result.status) render(result.status); else await refreshStatus(); }
  catch (error) { toast(error.message || "Something went wrong", true); }
  finally { setBusy(false); }
}
async function train(options = {}) {
  const horizon = Number(options.horizon || $("#trainDays").value || 7);
  const folds = Number(options.folds || 4);
  return api("/api/train", { method: "POST", body: JSON.stringify({ horizon, folds, missing_days: $("#missingDays").value }) });
}
function setup() {
  $("#settingsButton").addEventListener("click", () => { $("#apiKey").value = accessKey(); $("#apiUrl").value = localStorage.getItem("busForecastApiUrl") || ""; $("#settingsDialog").showModal(); });
  $("#saveKey").addEventListener("click", () => { const key = $("#apiKey").value.trim(), rawUrl = $("#apiUrl").value.trim(); if (rawUrl) { try { const parsed = new URL(rawUrl); if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(); localStorage.setItem("busForecastApiUrl", parsed.origin); } catch (_) { toast("Enter a valid API address starting with http:// or https://", true); return; } } else localStorage.removeItem("busForecastApiUrl"); key ? localStorage.setItem("busForecastApiKey", key) : localStorage.removeItem("busForecastApiKey"); $("#settingsDialog").close(); refreshStatus(); });
  $("#clearKey").addEventListener("click", () => { localStorage.removeItem("busForecastApiKey"); localStorage.removeItem("busForecastApiUrl"); $("#apiKey").value = ""; $("#apiUrl").value = ""; toast("Connection reset on this device"); });
  $("#trainButton").addEventListener("click", () => withBusy("Training…", () => train()));
  $("#demoButton").addEventListener("click", () => withBusy("Running demo…", async () => { await api("/api/data/demo", { method: "POST" }); return train({ horizon: 7, folds: 4 }); }));
  $("#refreshForecast").addEventListener("click", () => withBusy("Refreshing…", () => api("/api/forecast", { method: "POST", body: JSON.stringify({ days: Number($("#forecastDays").value), missing_days: $("#missingDays").value }) })));
  $("#downloadForecast").addEventListener("click", (event) => { event.preventDefault(); downloadFile("/api/forecast.csv", "bus_demand_forecast.csv").catch((error) => toast(error.message, true)); });
  $("#downloadSample").addEventListener("click", (event) => { event.preventDefault(); downloadFile("/api/template.csv", "daily_route_counts_sample.csv").catch((error) => toast(error.message, true)); });
  $("#forecastDays").addEventListener("change", () => { if (state.data?.model_trained) $("#refreshForecast").click(); });
  $("#csvFile").addEventListener("change", (event) => { const file = event.target.files?.[0]; if (!file) return; withBusy("Uploading…", async () => { const form = new FormData(); form.append("file", file); form.append("missing_days", $("#missingDays").value); return api("/api/data/upload", { method: "POST", body: form }); }).finally(() => { event.target.value = ""; }); });
  const drop = $("#dropzone");
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (event) => { const file = event.dataTransfer.files?.[0]; if (!file) return; const input = $("#csvFile"); const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true })); });
  window.addEventListener("resize", () => { if (state.data) renderChart(state.data.forecasts); });
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); state.installPrompt = event; $("#installButton").classList.remove("hidden"); });
  $("#installButton").addEventListener("click", async () => { if (!state.installPrompt) return; state.installPrompt.prompt(); await state.installPrompt.userChoice; state.installPrompt = null; $("#installButton").classList.add("hidden"); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  refreshStatus();
  window.setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, 60000);
}
document.addEventListener("DOMContentLoaded", setup);
