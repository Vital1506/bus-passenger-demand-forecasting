import { canonicalRoute, matchDestinations, normalizePlaceLabel, placeSuggestions } from "./route_catalog.mjs";
import { buildMonthlySeries, chartModel, describeError, escapeHtml, formatNumber, missingMonthRanges, monthLabel, normalizeHistoryPayload, normalizeRoutesPayload, searchRoutes, summarizeRoute } from "./monthly_explorer.mjs";

const $ = (selector) => document.querySelector(selector);
const state = { data: null, installPrompt: null, busy: false, checking: false, toastTimer: null, publicDemo: false, lastCheckedAt: null, view: "overview", opening: true, openingStartedAt: Date.now(), openingTimer: null, openingMessageTimer: null, routeCatalog: null, monitoring: null, identity: null, permissions: null, monthly: { loaded: false, loading: false, routes: [], selected: "", history: new Map(), query: "" } };

function accessKey() { return localStorage.getItem("busForecastApiKey") || ""; }
const ROLE_LABELS = { admin: "Administrator", operator: "Transport operator", analyst: "Read-only analyst" };
function roleLabel(role) { return ROLE_LABELS[role] || role || "Unknown role"; }
function permissionAllowed(node) {
  const needed = node?.dataset?.permission;
  if (!needed || !state.permissions) return true;
  return needed.split(/\s+/).every((permission) => state.permissions.has(permission));
}
function applyPermissions() {
  document.querySelectorAll("[data-permission]").forEach((node) => {
    const allowed = permissionAllowed(node);
    node.disabled = !allowed;
    node.classList.toggle("permission-blocked", !allowed);
    if (!allowed) node.title = "Your role does not include this action";
  });
}
async function loadIdentity(force = false) {
  if (state.identity && !force) return state.identity;
  try {
    const payload = await api("/api/security/me");
    const principal = payload?.principal;
    state.identity = payload;
    state.permissions = new Set(principal?.permissions || []);
    const badge = $("#identity");
    if (badge && principal) {
      const agency = payload.workspace_agency || principal.agency;
      badge.textContent = `${principal.name} · ${roleLabel(principal.role)} · ${agency}`;
      badge.title = `Signed in as ${principal.name} (${roleLabel(principal.role)}) for agency ${agency}. Role permissions: ${(principal.permissions || []).join(", ")}. Requests are limited to ${payload.rate_limit_per_minute} per minute.`;
      badge.dataset.role = principal.role;
      badge.classList.remove("hidden");
    }
  } catch (error) {
    state.identity = null;
    state.permissions = null;
    const badge = $("#identity");
    if (badge) { badge.classList.add("hidden"); badge.textContent = ""; }
  }
  applyPermissions();
  return state.identity;
}
function apiBase() { return (localStorage.getItem("busForecastApiUrl") || window.location.origin).replace(/\/+$/, ""); }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (accessKey()) headers.set("Authorization", `Bearer ${accessKey()}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(`${apiBase()}${path}`, { ...options, headers, signal: controller.signal, cache: "no-store" });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = Array.isArray(payload?.detail)
        ? payload.detail.map((item) => item?.msg || "").filter(Boolean).join("; ")
        : payload?.detail;
      throw new Error(detail || payload || `Request failed (${response.status})`);
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("The forecast service took too long to respond. Retry or continue to the dashboard.");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
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
  [$("#csvFile"), $("#trainButton"), $("#demoButton"), $("#saveCountButton")].forEach((node) => { if (node) node.disabled = busy || !permissionAllowed(node) || (state.publicDemo && !state.operatorAccess); });
  $("#refreshForecast").disabled = busy;
  $("#forecastDays").disabled = busy;
  $("#forecastRoute").disabled = busy;
  $("#trainButton").innerHTML = busy ? `${label} <span>···</span>` : "Train my forecast <span>→</span>";
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
  const visibleRows = filterForecastRows(rows);
  $("#downloadForecast").classList.toggle("disabled-link", !visibleRows.length);
  if (!visibleRows.length) {
    target.innerHTML = '<tr class="empty-row"><td colspan="4">Forecast rows will appear after model training.</td></tr>';
    return;
  }
  target.innerHTML = visibleRows.slice(0, 250).map((row) => `<tr><td>${prettyDate(row.service_date)}</td><td><span class="route-pill">${escapeText(row.route_id)}</span></td><td class="numeric passenger-count">${Math.round(Number(row.predicted_passengers)).toLocaleString()}</td><td class="numeric">${Math.round(Number(row.lower_90_approx)).toLocaleString()} – ${Math.round(Number(row.upper_90_approx)).toLocaleString()}</td></tr>`).join("");
}
function escapeText(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function filterForecastRows(rows) {
  const items = Array.isArray(rows) ? rows : [];
  const selected = $("#forecastRoute")?.value || "all";
  return selected === "all" ? items : items.filter((row) => String(row.route_id) === selected);
}
function syncForecastRoutes(rows) {
  const select = $("#forecastRoute");
  if (!select) return;
  const previous = select.value || "all";
  const routes = [...new Set((Array.isArray(rows) ? rows : []).map((row) => String(row.route_id ?? "")).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  select.replaceChildren(new Option("All routes", "all"), ...routes.map((route) => new Option(route, route)));
  select.value = routes.includes(previous) ? previous : "all";
}
function renderForecastViewStatus() {
  const note = $("#forecastViewStatus");
  const chip = $("#forecastSourceLabel");
  if (!note) return;
  const data = state.data;
  const loaded = Boolean(data && data.data_loaded);
  const source = String(data?.source || "");
  const type = !loaded ? "unavailable" : /synthetic/i.test(source) ? "synthetic" : "forecast";
  note.textContent = !loaded
    ? "No data is loaded yet. Record a daily count on Overview, upload a CSV under Data Sources, or load the synthetic demo, then refresh the forecast."
    : `${source || "Unknown source"} · ${Number(data.route_count || 0).toLocaleString()} routes · ${Number(data.rows || 0).toLocaleString()} route-day records · horizon ${data.horizon_days || "—"} days · ${data.forecast_updated_at ? `forecast updated ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(data.forecast_updated_at))}` : "no forecast generated yet"}.`;
  if (chip) {
    chip.className = `data-label data-label-${type}`;
    chip.dataset.labelType = type;
    chip.textContent = type === "synthetic" ? "SYNTHETIC DEMO" : type === "forecast" ? "FORECAST" : "UNAVAILABLE";
  }
}
function renderChart(rows) {
  const svg = $("#forecastChart");
  const empty = $("#emptyChart");
  const visibleRows = filterForecastRows(rows);
  if (!visibleRows.length) { svg.classList.add("hidden"); empty.classList.remove("hidden"); $("#chartRange").textContent = "—"; return; }
  svg.classList.remove("hidden"); empty.classList.add("hidden");
  const grouped = new Map();
  for (const row of visibleRows) {
    const group = grouped.get(row.service_date) || { date: row.service_date, value: 0, low: 0, high: 0 };
    group.value += Number(row.predicted_passengers) || 0;
    group.low += Number(row.lower_90_approx) || 0;
    group.high += Number(row.upper_90_approx) || 0;
    grouped.set(row.service_date, group);
  }
  const points = [...grouped.values()].sort((a, b) => a.date.localeCompare(b.date));
  const chartColor = getComputedStyle(document.documentElement).getPropertyValue("--blue").trim() || "#8050d8";
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
    return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#ece4f4"/><text x="${pad.left - 9}" y="${yy + 3}" text-anchor="end" fill="#877c94" font-size="9" font-family="DM Mono,monospace">${Math.round(value / 1000)}k</text>`;
  }).join("");
  const xLabels = points.map((p, i) => (points.length <= 7 || i % Math.ceil(points.length / 7) === 0 || i === points.length - 1) ? `<text x="${x(i)}" y="${height - 8}" text-anchor="middle" fill="#877c94" font-size="9" font-family="DM Mono,monospace">${escapeText(p.date.slice(5))}</text>` : "").join("");
  const dots = points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.value)}" r="3.2" fill="#fff" stroke="${getComputedStyle(document.documentElement).getPropertyValue("--blue").trim() || "#8050d8"}" stroke-width="2"><title>${escapeText(p.date)} · ${Math.round(p.value).toLocaleString()} boardings</title></circle>`).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `${grid}<path d="${bandTop} ${bandBottom} Z" fill="#cbb2ed" opacity=".42"/><path d="${line}" fill="none" stroke="${chartColor}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>${dots}${xLabels}`;
  const total = points.reduce((sum, point) => sum + point.value, 0);
  const routeLabel = $("#forecastRoute")?.value;
  $("#chartRange").textContent = `${points.length} days${routeLabel && routeLabel !== "all" ? ` · ${routeLabel}` : ""} · ${Math.round(total).toLocaleString()} predicted boardings`;
}
function render(data) {
  state.data = data;
  state.lastCheckedAt = new Date();
  $("#routeCount").textContent = data.data_loaded ? Number(data.route_count).toLocaleString() : "—";
  $("#rowCount").textContent = data.data_loaded ? `${Number(data.rows).toLocaleString()} route day records` : "Upload route counts to begin";
  $("#latestDate").textContent = prettyDate(data.latest_observed_date);
  $("#modelWape").textContent = percent(data.metrics?.model?.wape);
  const ml = data.metrics?.model?.wape == null ? Number.NaN : Number(data.metrics.model.wape), baseline = data.metrics?.seasonal_naive?.wape == null ? Number.NaN : Number(data.metrics.seasonal_naive.wape);
  $("#modelCompare").textContent = Number.isFinite(ml) && Number.isFinite(baseline) ? `Baseline ${percent(baseline)} · ${ml <= baseline ? "model leads" : "baseline leads"}` : "Rolling time based validation";
  $("#horizonLabel").textContent = data.model_trained ? `${data.horizon_days} days` : "—";
  $("#modelState").textContent = data.model_trained ? `Trained through ${prettyDate(data.trained_through)}` : "No model trained";
  const source = ({ "synthetic demo data": "Synthetic sample data", "synthetic schedule demo data": "Synthetic timetable scenario", "company upload": "Operator data uploaded", "company feed": "Daily counts entered or imported" })[data.source] || "Waiting for data";
  $("#sourceLabel").textContent = source;
  $("#updatedLabel").textContent = data.forecast_updated_at ? `Updated ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(data.forecast_updated_at))}` : "No forecast yet";
  renderForecastViewStatus();
  $("#workflowNote").textContent = state.publicDemo ? (state.operatorAccess ? "Synthetic preview shown because no operator data is loaded yet. Upload daily counts, or a timetable to create a labelled synthetic scenario." : "This public preview is read-only. An authorized operator key is required to upload data or train a model.") : data.source === "synthetic schedule demo data" ? "This timetable-based scenario is synthetic, not observed passenger demand. Replace it with actual passenger counts before operational use." : data.source === "synthetic demo data" ? "Demo numbers are synthetic. Replace them with approved company data before operational use." : "Forecasts refresh when daily counts arrive. Upload only data approved for this environment.";
  syncForecastRoutes(data.forecasts);
  renderChart(data.forecasts);
  renderTable(data.forecasts);
  renderExtraPages(data);
}

const PAGE_INFO = {
  overview: ["Overview", "A quick read on service demand and forecast freshness."],
  live: ["Live Operations", "A rolling view of the latest observed counts and the forecast."],
  forecasts: ["Demand Forecasts", "Explore the demand outlook and download route-level estimates."],
  routes: ["Route Network", "Compare forecast totals and validation scores by route."],
  analytics: ["Passenger Analytics", "Monthly observed boardings by route."],
  occupancy: ["Occupancy & Capacity", "Projected occupancy and capacity utilisation."],
  alerts: ["Smart Alerts", "Operator alerts for demand, data-quality, and model checks."],
  planner: ["Service Planner", "Route totals, review counts, and an exportable plan."],
  data: ["Data Sources", "See which passenger counts are saved and when they were updated."],
  model: ["How it works", "A plain-language guide to the data and forecast."],
  monitoring: ["Model Monitoring", "Model version, accuracy, freshness, and data-quality checks."],
  notifications: ["Alerts & reports", "Operator notifications, delivery channels, and downloadable reports."],
  explorer: ["Historical Explorer", "Search Chicago CTA bus routes and review their observed monthly boardings."],
  settings: ["Settings", "API connection, key, and the signed-in agent."]
};
function setSplashStatus(message) { const label = $("#splashLoading"); if (label) label.textContent = message; }
function renderPlaceSuggestions(input, list, values, preferred = []) {
  if (!input || !list) return;
  const open = document.activeElement === input;
  const normalized = normalizePlaceLabel(input.value).toLocaleLowerCase("en-IN");
  const matching = values.filter((value) => normalizePlaceLabel(value).toLocaleLowerCase("en-IN") !== normalized);
  const preferredKeys = new Set(preferred.map((value) => normalizePlaceLabel(value).toLocaleLowerCase("en-IN")));
  list.replaceChildren(...matching.map((value, index) => {
    const option = document.createElement("div");
    option.id = `${list.id}-option-${index}`;
    option.className = "place-suggestion";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.dataset.value = value;
    const name = document.createElement("span");
    name.className = "place-suggestion-name";
    name.textContent = value;
    const kind = document.createElement("small");
    kind.textContent = preferredKeys.has(normalizePlaceLabel(value).toLocaleLowerCase("en-IN")) ? "Listed for this route" : "Place in route catalogue";
    option.append(name, kind);
    return option;
  }));
  const show = open && matching.length > 0;
  list.hidden = !show;
  input.setAttribute("aria-expanded", String(show));
  if (!show) input.removeAttribute("aria-activedescendant");
}
function updateRouteSuggestions() {
  const originInput = $("#countOrigin"), destinationInput = $("#countDestination");
  const originList = $("#originSuggestions"), destinationList = $("#destinationSuggestions");
  const hint = $("#routeCatalogHint");
  if (!originInput || !destinationInput || !originList || !destinationList || !hint) return;
  const catalogOrigins = Object.keys(state.routeCatalog?.origins || {});
  const origin = normalizePlaceLabel(originInput.value);
  const destinations = matchDestinations(origin, state.routeCatalog);
  renderPlaceSuggestions(originInput, originList, placeSuggestions(originInput.value, state.routeCatalog));
  renderPlaceSuggestions(destinationInput, destinationList, placeSuggestions(destinationInput.value, state.routeCatalog, destinations), destinations);
  if (!state.routeCatalog) {
    hint.textContent = "Place suggestions are unavailable right now. You can still type a starting point and destination; custom places are not map-verified.";
  } else if (!origin) {
    hint.textContent = `Search ${catalogOrigins.length.toLocaleString()} starting cities and all listed destinations. Choose a start to rank its listed routes first.`;
  } else if (!destinations.length) {
    hint.textContent = `No catalogue routes start at “${origin}”. You can still enter a custom place label; it will not be map-verified.`;
  } else {
    const exactDestination = destinations.some((value) => normalizePlaceLabel(value).toLocaleLowerCase("en-IN") === normalizePlaceLabel(destinationInput.value).toLocaleLowerCase("en-IN"));
    hint.textContent = exactDestination
      ? `Listed route: ${canonicalRoute(origin, destinationInput.value, state.routeCatalog).routeId}. Catalogue entries are suggestions, not live service confirmation.`
      : `${destinations.length.toLocaleString()} listed destinations from ${origin}. Start typing to narrow the list, or enter a custom destination.`;
  }
}
function wirePlacePicker(input, list, update) {
  if (!input || !list) return;
  let activeIndex = -1;
  const options = () => [...list.querySelectorAll('[role="option"]')];
  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  };
  const activate = (index) => {
    const items = options();
    if (!items.length) return;
    activeIndex = (index + items.length) % items.length;
    items.forEach((item, position) => item.setAttribute("aria-selected", String(position === activeIndex)));
    input.setAttribute("aria-activedescendant", items[activeIndex].id);
    items[activeIndex].scrollIntoView({ block: "nearest" });
  };
  const choose = (option) => {
    if (!option) return;
    input.value = option.dataset.value;
    update();
    close();
    input.focus();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  input.addEventListener("focus", () => { update(); });
  input.addEventListener("blur", () => window.setTimeout(() => {
    if (!input.closest(`#${input.id}-picker`)?.contains(document.activeElement)) close();
  }, 120));
  input.addEventListener("input", () => { activeIndex = -1; update(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const items = options();
      if (!items.length) { update(); return; }
      event.preventDefault();
      activate(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter" && !list.hidden && activeIndex >= 0) {
      event.preventDefault();
      choose(options()[activeIndex]);
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      close();
    }
  });
  list.addEventListener("mousedown", (event) => {
    if (event.target.closest('[role="option"]')) event.preventDefault();
  });
  list.addEventListener("click", (event) => {
    const option = event.target.closest('[role="option"]');
    if (!option) return;
    choose(option);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(`#${input.id}-picker`)) close();
  });
}
async function loadRouteCatalog() {
  try {
    const response = await fetch("/static/route_catalog.json", { cache: "force-cache" });
    if (!response.ok) throw new Error(`Route catalogue request failed (${response.status})`);
    const catalog = await response.json();
    if (!catalog?.origins || typeof catalog.origins !== "object") throw new Error("Route catalogue has an invalid format");
    state.routeCatalog = catalog;
  } catch (error) {
    console.warn("Route suggestions could not be loaded", error);
    state.routeCatalog = null;
  }
  updateRouteSuggestions();
}
function dismissSplash() {
  if (!state.opening) return;
  state.opening = false;
  clearTimeout(state.openingTimer);
  clearTimeout(state.openingMessageTimer);
  const splash = $("#appSplash");
  if (splash) { splash.setAttribute("aria-busy", "false"); splash.classList.add("is-hidden"); }
  const shell = $("#appShell");
  if (shell) shell.removeAttribute("aria-hidden");
  const heading = $("#pageTitle");
  if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
}
async function finishSplash(connected, failureMessage = "") {
  if (!state.opening) return;
  clearTimeout(state.openingMessageTimer);
  const remaining = Math.max(0, 700 - (Date.now() - state.openingStartedAt));
  if (remaining) await new Promise((resolve) => setTimeout(resolve, remaining));
  if (!state.opening) return;
  const splash = $("#appSplash");
  splash.setAttribute("aria-busy", "false");
  splash.classList.toggle("is-ready", connected);
  splash.classList.toggle("is-error", !connected);
  $("#splashProgress").classList.toggle("is-complete", connected);
  if (connected) {
    setSplashStatus("Forecast ready. Opening dashboard…");
    $("#openDashboard").classList.remove("hidden");
    $("#openDashboard").focus({ preventScroll: true });
    state.openingTimer = setTimeout(dismissSplash, 1600);
  } else {
    setSplashStatus(failureMessage || "The forecast service could not be reached. Check your connection and try again.");
    $("#retryOpening").classList.remove("hidden");
    $("#offlineOpening").classList.remove("hidden");
    $("#retryOpening").focus({ preventScroll: true });
  }
}
function navigate(view, updateHash = true) {
  if (!PAGE_INFO[view]) view = "overview";
  state.view = view;
  document.querySelectorAll("[data-pages]").forEach((node) => {
    const pages = (node.dataset.pages || "").split(/\s+/);
    node.hidden = !pages.includes(view);
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("#pageTitle").textContent = PAGE_INFO[view][0];
  $("#pageSummary").textContent = PAGE_INFO[view][1];
  if (updateHash && location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  if (view === "forecasts") {
    if (state.data) { syncForecastRoutes(state.data.forecasts); renderChart(state.data.forecasts); renderTable(state.data.forecasts); }
    renderForecastViewStatus();
  }
  if (view === "explorer") void loadMonthlyDataset();
  if (view === "monitoring") void loadMonitoring();
  if (view === "notifications") void loadNotificationsPage();
}
function readRoutePlans() {
  try {
    const value = JSON.parse(localStorage.getItem("busForecastRoutePlans") || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (_) { return {}; }
}
function routePlanFields(route) {
  const value = readRoutePlans()[route] || {};
  const trips = Number(value.trips), target = Number(value.target);
  return {
    trips: Number.isInteger(trips) && trips > 0 && trips <= 1000 ? trips : null,
    target: Number.isFinite(target) && target > 0 && target <= 100000 ? target : null,
  };
}
function exportRoutePlan() {
  const forecasts = Array.isArray(state.data?.forecasts) ? state.data.forecasts : [];
  if (!forecasts.length) { toast("Generate a forecast before exporting a planning scenario", true); return; }
  const header = ["service_date", "route_id", "predicted_boardings", "lower_90_approx", "upper_90_approx", "operator_planned_trips_per_day", "operator_target_boardings_per_trip", "trips_at_target_point_forecast", "trips_at_target_upper_approx", "additional_trips_at_upper_approx"];
  const quote = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const rows = forecasts.slice().sort((a, b) => String(a.service_date).localeCompare(String(b.service_date)) || String(a.route_id).localeCompare(String(b.route_id), undefined, { numeric: true })).map((row) => {
    const plan = routePlanFields(String(row.route_id));
    const point = Math.max(0, Number(row.predicted_passengers) || 0);
    const upper = Math.max(point, Number(row.upper_90_approx) || point);
    const pointTrips = plan.target ? Math.ceil(point / plan.target) : "";
    const upperTrips = plan.target ? Math.ceil(upper / plan.target) : "";
    const gap = plan.trips && plan.target ? Math.max(0, upperTrips - plan.trips) : "";
    return [row.service_date, row.route_id, point, row.lower_90_approx ?? "", row.upper_90_approx ?? "", plan.trips ?? "", plan.target ?? "", pointTrips, upperTrips, gap].map(quote).join(",");
  });
  const blob = new Blob(["\uFEFF", header.map(quote).join(","), "\r\n", rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = "bus_service_planning_scenario.csv"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderRoutes(data) {
  const forecasts = Array.isArray(data.forecasts) ? data.forecasts : [];
  const groups = new Map();
  for (const row of forecasts) {
    const route = String(row.route_id ?? "Unknown");
    const days = groups.get(route) || new Map();
    const predicted = Math.max(0, Number(row.predicted_passengers) || 0);
    const daily = days.get(row.service_date) || { predicted: 0, lower: 0, upper: 0 };
    daily.predicted += predicted;
    daily.lower += Math.max(0, Number(row.lower_90_approx) || 0);
    daily.upper += Math.max(predicted, Number(row.upper_90_approx) || predicted);
    days.set(row.service_date, daily);
    groups.set(route, days);
  }
  const plans = readRoutePlans();
  const routes = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }));
  const total = forecasts.reduce((sum, row) => sum + (Number(row.predicted_passengers) || 0), 0);
  const days = new Set(forecasts.map((row) => row.service_date)).size;
  let reviewCount = 0;
  $("#routesTotal").textContent = routes.length.toLocaleString();
  $("#routesBoardings").textContent = Math.round(total).toLocaleString();
  $("#routesDailyAverage").textContent = routes.length && days ? Math.round(total / (routes.length * days)).toLocaleString() : "—";
  $("#routeRows").innerHTML = routes.length ? routes.map(([route, routeDays], routeIndex) => {
    const peak = [...routeDays.entries()].sort((a, b) => b[1].predicted - a[1].predicted || a[0].localeCompare(b[0]))[0];
    const config = plans[route] || {};
    const trips = Number(config.trips), target = Number(config.target);
    const tripsValue = Number.isInteger(trips) && trips > 0 && trips <= 1000 ? trips : "";
    const targetValue = Number.isFinite(target) && target > 0 && target <= 100000 ? target : "";
    const hasPlan = tripsValue !== "" && targetValue !== "";
    const pointPerTrip = hasPlan ? peak[1].predicted / trips : Number.NaN;
    const upperPerTrip = hasPlan ? peak[1].upper / trips : Number.NaN;
    const pointTrips = hasPlan ? Math.ceil(peak[1].predicted / target) : null;
    const upperTrips = hasPlan ? Math.ceil(peak[1].upper / target) : null;
    const gap = hasPlan ? Math.max(0, upperTrips - trips) : null;
    let signal = "Add assumptions", signalClass = "signal-muted";
    if (hasPlan && gap > 0) { signal = `Review · +${gap} trip${gap === 1 ? "" : "s"} at high estimate`; signalClass = "signal-alert"; reviewCount += 1; }
    else if (hasPlan && pointPerTrip >= target * 0.85) { signal = "Near target"; signalClass = "signal-near"; }
    else if (hasPlan) { signal = "Within target"; signalClass = "signal-ok"; }
    const safeRoute = escapeText(route);
    const boardingsRange = `${Math.round(peak[1].predicted).toLocaleString()} / ${Math.round(peak[1].upper).toLocaleString()}`;
    const perTripRange = hasPlan ? `${Math.round(pointPerTrip).toLocaleString()} / ${Math.round(upperPerTrip).toLocaleString()}` : "—";
    const tripRange = hasPlan ? `${pointTrips} / ${upperTrips}` : "—";
    return `<tr><td><span class="route-pill">${safeRoute}</span></td><td>${prettyDate(peak[0])}</td><td class="numeric passenger-count" title="Point forecast / upper approximate range">${boardingsRange}</td><td class="numeric"><label class="sr-only" for="trips-${routeIndex}">Operator planned trips per day for route ${safeRoute}</label><input id="trips-${routeIndex}" class="route-plan-input" type="number" min="1" max="1000" step="1" inputmode="numeric" data-route="${safeRoute}" data-field="trips" value="${tripsValue}" /></td><td class="numeric"><label class="sr-only" for="target-${routeIndex}">Operator target boardings per trip for route ${safeRoute}</label><input id="target-${routeIndex}" class="route-plan-input" type="number" min="1" max="100000" step="1" inputmode="numeric" data-route="${safeRoute}" data-field="target" value="${targetValue}" /></td><td class="numeric" title="Daily average divided evenly by planned trips; not a peak-load estimate">${perTripRange}</td><td class="numeric" title="Trips per day calculated to keep daily average at or below your target: point / upper approximate estimate">${tripRange}</td><td><span class="planning-signal ${signalClass}">${signal}</span></td></tr>`;
  }).join("") : '<tr class="empty-row"><td colspan="8">No forecast is available yet. Train a model after adding route counts.</td></tr>';
  $("#routesReview").textContent = routes.length ? reviewCount.toLocaleString() : "—";
}
function renderModel(data) {
  const model = data.metrics?.model || {}, baseline = data.metrics?.seasonal_naive || {};
  $("#modelPageWape").textContent = percent(model.wape);
  $("#modelPageBaseline").textContent = percent(baseline.wape);
  const mw = Number(model.wape), bw = Number(baseline.wape);
  $("#modelWinner").textContent = Number.isFinite(mw) && Number.isFinite(bw) ? (mw <= bw ? "Model leads" : "Baseline leads") : "Not evaluated";
  $("#modelTrainedThrough").textContent = data.model_trained ? prettyDate(data.trained_through) : "Not trained";
  const metrics = data.metrics?.by_route || {};
  const rows = Object.entries(metrics).sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
  $("#modelRows").innerHTML = rows.length ? rows.map(([route, score]) => {
    const mwape = Number(score.model?.wape), bwape = Number(score.seasonal_naive?.wape);
    const winner = Number.isFinite(mwape) && Number.isFinite(bwape) ? (mwape <= bwape ? "Model" : "Seasonal baseline") : "Not evaluated";
    return `<tr><td><span class="route-pill">${escapeText(route)}</span></td><td class="numeric">${percent(score.model?.wape)}</td><td class="numeric">${percent(score.seasonal_naive?.wape)}</td><td>${winner}</td></tr>`;
  }).join("") : '<tr class="empty-row"><td colspan="4">Per-route validation appears after model training.</td></tr>';
}
const MONITORING_VERDICTS = { model_leads: "Model leads", baseline_leads: "Baseline leads", not_evaluated: "Not evaluated" };
const MONITORING_RECOMMENDATIONS = { hold: "Hold the current model", retrain_candidate: "Review retraining", insufficient_information: "Insufficient information" };
const MONITORING_CHECK_LABELS = { pass: "Pass", fail: "Fail", warning: "Review", unknown: "Unknown" };
const MONITORING_POLICY_FIELDS = {
  max_model_age_days: ["#monPolicyModelAge", "Maximum model age"],
  max_data_age_days: ["#monPolicyDataAge", "Maximum data age"],
  max_missing_percent: ["#monPolicyMissing", "Maximum missing data"],
  max_wape_gap: ["#monPolicyWapeGap", "Allowed WAPE gap"],
  max_holdout_degradation: ["#monPolicyHoldout", "Maximum holdout degradation"],
  distribution_shift_percent: ["#monPolicyShift", "Distribution shift threshold"],
  min_observations_per_route: ["#monPolicyCoverage", "Minimum observations per route"],
};
function formatStamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isFinite(date.valueOf()) ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date) : "—";
}
function formatScore(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
}
function formatDays(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString(undefined, { maximumFractionDigits: 1 })} day${Math.abs(number) === 1 ? "" : "s"}` : "—";
}
function renderMonitoring(payload) {
  state.monitoring = payload;
  const model = payload?.model || {}, data = payload?.data || {}, forecast = payload?.forecast || {};
  const metrics = payload?.metrics || {}, comparison = payload?.baseline_comparison || {};
  const distribution = payload?.distribution || {}, retraining = payload?.retraining || {}, coverage = payload?.training_coverage || {};
  const scores = metrics.holdout?.available ? metrics.holdout.model : metrics.selection?.model;
  const baselineScores = metrics.holdout?.available ? metrics.holdout.baseline : metrics.selection?.baseline;
  $("#monModelVersion").textContent = model.version || "Not trained";
  $("#monTrainedAt").textContent = formatStamp(model.trained_at);
  $("#monTrainedThrough").textContent = model.trained_through ? prettyDate(String(model.trained_through).slice(0, 10)) : "—";
  $("#monHorizon").textContent = model.horizon_days ? `${model.horizon_days} days` : "—";
  $("#monRoutes").textContent = model.route_count ? Number(model.route_count).toLocaleString() : "—";
  const minObservations = Number(coverage.min_observations_per_route);
  $("#monCoverage").textContent = coverage.span_days
    ? `${Number(coverage.route_count).toLocaleString()} routes · ${Number(coverage.span_days).toLocaleString()} days${Number.isFinite(minObservations) ? ` · min ${minObservations.toLocaleString()} obs/route` : ""}`
    : "No training data";
  $("#monLastObserved").textContent = data.last_observed_date ? prettyDate(data.last_observed_date) : "—";
  $("#monLastForecast").textContent = forecast.updated_at ? formatStamp(forecast.updated_at) : "No forecast";
  $("#monMae").textContent = formatScore(scores?.mae);
  $("#monRmse").textContent = formatScore(scores?.rmse);
  $("#monWape").textContent = percent(scores?.wape);
  $("#monBaselineWape").textContent = percent(baselineScores?.wape);
  $("#monVerdict").textContent = MONITORING_VERDICTS[comparison.verdict] || "Not evaluated";
  $("#monMissing").textContent = data.missing_percent === null || data.missing_percent === undefined
    ? "No data"
    : `${formatScore(data.missing_percent, 2)}% · ${formatScore(data.missing_route_days, 0)} route-days`;
  $("#monModelAge").textContent = formatDays(model.age_days);
  $("#monDataAge").textContent = formatDays(data.age_days);
  $("#monForecastAge").textContent = `Forecast ${formatDays(forecast.age_days)}`;

  const warnings = Array.isArray(payload?.warnings) ? payload.warnings : [];
  $("#monWarnings").innerHTML = warnings.length ? warnings.map((warning) => `<li class="monitoring-warning severity-${escapeText(warning.severity)}"><div><strong>${escapeText(warning.title)}</strong><span>${escapeText(warning.detail)}</span></div><em>${escapeText(warning.suggested_action)}</em></li>`).join("") : '<li class="monitoring-empty">No warnings: every configured check is within its threshold.</li>';

  const routeRows = Array.isArray(metrics.by_route) ? metrics.by_route : [];
  $("#monRouteRows").innerHTML = routeRows.length ? routeRows.map((row) => {
    const better = row.model_better === true ? "Model" : row.model_better === false ? "Seasonal baseline" : "Not evaluated";
    return `<tr><td><span class="route-pill">${escapeText(row.route)}</span></td><td class="numeric">${formatScore(row.model?.mae)}</td><td class="numeric">${formatScore(row.model?.rmse)}</td><td class="numeric">${percent(row.model?.wape)}</td><td class="numeric">${percent(row.baseline?.wape)}</td><td>${better}</td></tr>`;
  }).join("") : '<tr class="empty-row"><td colspan="6">Route error appears after training.</td></tr>';

  const horizonRows = Array.isArray(metrics.by_horizon) ? metrics.by_horizon : [];
  $("#monHorizonRows").innerHTML = horizonRows.length ? horizonRows.map((row) => `<tr><td>Day ${escapeText(row.horizon_days)}</td><td class="numeric">${formatScore(row.mae)}</td><td class="numeric">${formatScore(row.rmse)}</td><td class="numeric">${percent(row.wape)}</td></tr>`).join("") : '<tr class="empty-row"><td colspan="4">Horizon error appears after training.</td></tr>';

  const distributionRows = Array.isArray(distribution.routes) ? distribution.routes : [];
  const overall = distribution.overall;
  const distributionMarkup = distributionRows.map((row) => {
    const change = Number(row.change_percent);
    const signal = row.flagged ? '<span class="planning-signal signal-alert">Changed</span>' : '<span class="planning-signal signal-ok">Stable</span>';
    return `<tr><td><span class="route-pill">${escapeText(row.route)}</span></td><td class="numeric">${formatScore(row.prior_mean)}</td><td class="numeric">${formatScore(row.recent_mean)}</td><td class="numeric">${Number.isFinite(change) ? `${change > 0 ? "+" : ""}${formatScore(change)}%` : "—"}</td><td>${signal}</td></tr>`;
  });
  if (overall) {
    distributionMarkup.unshift(`<tr><td><strong>All routes</strong></td><td class="numeric">${formatScore(overall.prior_mean)}</td><td class="numeric">${formatScore(overall.recent_mean)}</td><td class="numeric">${Number.isFinite(Number(overall.change_percent)) ? `${Number(overall.change_percent) > 0 ? "+" : ""}${formatScore(overall.change_percent)}%` : "—"}</td><td><span class="planning-signal ${overall.flagged ? "signal-alert" : "signal-ok"}">${overall.flagged ? "Changed" : "Stable"}</span></td></tr>`);
  }
  $("#monDistributionRows").innerHTML = distributionMarkup.length ? distributionMarkup.join("") : '<tr class="empty-row"><td colspan="5">Distribution checks appear when enough observations exist.</td></tr>';

  $("#monRetrainingVerdict").textContent = MONITORING_RECOMMENDATIONS[retraining.recommendation] || "—";
  $("#monRetrainingSummary").textContent = retraining.summary || "No retraining recommendation is available yet.";
  const checks = Array.isArray(retraining.required_checks) ? retraining.required_checks : [];
  $("#monRetrainingChecks").innerHTML = checks.length ? checks.map((check) => {
    const status = MONITORING_CHECK_LABELS[check.status] || "Unknown";
    const css = check.status === "pass" ? "signal-ok" : check.status === "fail" ? "signal-alert" : "signal-near";
    return `<li class="monitoring-check"><div><strong>${escapeText(check.label)}</strong><span>${escapeText(check.detail)}</span></div><span class="planning-signal ${css}">${status}</span></li>`;
  }).join("") : '<li class="monitoring-empty">No checks are available yet.</li>';

  const policy = retraining.policy || {};
  for (const [key, [selector]] of Object.entries(MONITORING_POLICY_FIELDS)) {
    const input = $(selector);
    if (input && document.activeElement !== input && policy[key] !== undefined) input.value = String(policy[key]);
  }
}
async function loadMonitoring() {
  const note = $("#monStatusNote");
  if (!note) return;
  note.textContent = "Loading model monitoring…";
  try {
    const payload = await api("/api/monitoring");
    renderMonitoring(payload);
    note.textContent = payload.generated_at ? `Snapshot generated ${formatStamp(payload.generated_at)}. Read-only: no model was retrained.` : "Monitoring loaded.";
  } catch (error) {
    note.textContent = /access key/i.test(error.message)
      ? "Model monitoring needs an operator access key. Add it in Settings to read model health; the public demo does not expose it."
      : `Monitoring unavailable: ${error.message}`;
  }
}
async function saveMonitoringPolicy() {
  const status = $("#monPolicyStatus");
  const payload = {};
  for (const [key, [selector, label]] of Object.entries(MONITORING_POLICY_FIELDS)) {
    const raw = ($(selector)?.value || "").trim();
    if (raw === "") continue;
    const value = Number(raw);
    if (!Number.isFinite(value)) { status.textContent = `${label} must be a number.`; return; }
    payload[key] = value;
  }
  status.textContent = "Saving thresholds…";
  try {
    const result = await api("/api/monitoring/policy", { method: "PUT", body: JSON.stringify(payload) });
    if (result?.monitoring) renderMonitoring(result.monitoring);
    status.textContent = "Thresholds saved. No model was retrained.";
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  }
}
const NOTIF_SEVERITY_LABELS = { critical: "Critical", warning: "Warning", info: "Info" };
function fillNotificationPolicy(policy = {}) {
  const inApp = policy.in_app || {}, email = policy.email || {}, webhook = policy.webhook || {}, thresholds = policy.thresholds || {};
  const setValue = (selector, value) => { const node = $(selector); if (node && value !== undefined && value !== null) node.value = String(value); };
  setValue("#notifInAppEnabled", inApp.enabled ?? true);
  setValue("#notifEmailEnabled", email.enabled ?? false);
  setValue("#notifEmailProvider", email.provider || "smtp");
  setValue("#notifEmailHost", email.host || "");
  setValue("#notifEmailPort", email.port || 587);
  setValue("#notifEmailTls", email.use_tls ?? true);
  setValue("#notifEmailUser", email.username || "");
  setValue("#notifEmailUrl", email.url || "");
  setValue("#notifEmailSender", email.sender || "");
  setValue("#notifEmailRecipients", (email.recipients || []).join(", "));
  setValue("#notifWebhookEnabled", webhook.enabled ?? false);
  setValue("#notifWebhookUrl", webhook.url || "");
  setValue("#notifMinSeverity", thresholds.min_severity || "warning");
  setValue("#notifMinPassengers", thresholds.min_predicted_passengers ?? 0);
  setValue("#notifCapacityUtil", thresholds.capacity_utilization_percent ?? 90);
  setValue("#notifMaxError", thresholds.max_forecast_error_percent ?? 25);
  setValue("#notifDemandChange", thresholds.demand_change_percent ?? 30);
  setValue("#notifMaxPerRun", thresholds.max_notifications_per_run ?? 50);
  const password = $("#notifEmailPassword");
  if (password) { password.value = ""; password.placeholder = email.has_password ? "Stored — leave empty to keep" : "Stored server-side"; }
  const token = $("#notifEmailToken");
  if (token) { token.value = ""; token.placeholder = email.has_token ? "Stored — leave empty to keep" : "Stored server-side"; }
  const secret = $("#notifWebhookSecret");
  if (secret) { secret.value = ""; secret.placeholder = webhook.has_secret ? "Stored — leave empty to keep" : "Stored server-side"; }
}
function renderNotificationInbox(payload) {
  const notifications = Array.isArray(payload?.notifications) ? payload.notifications : [];
  const summary = payload?.summary || {};
  $("#notifUnacked").textContent = Number(summary.unacknowledged || 0).toLocaleString();
  $("#notifCritical").textContent = Number(summary.critical || 0).toLocaleString();
  $("#notifWarning").textContent = Number(summary.warning || 0).toLocaleString();
  $("#notifInfo").textContent = Number(summary.info || 0).toLocaleString();
  $("#notifRows").innerHTML = notifications.length ? notifications.map((item) => {
    const channels = Array.isArray(item.delivered) && item.delivered.length ? ` · ${escapeText(item.delivered.join(", "))}` : "";
    const action = item.acknowledged
      ? '<span class="planning-signal signal-ok">Acknowledged</span>'
      : `<button class="button button-quiet" type="button" data-notif-ack="${escapeText(item.id)}">Acknowledge</button>`;
    return `<li class="monitoring-warning severity-${escapeText(item.severity)}"><div><strong>${escapeText(NOTIF_SEVERITY_LABELS[item.severity] || item.severity)} · ${escapeText(item.title)}</strong><span>${escapeText(item.message)}</span><span class="notification-meta">${escapeText(item.route || "all routes")}${channels}</span></div><em>${escapeText(item.action || "Review the dashboard.")}</em>${action}</li>`;
  }).join("") : '<li class="monitoring-empty">No notifications yet. Run an evaluation to check the configured thresholds.</li>';
  fillNotificationPolicy(payload?.policy || {});
}
function collectNotificationPolicy() {
  const recipients = ($("#notifEmailRecipients").value || "").split(",").map((value) => value.trim()).filter(Boolean);
  const numberOrUndefined = (selector) => { const raw = ($(selector).value || "").trim(); if (raw === "") return undefined; const value = Number(raw); return Number.isFinite(value) ? value : undefined; };
  const payload = {
    in_app: { enabled: $("#notifInAppEnabled").value === "true" },
    email: {
      enabled: $("#notifEmailEnabled").value === "true",
      provider: $("#notifEmailProvider").value,
      host: $("#notifEmailHost").value.trim(),
      use_tls: $("#notifEmailTls").value === "true",
      username: $("#notifEmailUser").value.trim(),
      sender: $("#notifEmailSender").value.trim(),
      recipients,
      url: $("#notifEmailUrl").value.trim(),
    },
    webhook: { enabled: $("#notifWebhookEnabled").value === "true", url: $("#notifWebhookUrl").value.trim() },
    thresholds: {
      min_severity: $("#notifMinSeverity").value,
      capacity_utilization_percent: numberOrUndefined("#notifCapacityUtil"),
      min_predicted_passengers: numberOrUndefined("#notifMinPassengers"),
      max_forecast_error_percent: numberOrUndefined("#notifMaxError"),
      demand_change_percent: numberOrUndefined("#notifDemandChange"),
      max_notifications_per_run: numberOrUndefined("#notifMaxPerRun"),
    },
  };
  const port = numberOrUndefined("#notifEmailPort");
  if (port !== undefined) payload.email.port = Math.round(port);
  const password = $("#notifEmailPassword").value;
  if (password) payload.email.password = password;
  const token = $("#notifEmailToken").value;
  if (token) payload.email.token = token;
  const secret = $("#notifWebhookSecret").value;
  if (secret) payload.webhook.secret = secret;
  for (const section of ["email", "thresholds"]) {
    for (const [key, value] of Object.entries(payload[section])) if (value === undefined) delete payload[section][key];
  }
  return payload;
}
async function loadNotificationsPage() {
  const note = $("#notifStatusNote");
  if (!note) return;
  note.textContent = "Loading notifications and reports…";
  try {
    const [inbox, reportCatalog, capacity] = await Promise.all([
      api("/api/notifications"),
      api("/api/reports"),
      api("/api/reports/capacity"),
    ]);
    renderNotificationInbox(inbox);
    renderReports(reportCatalog, capacity);
    note.textContent = "Notifications, channels, and report availability loaded.";
  } catch (error) {
    note.textContent = /access key/i.test(error.message)
      ? "Notifications need an operator access key. Add it in Settings to read alerts and export reports."
      : `Notifications unavailable: ${error.message}`;
  }
}
async function evaluateNotifications() {
  const status = $("#notifRunStatus");
  status.textContent = "Evaluating configured thresholds…";
  try {
    const result = await api("/api/notifications/evaluate", { method: "POST" });
    renderNotificationInbox(await api("/api/notifications"));
    const delivery = result.delivery || {};
    const parts = [`${result.notifications.length} notification(s)`, `in-app ${delivery.in_app ?? 0}`];
    if (delivery.email?.enabled) parts.push(`email sent ${delivery.email.sent}, failed ${delivery.email.failed}`);
    if (delivery.webhook?.enabled) parts.push(`webhook sent ${delivery.webhook.sent}, failed ${delivery.webhook.failed}`);
    status.textContent = `${parts.join(" · ")}. Nothing was retrained.`;
    toast("Notification evaluation complete");
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  }
}
async function acknowledgeNotificationItem(id) {
  try {
    await api(`/api/notifications/${encodeURIComponent(id)}/acknowledge`, { method: "POST" });
    renderNotificationInbox(await api("/api/notifications"));
    toast("Notification acknowledged");
  } catch (error) {
    toast(error.message, true);
  }
}
async function saveNotificationPolicy() {
  const status = $("#notifPolicyStatus");
  status.textContent = "Saving settings…";
  try {
    const result = await api("/api/notifications/policy", { method: "PUT", body: JSON.stringify(collectNotificationPolicy()) });
    fillNotificationPolicy(result.policy);
    status.textContent = "Settings saved. Secrets stay on the server.";
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  }
}
function renderReports(payload, capacityPayload) {
  const reports = Array.isArray(payload?.reports) ? payload.reports : [];
  $("#reportsRows").innerHTML = reports.length ? reports.map((report) => {
    const status = report.available
      ? `<span class="planning-signal signal-ok">${Number(report.rows).toLocaleString()} rows</span>`
      : `<span class="planning-signal signal-muted" title="${escapeText(report.unavailable_reason || "")}">Unavailable</span><span class="report-reason">${escapeText(report.unavailable_reason || "")}</span>`;
    const actions = report.available
      ? `<button class="button button-quiet" type="button" data-report-csv="${escapeText(report.id)}">CSV ↓</button> <button class="button button-quiet" type="button" data-report-print="${escapeText(report.id)}">Print</button>`
      : "—";
    return `<tr><td><strong>${escapeText(report.title)}</strong></td><td class="report-description">${escapeText(report.description)}</td><td>${status}</td><td class="report-actions">${actions}</td></tr>`;
  }).join("") : '<tr class="empty-row"><td colspan="4">Report availability loads with the workspace.</td></tr>';
  const available = reports.filter((report) => report.available).length;
  $("#reportsStatusNote").textContent = `${available} of ${reports.length} reports available. ${payload?.notice || ""}`;
  const capacity = capacityPayload?.capacity || {};
  const editor = $("#capacityJson");
  if (editor && document.activeElement !== editor) editor.value = Object.keys(capacity).length ? JSON.stringify(capacity, null, 2) : "";
}
async function openPrintableReport(reportId) {
  const headers = new Headers();
  if (accessKey()) headers.set("Authorization", `Bearer ${accessKey()}`);
  try {
    const response = await fetch(`${apiBase()}/api/reports/${encodeURIComponent(reportId)}.html`, { headers, cache: "no-store" });
    if (!response.ok) {
      let message = `Report failed (${response.status})`;
      try { message = (await response.json()).detail || message; } catch (_) {}
      throw new Error(message);
    }
    const url = URL.createObjectURL(new Blob([await response.text()], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    toast(error.message, true);
  }
}
async function saveCapacityAssumptions() {
  const status = $("#capacityStatus");
  let parsed;
  try {
    const raw = $("#capacityJson").value.trim();
    parsed = raw ? JSON.parse(raw) : {};
  } catch (_) {
    status.textContent = "Enter valid JSON before saving.";
    return;
  }
  status.textContent = "Saving capacity assumptions…";
  try {
    await api("/api/reports/capacity", { method: "PUT", body: JSON.stringify({ capacity: parsed }) });
    renderReports(await api("/api/reports"), await api("/api/reports/capacity"));
    status.textContent = "Capacity assumptions saved.";
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  }
}
function renderDataPage(data) {
  const source = data.source === "synthetic demo data" ? "Synthetic demo" : data.source === "company upload" ? "Company upload" : data.source === "company feed" ? "Connected feed" : "No data";
  $("#dataSource").textContent = source;
  $("#dataRows").textContent = data.data_loaded ? Number(data.rows).toLocaleString() : "—";
  $("#dataRoutes").textContent = data.data_loaded ? Number(data.route_count).toLocaleString() : "—";
  $("#dataLatest").textContent = prettyDate(data.latest_observed_date);
  const checked = state.lastCheckedAt ? new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(state.lastCheckedAt) : "—";
  const received = data.data_updated_at ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(data.data_updated_at)) : "not recorded";
  const forecasted = data.forecast_updated_at ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(data.forecast_updated_at)) : "not generated";
  $("#dataFreshness").textContent = data.data_updated_at ? `Counts last received ${received}` : data.public_demo ? "Synthetic example data" : "No data receipt timestamp available";
  const sourceSystem = data.source_system ? ` Source: ${data.source_system}.` : "";
  $("#dataGuidance").textContent = `Last API check: ${checked}.${sourceSystem} Forecast last generated: ${forecasted}. The browser checks the API every 60 seconds; company feed updates arrive on the schedule configured by its owner.`;
  const labels = { upsert: "Daily counts received", replace_upload: "CSV data replaced", load_synthetic_demo: "Synthetic demo loaded", train_model: "Model trained" };
  const activity = Array.isArray(data.recent_activity) ? data.recent_activity : [];
  $("#activityRows").innerHTML = activity.length ? activity.map((event) => {
    const timestamp = event.timestamp_utc ? new Date(event.timestamp_utc) : null;
    const when = timestamp && Number.isFinite(timestamp.valueOf()) ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(timestamp) : "Time unavailable";
    const label = labels[event.action] || "Data operation";
    const detail = `${Number(event.record_count || 0).toLocaleString()} records · ${escapeText(event.source_system || "unknown source")}`;
    return `<li><span class="activity-marker"></span><div><strong>${label}</strong><small>${detail}</small></div><time>${when}</time></li>`;
  }).join("") : '<li class="activity-empty">No data updates recorded yet.</li>';
}
function renderExtraPages(data) { renderRoutes(data); renderModel(data); renderDataPage(data); }

function setPublicDemoMode(enabled, allowOperatorEdits = false) {
  state.publicDemo = enabled;
  state.operatorAccess = allowOperatorEdits;
  [$("#csvFile"), $("#trainButton"), $("#demoButton"), $("#missingDays"), $("#trainDays"), $("#saveCountButton")].forEach((node) => { if (node) node.disabled = state.busy || (enabled && !allowOperatorEdits); });
  $("#forecastDays").disabled = state.busy;
  $("#forecastRoute").disabled = state.busy;
  $("#refreshForecast").disabled = state.busy;
  $("#operatorControls").classList.toggle("hidden", enabled && !allowOperatorEdits);
  $("#publicInfo").classList.toggle("hidden", !enabled);
  $("#operatorForecastTools").classList.remove("hidden");
  $("#refreshForecast").textContent = enabled ? "Update example" : "Refresh forecast";
  $("#heroCta").href = enabled ? "#forecast-panel" : "#workflowPanel";
  $("#heroCta").innerHTML = enabled ? "Explore the forecast <span>↓</span>" : "Upload daily counts <span>→</span>";
  $("#demoBadge").classList.toggle("hidden", !enabled);
  document.body.classList.toggle("public-demo-mode", enabled);
  const sampleLink = $("#downloadSample");
  if (sampleLink) sampleLink.href = enabled ? "/api/public-demo/sample.csv" : "/api/template.csv";
  const publicSampleLink = $("#publicDownloadSample");
  if (publicSampleLink) publicSampleLink.href = "/api/public-demo/sample.csv";
  $("#downloadForecast").href = enabled ? "/api/public-demo/forecast.csv" : "/api/forecast.csv";
}
async function refreshStatus() {
  if (state.checking) return false;
  state.checking = true;
  let connected = false;
  let connectionError = "";
  try {
    if (state.opening) {
      setSplashStatus("Checking the forecast service…");
      clearTimeout(state.openingMessageTimer);
      state.openingMessageTimer = setTimeout(() => {
        if (state.opening) setSplashStatus("Waking the demo service… Free hosting can take a little longer after idle.");
      }, 8000);
    }
    if (accessKey()) {
      try {
        const operatorStatus = await api("/api/status");
        if (operatorStatus.data_loaded) {
          setPublicDemoMode(false);
          render(operatorStatus);
        } else {
          setPublicDemoMode(true, true);
          if (state.opening) setSplashStatus("Loading the synthetic sample forecast…");
          render(await api("/api/public-demo/status"));
        }
        setConnection(true);
        connected = true;
      } catch (error) {
        if (!error.message.includes("access key")) throw error;
      }
    }
    if (!connected) {
      setPublicDemoMode(true, false);
      if (state.opening) setSplashStatus("Loading the synthetic sample forecast…");
      render(await api("/api/public-demo/status"));
      setConnection(true);
      connected = true;
    }
  } catch (error) {
    setConnection(false);
    if (error.message.includes("access key")) toast(error.message, true);
    if (state.opening) {
      const savedAddress = localStorage.getItem("busForecastApiUrl");
      connectionError = savedAddress
        ? `Could not reach the saved API address (${savedAddress}). Check API access settings, then retry.`
        : "Could not reach this website’s forecast service. It may be waking up; retry, check your connection, or continue offline.";
    }
  } finally {
    state.checking = false;
    const retryButton = $("#retryOpening");
    if (retryButton) { retryButton.disabled = false; retryButton.innerHTML = "Retry connection <span>↻</span>"; }
    if (state.opening) await finishSplash(connected, connectionError);
  }
  return connected;
}
async function withBusy(label, operation) {
  if (state.busy) return;
  setBusy(true, label);
  try { const result = await operation(); if (result.message) toast(result.message); const updatedStatus = result.status || (result.public_demo ? result : null); if (updatedStatus) { setPublicDemoMode(Boolean(updatedStatus.public_demo), Boolean(updatedStatus.public_demo ? state.operatorAccess : updatedStatus.data_loaded)); render(updatedStatus); } else await refreshStatus(); }
  catch (error) {
    const message = error.message || "Something went wrong";
    if (/missing route\/date observations detected/i.test(message)) toast("This route has days without a count. Add the missing daily counts, or choose zero only for a confirmed no-service day.", true);
    else if (/need at least \d+ continuous calendar days/i.test(message)) toast("There is not enough history to train yet. Keep recording daily counts until this route has at least 118 consecutive days.", true);
    else toast(message, true);
  }
  finally { setBusy(false); }
}
async function train(options = {}) {
  const horizon = Number(options.horizon || $("#trainDays").value || 7);
  const folds = Number(options.folds || 4);
  return api("/api/train", { method: "POST", body: JSON.stringify({ horizon, folds, missing_days: $("#missingDays").value }) });
}
/* ------------------------------------------------------------------ *
 * Smart Route Explorer — observed Chicago CTA monthly boardings
 * Public read-only endpoints only; no operator key is used here.
 * ------------------------------------------------------------------ */
const MONTHLY_ENDPOINTS = {
  routes: "/api/public/monthly-data/routes",
  history: (routeId) => `/api/public/monthly-data/routes/${encodeURIComponent(routeId)}/history`,
};

async function publicJson(path) {
  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    throw Object.assign(new Error("Failed to fetch"), { status: 0 });
  }
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json().catch(() => null) : null;
  if (!response.ok) {
    throw Object.assign(new Error(payload?.detail || `Request failed (${response.status})`), { status: response.status });
  }
  return payload;
}

function monthlyBadge(text) { const node = $("#monthlyExplorerBadge"); if (node) node.textContent = text; }
function monthlyShow(target) {
  for (const id of ["monthlyExplorerLoading", "monthlyExplorerError", "monthlyExploreEmpty", "monthlyExplorerBody"]) {
    const node = $(`#${id}`);
    if (node) node.classList.toggle("hidden", id !== target);
  }
}
async function loadMonthlyDataset() {
  const monthly = state.monthly;
  if (monthly.loaded || monthly.loading) return;
  monthly.loading = true;
  monthlyShow("monthlyExplorerLoading");
  monthlyBadge("Loading routes…");
  $("#monthlySearchHint").textContent = "Loading the CTA monthly route index…";
  try {
    const routes = normalizeRoutesPayload(await publicJson(MONTHLY_ENDPOINTS.routes));
    monthly.routes = routes;
    if (!routes.length) {
      monthlyShow("monthlyExploreEmpty");
      monthlyBadge("No monthly data");
      $("#monthlySearchHint").textContent = "No monthly routes are stored on this server yet.";
      monthly.loaded = true;
      return;
    }
    monthly.loaded = true;
    const firstMonth = routes.reduce((earliest, route) => (route.first_month && (!earliest || route.first_month < earliest) ? route.first_month : earliest), null);
    const lastMonth = routes.reduce((latest, route) => (route.last_month && route.last_month > (latest || "") ? route.last_month : latest), null);
    monthlyBadge(`${routes.length.toLocaleString()} routes · ${monthLabel(firstMonth)} – ${monthLabel(lastMonth)}`);
    monthlyShow("monthlyExplorerBody");
    renderMonthlyResults();
    const initial = searchRoutes(routes, "", 1)[0];
    if (initial) await selectMonthlyRoute(initial.route_id);
  } catch (error) {
    monthlyShow("monthlyExplorerError");
    const node = $("#monthlyExplorerError");
    if (node) node.textContent = describeError(error, "dataset");
    monthlyBadge("Monthly data unavailable");
    $("#monthlySearchHint").textContent = "Monthly data could not be loaded.";
  } finally {
    monthly.loading = false;
  }
}
function renderMonthlyResults() {
  const monthly = state.monthly;
  const query = $("#monthlyRouteSearch")?.value ?? "";
  monthly.query = query;
  const results = searchRoutes(monthly.routes, query, 60);
  const list = $("#monthlyRouteResults");
  list.replaceChildren(...results.map((route) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "explorer-route";
    button.dataset.routeId = route.route_id;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(route.route_id === monthly.selected));
    const pill = document.createElement("span");
    pill.className = "route-pill";
    pill.textContent = route.route_id;
    const name = document.createElement("span");
    name.className = "explorer-route-name";
    name.textContent = route.route_name;
    const meta = document.createElement("small");
    meta.textContent = `${route.month_count.toLocaleString()} months · ${formatNumber(route.total_boardings)} boardings`;
    button.append(pill, name, meta);
    item.append(button);
    return item;
  }));
  $("#monthlyResultsEmpty").classList.toggle("hidden", results.length > 0);
  $("#monthlySearchHint").textContent = query.trim()
    ? `${results.length.toLocaleString()} matching route${results.length === 1 ? "" : "s"} for “${query.trim()}”.`
    : `Showing ${results.length.toLocaleString()} of ${monthly.routes.length.toLocaleString()} CTA bus routes, busiest first. Type a route number or name to narrow the list.`;
}
async function selectMonthlyRoute(routeId) {
  const monthly = state.monthly;
  monthly.selected = routeId;
  document.querySelectorAll("#monthlyRouteResults .explorer-route").forEach((node) => {
    node.setAttribute("aria-selected", String(node.dataset.routeId === routeId));
  });
  $("#monthlyDetailEmpty").classList.add("hidden");
  $("#monthlyDetail").classList.remove("hidden");
  $("#monthlyRoutePill").textContent = routeId;
  try {
    let history = monthly.history.get(routeId);
    if (!history) {
      $("#monthlyRouteName").textContent = "Loading…";
      history = normalizeHistoryPayload(await publicJson(MONTHLY_ENDPOINTS.history(routeId)));
      if (!history) throw Object.assign(new Error("No stored monthly records"), { status: 404 });
      monthly.history.set(routeId, history);
    }
    renderMonthlyHistory(history);
  } catch (error) {
    $("#monthlyRouteName").textContent = "Unavailable";
    $("#monthlySpan").textContent = "—";
    $("#monthlyChart").classList.add("hidden");
    const empty = $("#monthlyChartEmpty");
    empty.classList.remove("hidden");
    empty.textContent = describeError(error, "route");
    $("#monthlyTableRows").innerHTML = `<tr class="empty-row"><td colspan="3">${escapeHtml(describeError(error, "route"))}</td></tr>`;
    $("#monthlyTableCount").textContent = "—";
  }
}
function renderMonthlyChart(history) {
  const svg = $("#monthlyChart");
  const empty = $("#monthlyChartEmpty");
  const model = chartModel(history, { width: Math.max(320, svg.parentElement?.clientWidth || 640), height: 260 });
  if (!model.hasData) {
    svg.classList.add("hidden");
    svg.innerHTML = "";
    empty.classList.remove("hidden");
    empty.textContent = "No observed records for this route.";
    return;
  }
  empty.classList.add("hidden");
  svg.classList.remove("hidden");
  const colour = getComputedStyle(document.documentElement).getPropertyValue("--blue").trim() || "#8050d8";
  const { width, height, padding, segments, yTicks, xTicks } = model;
  const grid = yTicks.map((tick) => `<line x1="${padding.left}" y1="${tick.y}" x2="${width - padding.right}" y2="${tick.y}" stroke="#ece4f4"/><text x="${padding.left - 9}" y="${tick.y + 3}" text-anchor="end" fill="#877c94" font-size="9" font-family="DM Mono,monospace">${escapeHtml(tick.label)}</text>`).join("");
  const labels = xTicks.map((tick) => `<text x="${tick.x}" y="${height - 10}" text-anchor="${tick.anchor}" fill="#877c94" font-size="9" font-family="DM Mono,monospace">${escapeHtml(tick.label)}</text>`).join("");
  const paths = segments.map((segment) => `<path d="${segment.path}" fill="none" stroke="${colour}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>`).join("");
  const dots = segments.flatMap((segment) => segment.points).map((point) => `<circle cx="${point.x}" cy="${point.y}" r="3" fill="#fff" stroke="${colour}" stroke-width="2"><title>${escapeHtml(monthLabel(point.month))} · ${escapeHtml(formatNumber(point.boardings))} boardings</title></circle>`).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-label", `Observed monthly boardings for route ${history.route_id}, ${monthLabel(history.first_month)} to ${monthLabel(history.last_month)}, with months the source does not report shown as gaps`);
  svg.innerHTML = `${grid}${labels}${paths}${dots}`;
}
function renderMonthlyHistory(history) {
  const summary = summarizeRoute(history);
  if (!summary) return;
  $("#monthlyRoutePill").textContent = summary.routeId;
  $("#monthlyRouteName").textContent = summary.routeName;
  $("#monthlySpan").textContent = `${monthLabel(summary.firstMonth)} – ${monthLabel(summary.lastMonth)}`;
  $("#monthlyLatestMonth").textContent = monthLabel(summary.latestMonth);
  $("#monthlyLatest").textContent = formatNumber(summary.latestBoardings);
  $("#monthlyObserved").textContent = summary.observedMonths.toLocaleString();
  $("#monthlyNameChanges").textContent = summary.nameChanges.toLocaleString();
  renderMonthlyChart(history);
  const gaps = missingMonthRanges(history);
  $("#monthlyGaps").textContent = gaps.length
    ? `Missing months shown as gaps (${summary.missingMonths.toLocaleString()}): ${gaps.map((gap) => (gap.start === gap.end ? monthLabel(gap.start) : `${monthLabel(gap.start)} – ${monthLabel(gap.end)}`)).join(", ")}.`
    : "No missing months inside this route’s observed span.";
  $("#monthlyNameHistory").replaceChildren(...summary.nameHistory.map((segment) => {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = segment.route_name;
    const span = document.createElement("span");
    span.textContent = segment.first_month === segment.last_month
      ? monthLabel(segment.first_month)
      : `${monthLabel(segment.first_month)} – ${monthLabel(segment.last_month)}`;
    item.append(name, span);
    return item;
  }));
  const rows = history.records.slice().reverse();
  $("#monthlyTableRows").innerHTML = rows.map((record) => `<tr><td>${escapeHtml(monthLabel(record.month_start))}</td><td>${escapeHtml(record.route_name)}</td><td class="numeric passenger-count">${escapeHtml(formatNumber(record.boardings))}</td></tr>`).join("");
  $("#monthlyTableCount").textContent = `${rows.length.toLocaleString()} observed monthly records · ${monthLabel(summary.firstMonth)} – ${monthLabel(summary.lastMonth)}. Months the source does not publish are omitted, not filled.`;
}

function setup() {
  const initialView = location.hash.slice(1);
  navigate(PAGE_INFO[initialView] ? initialView : "overview", false);
  $("#mainNav").addEventListener("click", (event) => { const button = event.target.closest("[data-view]"); if (button) navigate(button.dataset.view); });
  window.addEventListener("hashchange", () => { const view = location.hash.slice(1); if (PAGE_INFO[view]) navigate(view, false); });
  $("#openDashboard").addEventListener("click", dismissSplash);
  $("#offlineOpening").addEventListener("click", dismissSplash);
  $("#retryOpening").addEventListener("click", () => {
    if (state.checking) return;
    state.opening = true;
    state.openingStartedAt = Date.now();
    $("#appSplash").classList.remove("is-error", "is-ready");
    $("#appSplash").setAttribute("aria-busy", "true");
    clearTimeout(state.openingMessageTimer);
    $("#retryOpening").classList.add("hidden");
    $("#offlineOpening").classList.add("hidden");
    $("#retryOpening").disabled = true;
    $("#retryOpening").innerHTML = "Checking… <span>↻</span>";
    setSplashStatus("Checking the forecast service…");
    refreshStatus();
  });
  $("#settingsButton").addEventListener("click", () => { $("#apiKey").value = accessKey(); $("#apiUrl").value = localStorage.getItem("busForecastApiUrl") || ""; $("#settingsDialog").showModal(); });
  $("#saveKey").addEventListener("click", () => { const key = $("#apiKey").value.trim(), rawUrl = $("#apiUrl").value.trim(); if (rawUrl) { try { const parsed = new URL(rawUrl); if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(); localStorage.setItem("busForecastApiUrl", parsed.origin); } catch (_) { toast("Enter a valid API address starting with http:// or https://", true); return; } } else localStorage.removeItem("busForecastApiUrl"); key ? localStorage.setItem("busForecastApiKey", key) : localStorage.removeItem("busForecastApiKey"); $("#settingsDialog").close(); refreshStatus(); void loadIdentity(true); });
  $("#clearKey").addEventListener("click", () => { localStorage.removeItem("busForecastApiKey"); localStorage.removeItem("busForecastApiUrl"); $("#apiKey").value = ""; $("#apiUrl").value = ""; toast("Connection reset on this device"); void loadIdentity(true); });
  $("#monSavePolicy").addEventListener("click", () => { void saveMonitoringPolicy(); });
  $("#notifSavePolicy").addEventListener("click", () => { void saveNotificationPolicy(); });
  $("#notifRun").addEventListener("click", () => { void evaluateNotifications(); });
  $("#notifRows").addEventListener("click", (event) => { const button = event.target.closest("[data-notif-ack]"); if (button) void acknowledgeNotificationItem(button.dataset.notifAck); });
  $("#reportsRows").addEventListener("click", (event) => {
    const csvButton = event.target.closest("[data-report-csv]");
    if (csvButton) { void downloadFile(`/api/reports/${csvButton.dataset.reportCsv}.csv`, `bus_demand_${csvButton.dataset.reportCsv}.csv`).catch((error) => toast(error.message, true)); return; }
    const printButton = event.target.closest("[data-report-print]");
    if (printButton) void openPrintableReport(printButton.dataset.reportPrint);
  });
  $("#capacitySave").addEventListener("click", () => { void saveCapacityAssumptions(); });
  void loadIdentity();
  $("#routeRows").addEventListener("change", (event) => {
    const input = event.target.closest(".route-plan-input");
    if (!input || !state.data) return;
    if (!input.checkValidity()) { input.reportValidity(); return; }
    const route = input.dataset.route, field = input.dataset.field;
    const plans = readRoutePlans();
    plans[route] ||= {};
    if (input.value === "") delete plans[route][field];
    else plans[route][field] = Number(input.value);
    if (!Object.keys(plans[route]).length) delete plans[route];
    try { localStorage.setItem("busForecastRoutePlans", JSON.stringify(plans)); renderRoutes(state.data); }
    catch (_) { toast("Could not save route assumptions in this browser", true); }
  });
  const exportPlanButton = $("#exportRoutePlan");
  if (exportPlanButton) exportPlanButton.addEventListener("click", exportRoutePlan);
  $("#resetRoutePlans").addEventListener("click", () => {
    try { localStorage.removeItem("busForecastRoutePlans"); if (state.data) renderRoutes(state.data); toast("Route assumptions cleared on this device"); }
    catch (_) { toast("Could not clear route assumptions", true); }
  });
  $("#trainButton").addEventListener("click", () => withBusy("Training…", () => train()));
  const countDate = $("#countDate");
  const originInput = $("#countOrigin"), destinationInput = $("#countDestination");
  if (originInput && destinationInput) {
    wirePlacePicker(originInput, $("#originSuggestions"), updateRouteSuggestions);
    wirePlacePicker(destinationInput, $("#destinationSuggestions"), updateRouteSuggestions);
    void loadRouteCatalog();
  }
  if (countDate) {
    const now = new Date();
    const localToday = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    countDate.value = localToday;
    countDate.max = localToday;
  }
  $("#quickCountForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const rawOrigin = originInput.value, rawDestination = destinationInput.value;
    const route = canonicalRoute(rawOrigin, rawDestination, state.routeCatalog);
    const { origin, destination, routeId } = route;
    const serviceDate = countDate.value, passengers = Number($("#countPassengers").value);
    if (!origin || !destination || origin.toLocaleLowerCase() === destination.toLocaleLowerCase()) {
      toast("Enter two different places for this route", true);
      return;
    }
    if (!serviceDate || serviceDate > countDate.max || !Number.isInteger(passengers) || passengers < 0) {
      toast("Check the service date and enter a whole passenger count of zero or more", true);
      return;
    }
    withBusy("Saving count…", async () => {
      const result = await api("/api/data/ingest", {
        method: "POST",
        body: JSON.stringify({ source_system: "manual_daily_entry", missing_days: $("#missingDays").value, records: [{ service_date: serviceDate, route_id: routeId, passengers }] })
      });
      const action = Number(result.inserted_records) === 1 ? "new count saved" : Number(result.updated_records) === 1 ? "existing date updated" : "count saved";
      result.message = `${action}: ${passengers.toLocaleString()} boardings for ${routeId} on ${serviceDate}. ${result.message || ""}`.trim();
      originInput.value = origin;
      destinationInput.value = destination;
      $("#countPassengers").value = "";
      updateRouteSuggestions();
      return result;
    });
  });
  $("#demoButton").addEventListener("click", () => withBusy("Running demo…", async () => { await api("/api/data/demo", { method: "POST" }); return train({ horizon: 7, folds: 4 }); }));
  $("#refreshForecast").addEventListener("click", () => withBusy(state.publicDemo ? "Updating example…" : "Refreshing…", () => state.publicDemo
    ? api(`/api/public-demo/status?days=${Number($("#forecastDays").value)}`)
    : api("/api/forecast", { method: "POST", body: JSON.stringify({ days: Number($("#forecastDays").value), missing_days: $("#missingDays").value }) })));
  $("#forecastRoute").addEventListener("change", () => { if (state.data) { renderChart(state.data.forecasts); renderTable(state.data.forecasts); } });
  $("#downloadForecast").addEventListener("click", (event) => { event.preventDefault(); const publicDemo = state.publicDemo; let path = publicDemo ? `/api/public-demo/forecast.csv?days=${Number($("#forecastDays").value)}` : "/api/forecast.csv"; if ($("#forecastRoute").value !== "all") path += `${path.includes("?") ? "&" : "?"}route_id=${encodeURIComponent($("#forecastRoute").value)}`; downloadFile(path, publicDemo ? "bus_demand_synthetic_demo_forecast.csv" : "bus_demand_forecast.csv").catch((error) => toast(error.message, true)); });
  $("#downloadSample").addEventListener("click", (event) => { event.preventDefault(); const publicDemo = state.publicDemo; downloadFile(publicDemo ? "/api/public-demo/sample.csv" : "/api/template.csv", publicDemo ? "synthetic_daily_route_counts.csv" : "daily_route_counts_sample.csv").catch((error) => toast(error.message, true)); });
  $("#forecastDays").addEventListener("change", () => { if (state.publicDemo || state.data?.model_trained) $("#refreshForecast").click(); });
  $("#csvFile").addEventListener("change", (event) => { const file = event.target.files?.[0]; if (!file) return; withBusy("Uploading…", async () => { const form = new FormData(); form.append("file", file); form.append("missing_days", $("#missingDays").value); return api("/api/data/upload", { method: "POST", body: form }); }).finally(() => { event.target.value = ""; }); });
  const drop = $("#dropzone");
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (event) => { const file = event.dataTransfer.files?.[0]; if (!file) return; const input = $("#csvFile"); const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true })); });
  const monthlySearch = $("#monthlyRouteSearch");
  if (monthlySearch) {
    monthlySearch.addEventListener("input", () => { if (state.monthly.loaded) renderMonthlyResults(); });
    $("#monthlySearchForm").addEventListener("submit", (event) => { event.preventDefault(); if (state.monthly.loaded) renderMonthlyResults(); });
    $("#monthlyRouteClear").addEventListener("click", () => {
      monthlySearch.value = "";
      if (state.monthly.loaded) renderMonthlyResults();
      monthlySearch.focus();
    });
    $("#monthlyRouteResults").addEventListener("click", (event) => {
      const button = event.target.closest(".explorer-route");
      if (button && state.monthly.loaded) void selectMonthlyRoute(button.dataset.routeId);
    });
  }
  window.addEventListener("resize", () => {
    if (state.data) renderChart(state.data.forecasts);
    const history = state.monthly.history.get(state.monthly.selected);
    if (history && state.view === "explorer") renderMonthlyChart(history);
  });
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); state.installPrompt = event; $("#installButton").classList.remove("hidden"); });
  $("#installButton").addEventListener("click", async () => { if (!state.installPrompt) return; state.installPrompt.prompt(); await state.installPrompt.userChoice; state.installPrompt = null; $("#installButton").classList.add("hidden"); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  refreshStatus();
  window.setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, 60000);
}

import { buildGtfsNetworkState, gtfsRouteSearch, gtfsStopSequence, routeTypeLabel } from "./gtfs_network.mjs";

const GTFS_AGENCY_ENDPOINT = "/api/gtfs/agencies";
const GTFS_ROUTES_ENDPOINT = (agencyCode) => `/api/gtfs/routes?agency=${encodeURIComponent(agencyCode)}`;
const GTFS_ROUTE_DETAIL_ENDPOINT = (agencyCode, routeId) => `/api/gtfs/routes/${encodeURIComponent(routeId)}?agency=${encodeURIComponent(agencyCode)}`;

function gtfsNetworkBadge(text){ const node=$("#gtfsNetworkBadge"); if(node) node.textContent=text; }
function gtfsShow(target){ for(const id of ["gtfsNetworkLoading","gtfsNetworkError","gtfsNetworkEmpty","gtfsNetworkBody"]){ const n=$(`#${id}`); if(n) n.classList.toggle("hidden", id!==target); } }

function normalizeGtfsAgencies(payload){
  const list=Array.isArray(payload?.agencies)?payload.agencies:[];
  return list.filter((a)=>a?.agency_code && a.route_count!=null).map((a)=>({agency_code:String(a.agency_code),route_count:Number(a.route_count),stop_count:Number(a.stop_count)}));
}
function countAgencies(list){ return new Set(list.map(a=>a.agency_code)).size; }
function countRoutes(list){ return list.reduce((total,a)=>total+(a.route_count||0),0); }

function renderGtfsAgencyList(agencies){
  const list=$("#gtfsAgencySearch");
  if(!list) return;
  while(list.nextElementSibling && list.nextElementSibling.tagName==="DATALIST") list.nextElementSibling.remove();
  const datalist=document.createElement("datalist");
  datalist.id="gtfsAgencyList";
  datalist.replaceChildren(...agencies.map((a)=>{const o=document.createElement("option");o.value=a.agency_code;return o;}));
  document.head.appendChild(datalist);
  list.setAttribute("list","gtfsAgencyList");
}

function renderGtfsRoutes(){
  const g=state.gtfs;
  const q=$("#gtfsRouteQuery")?.value ?? "";
  g.query=q;
  const results=gtfsRouteSearch(g.routes,q,120);
  const ul=$("#gtfsRouteResults");
  if(!ul) return;
  ul.replaceChildren(...results.map((route)=>{
    const li=document.createElement("li");
    const b=document.createElement("button");
    b.type="button";
    b.className="explorer-route";
    b.dataset.routeId=route.route_id;
    b.setAttribute("role","option");
    b.setAttribute("aria-selected",String(route.route_id===g.selected));
    const pill=document.createElement("span"); pill.className="route-pill"; pill.textContent=route.route_id;
    const name=document.createElement("span"); name.className="explorer-route-name"; name.textContent=route.long_name||route.short_name||route.route_id;
    const meta=document.createElement("small"); meta.textContent=`${route.stop_count?.toLocaleString()||0} stops · ${route.trip_count?.toLocaleString()||0} trips`;
    b.append(pill,name,meta);
    li.appendChild(b);
    return li;
  }));
  $("#gtfsRoutesEmpty").classList.toggle("hidden",results.length>0);
  $("#gtfsNetworkHint").textContent=q.trim()
    ?`${results.length.toLocaleString()} matching route${results.length===1?"":"s"} for “${q.trim()}”.`
    :`Showing ${results.length.toLocaleString()} of ${g.routes.length.toLocaleString()} routes, busiest first. Type a route number or name to narrow the list.`;
}

async function loadGtfsNetwork(){
  const g=state.gtfs;
  g.loading=true;
  gtfsShow("gtfsNetworkLoading");
  gtfsNetworkBadge("Loading network…");
  $("#gtfsNetworkHint").textContent="Loading the route network…";
  try{
    g.agencies=await normalizeGtfsAgencies(await publicJson(GTFS_AGENCY_ENDPOINT));
    if(!g.agencies.length){
      gtfsShow("gtfsNetworkEmpty");
      gtfsNetworkBadge("No network");
      $("#gtfsNetworkHint").textContent="No GTFS routes are stored on this server yet.";
      g.loaded=true;
      return;
    }
    g.loaded=true;
    gtfsNetworkBadge(`${countAgencies(g.agencies)} agency${g.agencies.length===1?"":"ies"} · ${countRoutes(g.agencies)} routes`);
    gtfsShow("gtfsNetworkBody");
    renderGtfsAgencyList(g.agencies);
    g.agency=g.agency || g.agencies[0].agency_code;
    $("#gtfsAgencySearch").value=g.agency;
    await selectGtfsAgency(g.agency);
  }catch(error){
    gtfsShow("gtfsNetworkError");
    const n=$("#gtfsNetworkError"); if(n) n.textContent=describeError(error,"dataset");
    gtfsNetworkBadge("Network unavailable");
    $("#gtfsNetworkHint").textContent="Route network could not be loaded.";
  }finally{
    g.loading=false;
  }
}

async function selectGtfsAgency(agencyCode){
  const g=state.gtfs;
  g.agency=agencyCode;
  g.selected=null;
  g.stopsByRoute.clear();
  $("#gtfsAgencyPill").textContent=agencyCode;
  try{
    g.routes=await gtfsNormalizeRoutes(await publicJson(GTFS_ROUTES_ENDPOINT(agencyCode)));
    if(!g.routes.length){
      gtfsShow("gtfsNetworkEmpty");
      $("#gtfsNetworkHint").textContent="No routes for this agency.";
      return;
    }
    gtfsShow("gtfsNetworkBody");
    renderGtfsRoutes();
    const initial=gtfsRouteSearch(g.routes,"",1)[0];
    if(initial) await selectGtfsRoute(initial.route_id);
  }catch(error){
    gtfsShow("gtfsNetworkError");
    const n=$("#gtfsNetworkError"); if(n) n.textContent=describeError(error,"dataset");
  }
}

function gtfsNormalizeRoutes(payload){
  const g=state.gtfs;
  const list=Array.isArray(payload?.routes)?payload.routes:[];
  g.routes=list.filter((r)=>r?.route_id && r.route_id.trim()).map((r)=>{
    const stops=(Array.isArray(r.stops)?r.stops:[]).map((s)=>({stop_id:String(s.stop_id),name:String(s.name),latitude:Number(s.latitude),longitude:Number(s.longitude),sequence:Number(s.sequence)}));
    const byStop=new Map(stops.map(s=>[s.stop_id,s]));
    return {
      agency_code:r.agency_code, route_id:String(r.route_id), short_name:r.short_name, long_name:r.long_name,
      route_type:Number(r.route_type)||null, color:String(r.color)||null,
      stop_count:byStop.size,
      trip_count:Number(r.trip_count)||0,
      stops:stops.sort((a,b)=>a.sequence-b.sequence),
      stopsByStop:byStop,
    };
  });
  return g.routes;
}

async function selectGtfsRoute(routeId){
  const g=state.gtfs;
  g.selected=routeId;
  $("#gtfsRouteResults .explorer-route").forEach((n)=>n.setAttribute("aria-selected",String(n.dataset.routeId===routeId)));
  $("#gtfsDetailEmpty").classList.add("hidden");
  $("#gtfsDetail").classList.remove("hidden");
  $("#gtfsRouteId").textContent=routeId;
  let route=g.routes.find((r)=>r.route_id===routeId) || g.stopsByRoute.get(routeId);
  if(!route){
    $("#gtfsRouteName").textContent="Loading…";
    try{
      const detail=await gtfsNormalizeRoutes(await publicJson(GTFS_ROUTE_DETAIL_ENDPOINT(g.agency,routeId)));
      g.stopsByRoute.set(routeId,detail);
      route=detail.find((r)=>r.route_id===routeId);
    }catch(error){
      $("#gtfsRouteName").textContent="Unavailable";
      $("#gtfsSpan").textContent="—";
      const empty=$("#gtfsStopsEmpty"); empty.classList.remove("hidden"); empty.textContent=describeError(error,"route");
      $("#gtfsStopsList").innerHTML=`<li class="empty-row"><td colspan="3">${escapeHtml(describeError(error,"route"))}</td></tr>`;
      $("#gtfsGeometryNote").textContent="—";
      return;
    }
  }
  if(!route){
    $("#gtfsRouteName").textContent="—";
    $("#gtfsSpan").textContent="—";
    return;
  }
  $("#gtfsRouteName").textContent=route.long_name||route.short_name||route.route_id;
  $("#gtfsRouteType").textContent=routeTypeLabel(route.route_type);
  $("#gtfsRouteStops").textContent=(route.stop_count??0).toLocaleString();
  $("#gtfsSpan").textContent=route.trip_count?.toLocaleString()+" trips";
  $("#gtfsGeometryNote").textContent="Stop sequence comes from the imported feed; no positions are inferred.";
  const ol=$("#gtfsStopsList");
  if(!ol) return;
  const stops=gtfsStopSequence(route.stops);
  if(!stops.length){
    ol.innerHTML=`<li class="empty-row"><td colspan="3">No stops for this route.</td></li>`;
    return;
  }
  ol.replaceChildren(...stops.map((s)=>{
    const li=document.createElement("li");
    const seq=document.createElement("span"); seq.className="seq"; seq.textContent=String(s.sequence==null?"—":s.sequence);
    const name=document.createElement("span"); name.className="stop-name"; name.textContent=s.name||"—";
    const coords=document.createElement("span"); coords.className="stop-coords";
    coords.textContent=(s.latitude!=null&&isFinite(s.latitude)&&isFinite(s.longitude))
      ?`${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}`
      :"—";
    li.append(seq,name,coords);
    return li;
  }));
}

$(document).on("input", "#gtfsRouteQuery", ()=>{ if(state.gtfs.loaded) renderGtfsRoutes(); });
$(document).on("submit", "#gtfsRouteSearchForm", (event)=>{ event.preventDefault(); if(state.gtfs.loaded) renderGtfsRoutes(); });
$(document).on("click", "#gtfsRouteClear", ()=>{ $("#gtfsRouteQuery").value=""; if(state.gtfs.loaded) renderGtfsRoutes(); $("#gtfsRouteQuery").focus(); });
$(document).on("click", "#gtfsRouteResults", (event)=>{ const button=event.target.closest(".explorer-route"); if(button && state.gtfs.loaded) void selectGtfsRoute(button.dataset.routeId); });
$(document).on("input", "#gtfsAgencySearch", ()=>{ if(state.gtfs.loaded){ state.gtfs.agency=$("#gtfsAgencySearch").value.trim()||state.gtfs.agencies[0]?.agency_code; void selectGtfsAgency(state.gtfs.agency); } });
window.addEventListener("resize", ()=>{ const route=state.gtfs.stopsByRoute.get(state.gtfs.selected); if(route) renderGtfsStopList(route); });

function renderGtfsStopList(route){
  const ol=$("#gtfsStopsList");
  if(!ol) return;
  const stops=gtfsStopSequence(route.stops);
  if(!stops.length){ ol.innerHTML=`<li class="empty-row"><td colspan="3">No stops for this route.</td></li>`; return; }
  ol.replaceChildren(...stops.map((s)=>{
    const li=document.createElement("li");
    const seq=document.createElement("span"); seq.className="seq"; seq.textContent=String(s.sequence==null?"—":s.sequence);
    const name=document.createElement("span"); name.className="stop-name"; name.textContent=s.name||"—";
    const coords=document.createElement("span"); coords.className="stop-coords";
    coords.textContent=(s.latitude!=null&&isFinite(s.latitude)&&isFinite(s.longitude))
      ?`${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}`
      :"—";
    li.append(seq,name,coords);
    return li;
  }));
}

;(() => {
  if(!PAGE_INFO.routes) return;
  navigate(PAGE_INFO.routes[0], false);
  $("#routesPage-v2").addEventListener("click", (e)=>{ if(state.gtfs.loaded) void loadGtfsNetwork(); });
})();
document.addEventListener("DOMContentLoaded", setup);


