import { canonicalRoute, matchDestinations, normalizePlaceLabel, placeSuggestions } from "./route_catalog.mjs";
import { buildMonthlyRouteChartModel } from "./monthly_route_chart.mjs";

const $ = (selector) => document.querySelector(selector);
const state = { data: null, installPrompt: null, busy: false, checking: false, toastTimer: null, publicDemo: false, lastCheckedAt: null, view: "overview", opening: true, openingStartedAt: Date.now(), openingTimer: null, openingMessageTimer: null, routeCatalog: null, monthlySearchTimer: null, monthlySearchRequest: 0, monthlyHistoryRequest: 0, selectedMonthlyRoute: null };

let operatorKeyInMemory = "";
function accessKey() { return operatorKeyInMemory; }
function apiBase() { return window.location.origin; }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (accessKey()) headers.set("Authorization", `Bearer ${accessKey()}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), path === "/health" || path === "/api/health" ? 9000 : 90000);
  try {
    const response = await fetch(`${apiBase()}${path}`, { ...options, headers, signal: controller.signal, cache: "no-store" });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) throw new Error(payload?.message || payload?.detail || payload || `Request failed (${response.status})`);
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("The forecast service took too long to respond. Retry or continue to the dashboard.");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}
async function publicMonthlyApi(path) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`${apiBase()}${path}`, { headers: { Accept: "application/json" }, signal: controller.signal, cache: "no-store" });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const error = new Error(payload?.detail || payload || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("Route data took too long to load. Check the connection and retry.");
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
  [$("#csvFile"), $("#trainButton"), $("#demoButton"), $("#saveCountButton")].forEach((node) => { if (node) node.disabled = busy || (state.publicDemo && !state.operatorAccess); });
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
    target.innerHTML = '<tr class="empty-row"><td colspan="4">A forecast appears after 28 continuous daily counts per route.</td></tr>';
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
  $("#modelCompare").textContent = Number.isFinite(ml) && Number.isFinite(baseline) ? `Baseline ${percent(baseline)} · ${ml <= baseline ? "model leads" : "baseline leads"}` : data.forecast_mode === "weekday_average" ? "Weekday baseline active · ML not trained" : "Rolling time based validation";
  $("#horizonLabel").textContent = data.forecasts?.length ? `${new Set(data.forecasts.map((row) => row.service_date)).size} days` : "—";
  const updatedAt = data.forecast_mode === "weekday_average" ? data.forecast_updated_at : data.model_updated_at || data.forecast_updated_at;
  const modelUpdated = updatedAt ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(updatedAt)) : "";
  $("#modelState").textContent = data.forecast_mode === "weekday_average" ? `Weekday-average baseline · updated ${modelUpdated || "just now"}` : data.model_trained ? `ML trained through ${prettyDate(data.trained_through)} · updated ${modelUpdated || "time unavailable"}` : "No model trained";
  const source = ({ "synthetic demo data": "Synthetic sample data", "synthetic schedule demo data": "Synthetic timetable scenario", "company upload": "Operator data uploaded", "company feed": "Daily counts entered or imported" })[data.source] || "Waiting for data";
  $("#sourceLabel").textContent = source;
  $("#updatedLabel").textContent = data.forecast_updated_at ? `Updated ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(data.forecast_updated_at))}` : "No forecast yet";
  $("#workflowNote").textContent = state.publicDemo ? (state.operatorAccess ? "You are viewing a read-only SYNTHETIC preview. Operator data may be present, but it has not produced a forecast yet. Synthetic values are never mixed with your records." : "This read-only preview uses SYNTHETIC passenger counts. An authorized operator key is required to upload data or train a model.") : data.source === "synthetic schedule demo data" ? "This timetable-based scenario is synthetic, not observed passenger demand. Replace it with actual passenger counts before operational use." : data.source === "synthetic demo data" ? "Demo numbers are synthetic. Replace them with approved company data before operational use." : data.forecast_mode === "weekday_average" ? "Forecast uses each route's recent same-weekday average. It is a transparent baseline, not ML. The ML history requirement depends on the selected forecast length." : "Forecasts refresh when daily counts arrive. Upload only data approved for this environment.";
  syncForecastRoutes(data.forecasts);
  renderChart(data.forecasts);
  renderTable(data.forecasts);
  renderExtraPages(data);
}

const PAGE_INFO = {
  overview: ["Start here", "Choose the example or record a daily passenger count."],
  forecasts: ["Forecasts", "Explore the demand outlook and download route-level estimates."],
  routes: ["Routes", "Search route history and compare forecast totals."],
  network: ["Transit Network", "Inspect transit agencies, route alignments, schedules, and stops."],
  operations: ["Live Operations", "Monitor real-time vehicle positions, delays, bunching alerts, and dispatch actions."],
  planner: ["Service Planner", "Simulate frequency adjustments and capacity buffers against forecast demand."],
  occupancy: ["Occupancy & Capacity", "Inspect stop-level passenger conservation loads and overcapacity warnings."],
  data: ["My data", "See which passenger counts are saved and when they were updated."],
  model: ["How it works", "A plain-language guide to the data and forecast."]
};

function formatMonth(value) {
  const match = /^(\d{4})-(\d{2})-01$/.exec(String(value || ""));
  if (!match) return String(value || "—");
  return new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1)));
}
function formatMonthlyBoardings(value) {
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(amount) : "—";
}
function setMonthlySearchMessage(message, stateName = "info") {
  const status = $("#monthlyRouteSearchStatus");
  status.textContent = message;
  status.dataset.state = stateName;
}
function setMonthlyHistoryState(stateName, message = "") {
  $("#monthlyRouteHistoryLoading").hidden = stateName !== "loading";
  $("#monthlyRouteHistoryError").hidden = stateName !== "error";
  $("#monthlyRouteHistoryError").textContent = stateName === "error" ? message : "";
  $("#monthlyRouteHistoryEmpty").hidden = stateName !== "empty";
  $("#monthlyRouteHistory").hidden = stateName !== "ready";
}
function addSvgElement(svg, name, attributes = {}, text = "") {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text) element.textContent = text;
  svg.append(element);
  return element;
}
function renderMonthlyRouteChart(records, routeId) {
  const svg = $("#monthlyRouteChart");
  const model = buildMonthlyRouteChartModel(records);
  const width = Math.max(320, svg.parentElement.clientWidth || 640);
  const height = 260;
  const pad = { left: 56, right: 16, top: 22, bottom: 43 };
  const span = model.last_month_index - model.first_month_index;
  const max = model.max_boardings > 0 ? model.max_boardings * 1.12 : 1;
  const x = (point) => pad.left + (span ? (point.month_index - model.first_month_index) / span : 0.5) * (width - pad.left - pad.right);
  const y = (value) => height - pad.bottom - (value / max) * (height - pad.top - pad.bottom);
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-label", `Observed monthly boardings for route ${routeId}. Gaps are left blank; no monthly values are filled or estimated.`);
  addSvgElement(svg, "title", {}, `Monthly boardings for ${routeId}`);
  addSvgElement(svg, "desc", {}, "Each point is an observed monthly record. Lines break where one or more months have no observation.");
  const gridColor = getComputedStyle(document.documentElement).getPropertyValue("--line").trim() || "#e5dfed";
  const textColor = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#716b83";
  const chartColor = getComputedStyle(document.documentElement).getPropertyValue("--blue").trim() || "#7851c8";
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = max * (tick / 4), yy = y(value);
    addSvgElement(svg, "line", { x1: pad.left, y1: yy, x2: width - pad.right, y2: yy, stroke: gridColor });
    addSvgElement(svg, "text", { x: pad.left - 8, y: yy + 3, "text-anchor": "end", fill: textColor, "font-size": 10 }, formatMonthlyBoardings(value));
  }
  for (const segment of model.segments) {
    if (segment.length < 2) continue;
    const path = segment.map((point, index) => `${index ? "L" : "M"}${x(point).toFixed(1)},${y(point.boardings).toFixed(1)}`).join(" ");
    addSvgElement(svg, "path", { d: path, fill: "none", stroke: chartColor, "stroke-width": 2.5, "stroke-linecap": "round", "stroke-linejoin": "round" });
  }
  const labelStep = Math.max(1, Math.ceil(model.points.length / 7));
  model.points.forEach((point, index) => {
    const circle = addSvgElement(svg, "circle", { cx: x(point), cy: y(point.boardings), r: 4, fill: "#fff", stroke: chartColor, "stroke-width": 2 });
    addSvgElement(circle, "title", {}, `${formatMonth(point.month_start)} · ${formatMonthlyBoardings(point.boardings)} monthly boardings`);
    if (index % labelStep === 0 || index === model.points.length - 1) {
      addSvgElement(svg, "text", { x: x(point), y: height - 13, "text-anchor": "middle", fill: textColor, "font-size": 10 }, formatMonth(point.month_start));
    }
  });
}
function renderMonthlyRouteHistory(history) {
  const records = Array.isArray(history.records) ? history.records : [];
  if (!records.length) throw new Error("No observed monthly boarding records are available for this route.");
  $("#monthlyRouteTitle").textContent = `${history.route_id} · ${history.route_name || "Route name unavailable"}`;
  $("#monthlyRouteObserved").textContent = `${Number(history.observed_months).toLocaleString()} observed months`;
  $("#monthlyRouteLatest").textContent = `${formatMonth(history.last_month)} · ${formatMonthlyBoardings(records.at(-1).boardings)} boardings`;
  const names = Array.isArray(history.historical_route_names) ? history.historical_route_names : [];
  $("#monthlyRouteNames").textContent = names.length > 1 ? `Route names recorded over time: ${names.join(" · ")}` : names.length ? `Route name: ${names[0]}` : "No route name is recorded.";
  $("#monthlyRouteGapNote").textContent = history.missing_months > 0
    ? `${Number(history.missing_months).toLocaleString()} month${history.missing_months === 1 ? " is" : "s are"} missing within this route’s observed date range. Gaps remain blank; no zero values or estimates are added.`
    : "No missing months within this route’s observed date range. Every plotted point is an observed monthly record.";
  renderMonthlyRouteChart(records, history.route_id);

  const tbody = $("#monthlyRouteHistoryRows");
  tbody.replaceChildren();
  for (const record of records) {
    const row = document.createElement("tr");
    for (const value of [formatMonth(record.month_start), record.route_name || "—", formatMonthlyBoardings(record.boardings)]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    row.lastElementChild.classList.add("numeric");
    tbody.append(row);
  }
  setMonthlyHistoryState("ready");
}
async function selectMonthlyRoute(route) {
  const requestId = ++state.monthlyHistoryRequest;
  state.selectedMonthlyRoute = route.route_id;
  document.querySelectorAll(".monthly-route-result").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.routeId === route.route_id)));
  setMonthlyHistoryState("loading");
  try {
    const history = await publicMonthlyApi(`/api/public/monthly-data/routes/${encodeURIComponent(route.route_id)}/history`);
    if (requestId !== state.monthlyHistoryRequest) return;
    renderMonthlyRouteHistory(history);
  } catch (error) {
    if (requestId !== state.monthlyHistoryRequest) return;
    const message = error.status === 404
      ? "Monthly history for this route is unavailable. Refresh the search and select a listed route."
      : "Could not load route history. Check the connection and try again.";
    setMonthlyHistoryState("error", message);
  }
}
function appendMonthlyRouteResult(list, route) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "monthly-route-result";
  button.dataset.routeId = route.route_id;
  button.setAttribute("aria-pressed", String(state.selectedMonthlyRoute === route.route_id));
  const heading = document.createElement("strong");
  heading.textContent = `${route.route_id} · ${route.route_name || "Route name unavailable"}`;
  const summary = document.createElement("span");
  summary.textContent = `Latest ${formatMonth(route.latest_month)} · ${formatMonthlyBoardings(route.latest_boardings)} boardings · ${Number(route.observed_months).toLocaleString()} observed months`;
  button.append(heading, summary);
  button.addEventListener("click", () => { void selectMonthlyRoute(route); });
  list.append(button);
}
async function searchMonthlyRoutes(query = "") {
  const requestId = ++state.monthlySearchRequest;
  const trimmed = query.trim();
  const list = $("#monthlyRouteResults");
  list.replaceChildren();
  setMonthlySearchMessage("Searching monthly route records…", "loading");
  try {
    const params = new URLSearchParams({ limit: "25" });
    if (trimmed) params.set("q", trimmed);
    const result = await publicMonthlyApi(`/api/public/monthly-data/routes?${params}`);
    if (requestId !== state.monthlySearchRequest) return;
    const routes = Array.isArray(result.routes) ? result.routes : [];
    if (!routes.length) {
      setMonthlySearchMessage(trimmed ? "No routes matched this search. Try a route ID or another name." : "No monthly route data is available yet. Route data has not yet been imported.", "empty");
      return;
    }
    for (const route of routes) appendMonthlyRouteResult(list, route);
    setMonthlySearchMessage(`${Number(result.total_matches).toLocaleString()} matching route${result.total_matches === 1 ? "" : "s"}. Select a route to inspect its monthly history.`, "ready");
  } catch (error) {
    if (requestId !== state.monthlySearchRequest) return;
    const message = error.status === 404
      ? "The monthly Route Explorer is not enabled on this service yet."
      : "Could not connect to monthly route data. Check the connection and retry.";
    setMonthlySearchMessage(message, "error");
  }
}
function setupMonthlyRouteExplorer() {
  const search = $("#monthlyRouteSearch");
  $("#monthlyRouteSearchForm").addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(state.monthlySearchTimer); void searchMonthlyRoutes(search.value); });
  search.addEventListener("input", () => {
    clearTimeout(state.monthlySearchTimer);
    state.monthlySearchTimer = setTimeout(() => { void searchMonthlyRoutes(search.value); }, 250);
  });
  $("#monthlyRouteRetry").addEventListener("click", () => { void searchMonthlyRoutes(search.value); });
}

let networkInitialized = false;

async function initNetworkExplorer() {
  if (networkInitialized) return;
  networkInitialized = true;
  await loadNetworkAgenciesAndFeeds();
  await filterNetworkRoutes();
}

async function loadNetworkAgenciesAndFeeds() {
  try {
    const feeds = await api("/api/gtfs/feeds").catch(() => []);
    const badge = $("#networkFeedBadge");
    if (badge) {
      if (Array.isArray(feeds) && feeds.length > 0) {
        const active = feeds.find((f) => f.is_active) || feeds[0];
        badge.textContent = `${active.agency_id || "GTFS"} SCHEDULE · ACTIVE`;
        badge.classList.remove("error");
      } else {
        badge.textContent = "NO SCHEDULE LOADED";
      }
    }

    const agencies = await api("/api/gtfs/agencies").catch(() => []);
    const select = $("#networkAgencySelect");
    if (select && Array.isArray(agencies)) {
      const prev = select.value;
      select.innerHTML = '<option value="">All Agencies</option>' +
        agencies.map((a) => `<option value="${escapeText(a.agency_id)}">${escapeText(a.agency_name || a.agency_id)}</option>`).join("");
      if (prev) select.value = prev;
    }
  } catch (err) {
    console.warn("Failed to load GTFS feeds/agencies", err);
  }
}

async function filterNetworkRoutes() {
  const search = $("#networkRouteSearch")?.value || "";
  const agencyId = $("#networkAgencySelect")?.value || "";
  const status = $("#networkRouteStatus");
  const list = $("#networkRouteList");
  if (status) status.textContent = "Loading routes…";

  try {
    let path = "/api/gtfs/routes?";
    if (agencyId) path += `agency_id=${encodeURIComponent(agencyId)}&`;
    if (search) path += `search=${encodeURIComponent(search)}&`;

    const routes = await api(path);
    if (!Array.isArray(routes) || routes.length === 0) {
      if (status) status.textContent = "No transit routes found matching your criteria.";
      if (list) list.innerHTML = '<p class="monthly-route-empty">No routes found.</p>';
      return;
    }

    if (status) status.textContent = `Found ${routes.length} transit routes. Select a route to view schedule & stops.`;
    if (list) {
      list.innerHTML = routes.map((r) => `
        <button class="monthly-route-item" type="button" data-route-id="${escapeText(r.route_id)}">
          <div class="monthly-route-item-head">
            <span class="route-pill">${escapeText(r.route_short_name || r.route_id)}</span>
            <small style="color: var(--muted);">${r.trips_count || 0} trips</small>
          </div>
          <div class="monthly-route-item-name">${escapeText(r.route_long_name || r.route_short_name || r.route_id)}</div>
        </button>
      `).join("");

      list.querySelectorAll("[data-route-id]").forEach((btn) => {
        btn.addEventListener("click", () => {
          list.querySelectorAll(".is-selected").forEach((el) => el.classList.remove("is-selected"));
          btn.classList.add("is-selected");
          void selectNetworkRoute(btn.dataset.routeId);
        });
      });
    }
  } catch (err) {
    if (status) status.textContent = "Failed to load routes: " + err.message;
  }
}

function renderRouteShapeSvg(points) {
  const svg = $("#networkShapePreview");
  const countLabel = $("#networkShapeCount");
  if (!svg) return;

  if (!Array.isArray(points) || points.length === 0) {
    if (countLabel) countLabel.textContent = "No alignment data";
    svg.innerHTML = '<text x="200" y="105" text-anchor="middle" fill="#877c94" font-size="12">No geographic alignment shape for this route</text>';
    return;
  }

  if (countLabel) countLabel.textContent = `${points.length} GPS vertices`;

  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (const pt of points) {
    if (pt.shape_pt_lat < minLat) minLat = pt.shape_pt_lat;
    if (pt.shape_pt_lat > maxLat) maxLat = pt.shape_pt_lat;
    if (pt.shape_pt_lon < minLon) minLon = pt.shape_pt_lon;
    if (pt.shape_pt_lon > maxLon) maxLon = pt.shape_pt_lon;
  }

  const pad = 24;
  const w = 400, h = 200;
  const latSpan = (maxLat - minLat) || 0.0001;
  const lonSpan = (maxLon - minLon) || 0.0001;

  const projectX = (lon) => pad + ((lon - minLon) / lonSpan) * (w - 2 * pad);
  const projectY = (lat) => pad + ((maxLat - lat) / latSpan) * (h - 2 * pad);

  let pathData = "";
  for (let i = 0; i < points.length; i++) {
    const px = projectX(points[i].shape_pt_lon).toFixed(1);
    const py = projectY(points[i].shape_pt_lat).toFixed(1);
    pathData += (i === 0 ? `M ${px} ${py}` : ` L ${px} ${py}`);
  }

  const startPt = points[0];
  const endPt = points[points.length - 1];
  const sx = projectX(startPt.shape_pt_lon).toFixed(1);
  const sy = projectY(startPt.shape_pt_lat).toFixed(1);
  const ex = projectX(endPt.shape_pt_lon).toFixed(1);
  const ey = projectY(endPt.shape_pt_lat).toFixed(1);

  svg.innerHTML = `
    <rect width="${w}" height="${h}" fill="var(--bg, #fcfbfe)" rx="6" />
    <path d="${pathData}" fill="none" stroke="var(--blue, #8050d8)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
    <circle cx="${sx}" cy="${sy}" r="5" fill="#22c55e" stroke="#ffffff" stroke-width="2"><title>Route Start</title></circle>
    <circle cx="${ex}" cy="${ey}" r="5" fill="#ef4444" stroke="#ffffff" stroke-width="2"><title>Route Terminus</title></circle>
  `;
}

async function selectNetworkRoute(routeId) {
  const empty = $("#networkRouteEmpty");
  const detail = $("#networkRouteDetail");
  if (empty) empty.hidden = true;
  if (detail) detail.hidden = false;

  try {
    const data = await api(`/api/gtfs/routes/${encodeURIComponent(routeId)}`);
    const title = $("#networkRouteTitle");
    if (title) title.textContent = `${data.route_short_name || data.route_id} · ${data.route_long_name || "Bus Route"}`;

    const agency = $("#networkRouteAgency");
    if (agency) agency.textContent = data.agency_id || "Transit Agency";

    const typeBadge = $("#networkRouteType");
    const routeTypeNames = { 0: "Tram", 1: "Subway", 2: "Rail", 3: "Bus", 4: "Ferry" };
    if (typeBadge) typeBadge.textContent = routeTypeNames[data.route_type] || "Bus";

    const bounds = $("#networkServiceBounds");
    if (bounds) bounds.textContent = data.first_service_time && data.last_service_time ? `${data.first_service_time.slice(0, 5)} – ${data.last_service_time.slice(0, 5)}` : "Not scheduled";

    const daysContainer = $("#networkCalendarDays");
    if (daysContainer) {
      const activeDays = new Set();
      if (Array.isArray(data.calendar)) {
        for (const cal of data.calendar) {
          if (Array.isArray(cal.days)) {
            for (const d of cal.days) activeDays.add(d);
          }
        }
      }
      const allDays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
      daysContainer.innerHTML = allDays.map((d) => {
        const isActive = activeDays.has(d);
        const style = isActive
          ? "background: var(--blue-soft, #e9d5ff); color: var(--blue, #6b21a8); font-weight: 700; border: 1px solid var(--blue, #c084fc);"
          : "background: var(--surface-raised, #f3f4f6); color: var(--muted, #9ca3af); opacity: 0.6;";
        return `<span style="padding: 2px 8px; border-radius: 4px; font-size: 11px; ${style}">${d}</span>`;
      }).join("");
    }

    renderRouteShapeSvg(data.shape_points || []);

    const stopsCount = $("#networkStopsCount");
    if (stopsCount) stopsCount.textContent = `${data.stops?.length || 0} ordered stops`;

    const stopsList = $("#networkStopsList");
    if (stopsList) {
      if (Array.isArray(data.stops) && data.stops.length > 0) {
        stopsList.innerHTML = data.stops.map((s, idx) => `
          <tr>
            <td>${s.stop_sequence ?? (idx + 1)}</td>
            <td><strong>${escapeText(s.stop_name)}</strong></td>
            <td><code>${escapeText(s.stop_code || s.stop_id)}</code></td>
            <td class="numeric">${Number(s.stop_lat).toFixed(4)}</td>
            <td class="numeric">${Number(s.stop_lon).toFixed(4)}</td>
          </tr>
        `).join("");
      } else {
        stopsList.innerHTML = '<tr><td colspan="5" class="empty-row">No stops recorded for this route.</td></tr>';
      }
    }
  } catch (err) {
    toast("Failed to load route details: " + err.message, true);
  }
}

function setupTransitNetworkExplorer() {
  const gtfsInput = $("#gtfsFileInput");
  if (gtfsInput) {
    gtfsInput.addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      withBusy("Importing GTFS…", async () => {
        const form = new FormData();
        form.append("file", file);
        const res = await api("/api/gtfs/upload", { method: "POST", body: form });
        toast(res.message || "GTFS archive imported successfully!");
        networkInitialized = false;
        await initNetworkExplorer();
        return res;
      }).finally(() => { e.target.value = ""; });
    });
  }
  const agencySelect = $("#networkAgencySelect");
  if (agencySelect) {
    agencySelect.addEventListener("change", () => filterNetworkRoutes());
  }
  const routeForm = $("#networkRouteSearchForm");
  if (routeForm) {
    routeForm.addEventListener("submit", (e) => {
      e.preventDefault();
      filterNetworkRoutes();
    });
  }
  const uploadGtfsBtn = $("#uploadGtfsButton");
  if (uploadGtfsBtn) {
    uploadGtfsBtn.addEventListener("click", () => gtfsInput?.click());
  }
}

async function initOperationsDashboard() {
  await loadOperationsData();
}

async function loadOperationsData() {
  try {
    const [metrics, summary, alerts, recs, vehicles] = await Promise.all([
      api("/api/operations/metrics").catch(() => ({})),
      api("/api/realtime/summary").catch(() => ({})),
      api("/api/operations/alerts").catch(() => []),
      api("/api/operations/dispatch-recommendations").catch(() => []),
      api("/api/realtime/vehicles").catch(() => []),
    ]);

    renderOperationsKpis(metrics, summary);
    renderOperationsAlerts(alerts);
    renderDispatchRecommendations(recs);
    renderFleetMapSvg(vehicles);
    renderFleetVehiclesTable(vehicles);

    const badge = $("#operationsTelemetryBadge");
    if (badge) {
      const activeCount = Array.isArray(vehicles) ? vehicles.length : 0;
      if (activeCount > 0) {
        badge.textContent = `${activeCount} BUSES ACTIVE · LIVE`;
        badge.classList.remove("error");
      } else {
        badge.textContent = "NO LIVE TELEMETRY";
      }
    }
  } catch (err) {
    console.warn("Failed to load operations data", err);
  }
}

function renderOperationsKpis(metrics, summary) {
  const activeFleet = $("#opsKpiActiveFleet");
  if (activeFleet) activeFleet.textContent = metrics?.active_fleet_count != null ? metrics.active_fleet_count.toLocaleString() : "0";

  const otp = $("#opsKpiOtp");
  if (otp) otp.textContent = metrics?.on_time_performance_pct != null ? `${metrics.on_time_performance_pct}%` : "—";

  const ewt = $("#opsKpiEwt");
  if (ewt) ewt.textContent = metrics?.excess_wait_time_minutes != null ? `+${metrics.excess_wait_time_minutes} min` : "—";

  const reg = $("#opsKpiRegularity");
  if (reg) reg.textContent = metrics?.service_regularity_index != null ? `${(metrics.service_regularity_index * 100).toFixed(0)}%` : "—";
}

function renderOperationsAlerts(alerts) {
  const container = $("#opsAlertsList");
  if (!container) return;

  if (!Array.isArray(alerts) || alerts.length === 0) {
    container.innerHTML = '<p class="monthly-route-empty">No active operational alerts. Fleet is operating within normal parameters.</p>';
    return;
  }

  container.innerHTML = alerts.map((a) => {
    const isCritical = a.severity === "CRITICAL" || a.severity === "HIGH";
    const bgStyle = isCritical ? "background: #fef2f2; border-color: #f87171;" : "background: #fffbeb; border-color: #fcd34d;";
    const badgeColor = isCritical ? "#ef4444" : "#f59e0b";
    return `
      <div style="border: 1px solid var(--border); border-radius: 6px; padding: 10px; ${bgStyle}">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
          <span style="font-weight: 700; font-size: 11px; color: ${badgeColor}; text-transform: uppercase;">${escapeText(a.anomaly_type)} · ${escapeText(a.severity)}</span>
          <span class="route-pill">${escapeText(a.route_id)}</span>
        </div>
        <p style="margin: 0; font-size: 13px; line-height: 1.4;">${escapeText(a.description)}</p>
      </div>
    `;
  }).join("");
}

function renderDispatchRecommendations(recs) {
  const container = $("#opsRecommendationsList");
  if (!container) return;

  if (!Array.isArray(recs) || recs.length === 0) {
    container.innerHTML = '<p class="monthly-route-empty">No dispatch actions required right now.</p>';
    return;
  }

  container.innerHTML = recs.map((r) => `
    <div style="border: 1px solid var(--border); border-radius: 6px; padding: 10px; background: var(--surface-raised);">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
        <span style="font-weight: 700; font-size: 11px; color: var(--blue, #8050d8); text-transform: uppercase;">ACTION: ${escapeText(r.action_type)}</span>
        <span class="route-pill">${escapeText(r.route_id)}</span>
      </div>
      <p style="margin: 0 0 6px 0; font-size: 13px; font-weight: 600;">${escapeText(r.instruction)}</p>
      <small style="color: var(--muted); font-size: 11px;">${escapeText(r.estimated_passenger_benefit)}</small>
    </div>
  `).join("");
}

function renderFleetMapSvg(vehicles) {
  const svg = $("#opsFleetMap");
  if (!svg) return;

  if (!Array.isArray(vehicles) || vehicles.length === 0) {
    svg.innerHTML = '<text x="250" y="130" text-anchor="middle" fill="#877c94" font-size="13">No live GPS vehicles active on radar</text>';
    return;
  }

  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (const v of vehicles) {
    if (v.latitude < minLat) minLat = v.latitude;
    if (v.latitude > maxLat) maxLat = v.latitude;
    if (v.longitude < minLon) minLon = v.longitude;
    if (v.longitude > maxLon) maxLon = v.longitude;
  }

  const pad = 36;
  const w = 500, h = 260;
  const latSpan = (maxLat - minLat) || 0.01;
  const lonSpan = (maxLon - minLon) || 0.01;

  const projectX = (lon) => pad + ((lon - minLon) / lonSpan) * (w - 2 * pad);
  const projectY = (lat) => pad + ((maxLat - lat) / latSpan) * (h - 2 * pad);

  const dots = vehicles.map((v) => {
    const cx = projectX(v.longitude).toFixed(1);
    const cy = projectY(v.latitude).toFixed(1);
    const isCrowded = v.occupancy_status === "FULL" || v.occupancy_status === "STANDING_ROOM_ONLY";
    const color = isCrowded ? "#ef4444" : "#22c55e";
    const label = `${escapeText(v.vehicle_id)} (R${escapeText(v.route_id || "—")})`;
    return `
      <g>
        <circle cx="${cx}" cy="${cy}" r="12" fill="${color}" fill-opacity="0.25" />
        <circle cx="${cx}" cy="${cy}" r="6" fill="${color}" stroke="#ffffff" stroke-width="2">
          <title>${label} · ${v.speed_kmh || 0} km/h · ${v.occupancy_status}</title>
        </circle>
        <text x="${cx}" y="${Number(cy) - 9}" text-anchor="middle" fill="var(--text, #1f2937)" font-size="10" font-weight="700">${label}</text>
      </g>
    `;
  }).join("");

  svg.innerHTML = `
    <rect width="${w}" height="${h}" fill="var(--bg, #fcfbfe)" rx="8" />
    <defs>
      <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="var(--border, #f3e8ff)" stroke-width="1"/>
      </pattern>
    </defs>
    <rect width="${w}" height="${h}" fill="url(#grid)" />
    ${dots}
  `;
}

function renderFleetVehiclesTable(vehicles) {
  const tbody = $("#opsVehiclesTableBody");
  if (!tbody) return;

  if (!Array.isArray(vehicles) || vehicles.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No active vehicles reporting telemetry.</td></tr>';
    return;
  }

  tbody.innerHTML = vehicles.map((v) => `
    <tr>
      <td><strong>${escapeText(v.vehicle_id)}</strong></td>
      <td><span class="route-pill">${escapeText(v.route_id || "—")}</span></td>
      <td>${escapeText(v.current_status || "IN_TRANSIT")}</td>
      <td>${v.speed_kmh ? `${v.speed_kmh} km/h` : "—"}</td>
      <td><span class="route-pill" style="font-size: 11px;">${escapeText(v.occupancy_status || "UNKNOWN")}</span></td>
    </tr>
  `).join("");
}

function setupOperationsListeners() {
  const simBtn = $("#btnSimulateLiveFleet");
  if (simBtn) {
    simBtn.addEventListener("click", () => {
      withBusy("Simulating live telemetry…", async () => {
        const res = await api("/api/realtime/demo-snapshot", { method: "POST" });
        toast(res.message || "Simulated live telemetry generated!");
        await loadOperationsData();
        return res;
      });
    });
  }
}

function initPlanner() {
  const btn = $("#runSimButton");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "true";

  btn.addEventListener("click", () => {
    withBusy("Evaluating simulation scenario…", async () => {
      const payload = {
        route_id: $("#simRouteInput").value.trim() || "1",
        forecast_hourly_demand: Number($("#simDemandInput").value) || 200,
        baseline_trips_per_hour: Number($("#simBaseFreqInput").value) || 4,
        modified_trips_per_hour: Number($("#simModFreqInput").value) || 6,
        vehicle_capacity: Number($("#simCapacityInput").value) || 55,
        operating_hours: 16,
      };
      try {
        const res = await api("/api/simulator/evaluate", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        $("#simBaseCap").textContent = `${res.baseline_capacity_per_hour} pax`;
        $("#simModCap").textContent = `${res.modified_capacity_per_hour} pax`;
        $("#simBaseUtil").textContent = `${res.baseline_utilization_pct}%`;
        $("#simModUtil").textContent = `${res.modified_utilization_pct}%`;

        const badge = $("#simRiskBadge");
        badge.textContent = `Risk: ${res.crowding_risk_modified}`;
        badge.style.background = res.crowding_risk_modified === "CRITICAL" ? "#fee2e2" : (res.crowding_risk_modified === "HIGH" ? "#fef3c7" : "#dcfce7");
        badge.style.color = res.crowding_risk_modified === "CRITICAL" ? "#991b1b" : (res.crowding_risk_modified === "HIGH" ? "#92400e" : "#166534");

        $("#simSummaryText").textContent = res.operational_summary;
        toast("Simulation completed!");
      } catch (err) {
        toast(`Simulation error: ${err.message}`);
      }
    });
  });
}

function initOccupancy() {
  const tripBtn = $("#loadTripOccButton");
  if (tripBtn && !tripBtn.dataset.bound) {
    tripBtn.dataset.bound = "true";
    tripBtn.addEventListener("click", () => {
      const tripId = $("#occTripInput").value.trim();
      if (!tripId) {
        toast("Enter a valid Trip ID");
        return;
      }
      withBusy("Loading trip occupancy…", async () => {
        try {
          const res = await api(`/api/occupancy/trip/${encodeURIComponent(tripId)}`);
          $("#occPeakLoad").textContent = `${res.peak_occupancy} pax`;
          $("#occVehicleCap").textContent = `${res.vehicle_capacity} cap`;
          $("#occPeakUtil").textContent = `${res.peak_utilization_pct}%`;
          $("#occConfidenceBadge").textContent = `Status: ${res.confidence_status}`;

          const tbody = $("#occStopsTableBody");
          if (res.stops && res.stops.length > 0) {
            tbody.innerHTML = res.stops.map((s) => `
              <tr>
                <td>${s.stop_sequence}</td>
                <td><strong>${escapeText(s.stop_id)}</strong></td>
                <td class="numeric">${s.boardings}</td>
                <td class="numeric">${s.alightings}</td>
                <td class="numeric"><strong>${s.onboard_load}</strong></td>
                <td><span class="status-badge" style="background:${s.is_overcapacity ? '#fee2e2' : '#f1f5f9'}; color:${s.is_overcapacity ? '#991b1b' : '#334155'}; font-size:11px;">${s.utilization_pct}%</span></td>
              </tr>
            `).join("");
          } else {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No stops recorded for this trip.</td></tr>';
          }
          toast("Trip occupancy loaded!");
        } catch (err) {
          toast(err.message || "Trip occupancy not found");
        }
      });
    });
  }

  const routeBtn = $("#loadRouteOccButton");
  if (routeBtn && !routeBtn.dataset.bound) {
    routeBtn.dataset.bound = "true";
    routeBtn.addEventListener("click", () => {
      const routeId = $("#occRouteInput").value.trim();
      if (!routeId) {
        toast("Enter a valid Route ID");
        return;
      }
      withBusy("Checking route capacity…", async () => {
        try {
          const res = await api(`/api/occupancy/route/${encodeURIComponent(routeId)}`);
          $("#occPeakUtil").textContent = `${res.average_peak_utilization_pct}%`;
          $("#occConfidenceBadge").textContent = `Trips evaluated: ${res.trips_evaluated} (${res.status})`;
          toast(res.message || "Route capacity checked!");
        } catch (err) {
          toast(err.message || "Route capacity error");
        }
      });
    });
  }
}

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
  if (view === "forecasts" && state.data) renderChart(state.data.forecasts);
  if (view === "routes") void searchMonthlyRoutes($("#monthlyRouteSearch").value);
  if (view === "network") void initNetworkExplorer();
  if (view === "operations") void initOperationsDashboard();
  if (view === "planner") void initPlanner();
  if (view === "occupancy") void initOccupancy();
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
      setSplashStatus("Waking up the server (up to 60s)…");
      clearTimeout(state.openingMessageTimer);
      state.openingMessageTimer = setTimeout(() => {
        if (state.opening) setSplashStatus("Waking up the server (up to 60s)…");
      }, 8000);
    }
    if (state.opening) await waitForServer();
    if (accessKey()) {
      try {
        const operatorStatus = await api("/api/status");
        if (operatorStatus.forecasts?.length) {
          setPublicDemoMode(false, true);
          render(operatorStatus);
        } else {
          setPublicDemoMode(true, true);
          if (state.opening) setSplashStatus("Loading the synthetic sample forecast…");
          render({ ...(await api("/api/public-demo/status")), public_demo: true });
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
      connectionError = "Could not reach this website’s forecast service after 60 seconds. Retry when your connection is ready.";
    }
  } finally {
    state.checking = false;
    const retryButton = $("#retryOpening");
    if (retryButton) { retryButton.disabled = false; retryButton.innerHTML = "Retry connection <span>↻</span>"; }
    if (state.opening) await finishSplash(connected, connectionError);
  }
  return connected;
}
async function waitForServer() {
  const deadline = Date.now() + 60000;
  const delays = [1000, 2000, 4000, 8000];
  let attempt = 0;
  while (Date.now() < deadline) {
    try { await api("/health"); return; }
    catch (_) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      setSplashStatus("Waking up the server (up to 60s)…");
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(delays[Math.min(attempt++, delays.length - 1)], remaining)));
    }
  }
  throw new Error("Server wake-up timed out");
}
async function withBusy(label, operation) {
  if (state.busy) return;
  setBusy(true, label);
  try { const result = await operation(); if (result.message) toast(result.message); const updatedStatus = result.status || (result.public_demo ? result : null); if (updatedStatus) { if (!updatedStatus.public_demo && updatedStatus.data_loaded && !updatedStatus.forecasts?.length) { const demo = await api("/api/public-demo/status"); setPublicDemoMode(true, true); render({ ...demo, public_demo: true }); } else { setPublicDemoMode(Boolean(updatedStatus.public_demo), Boolean(updatedStatus.public_demo ? state.operatorAccess : updatedStatus.data_loaded)); render(updatedStatus); } } else await refreshStatus(); }
  catch (error) {
    const message = error.message || "Something went wrong";
    if (/missing route\/date observations detected/i.test(message)) toast("This route has days without a count. Add the missing daily counts, or choose zero only for a confirmed no-service day.", true);
    else if (/need at least \d+ continuous calendar days/i.test(message)) toast(message, true);
    else toast(message, true);
  }
  finally { setBusy(false); }
}
async function train(options = {}) {
  const horizon = Number(options.horizon || $("#trainDays").value || 7);
  const folds = Number(options.folds || 4);
  return api("/api/train", { method: "POST", body: JSON.stringify({ horizon, folds, missing_days: $("#missingDays").value }) });
}
function setup() {
  // Older versions persisted the operator key. Remove it and retain keys only in page memory.
  try { localStorage.removeItem("busForecastApiKey"); } catch (_) {}
  setupMonthlyRouteExplorer();
  setupTransitNetworkExplorer();
  setupOperationsListeners();
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
  $("#settingsButton").addEventListener("click", () => { $("#apiKey").value = accessKey(); $("#settingsDialog").showModal(); });
  $("#saveKey").addEventListener("click", () => { operatorKeyInMemory = $("#apiKey").value.trim(); try { localStorage.removeItem("busForecastApiKey"); localStorage.removeItem("busForecastApiUrl"); } catch (_) {} $("#settingsDialog").close(); refreshStatus(); });
  $("#clearKey").addEventListener("click", () => { operatorKeyInMemory = ""; try { localStorage.removeItem("busForecastApiKey"); localStorage.removeItem("busForecastApiUrl"); } catch (_) {} $("#apiKey").value = ""; toast("Operator access cleared for this page"); refreshStatus(); });
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
  $("#demoButton").addEventListener("click", () => withBusy("Loading synthetic preview…", async () => ({ ...(await api("/api/public-demo/status")), public_demo: true, message: "Read-only synthetic example loaded; your operator data was not changed." })));
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
  window.addEventListener("resize", () => { if (state.data) renderChart(state.data.forecasts); });
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); state.installPrompt = event; $("#installButton").classList.remove("hidden"); });
  $("#installButton").addEventListener("click", async () => { if (!state.installPrompt) return; state.installPrompt.prompt(); await state.installPrompt.userChoice; state.installPrompt = null; $("#installButton").classList.add("hidden"); });
function setupAiAssistant() {
  const toggleBtn = $("#aiChatToggle");
  const panel = $("#aiChatPanel");
  const closeBtn = $("#aiCloseChatBtn");
  const clearBtn = $("#aiClearHistoryBtn");
  const form = $("#aiChatForm");
  const input = $("#aiChatInput");
  const sendBtn = $("#aiSendBtn");
  const messagesContainer = $("#aiChatMessages");
  const statusDot = $("#aiStatusDot");
  const modelBadge = $("#aiModelBadge");
  const suggestedPrompts = $("#aiSuggestedPrompts");

  if (!toggleBtn || !panel || !form || !input) return;

  const chatHistory = [];
  let isSubmitting = false;

  async function checkStatus() {
    try {
      const res = await api("/api/ai/status");
      if (res && res.configured) {
        statusDot.classList.add("active");
        statusDot.title = `AI Assistant ready (${res.model})`;
        modelBadge.textContent = res.model || "OpenRouter";
      } else {
        statusDot.classList.remove("active");
        statusDot.title = "OpenRouter API Key not configured";
        modelBadge.textContent = "Key Required";
      }
    } catch (_) {
      statusDot.classList.remove("active");
      modelBadge.textContent = "Offline";
    }
  }

  function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function formatInline(str) {
    return str
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function renderFormattedText(text) {
    const escaped = String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    const paragraphs = escaped.split(/\n\n+/);
    return paragraphs.map((p) => {
      const lines = p.split(/\n/);
      if (lines.length > 1 && lines.every((l) => l.trim().startsWith("- ") || l.trim().startsWith("* "))) {
        const lis = lines.map((l) => `<li>${formatInline(l.trim().replace(/^[-*]\s+/, ""))}</li>`).join("");
        return `<ul>${lis}</ul>`;
      }
      return `<p>${lines.map(formatInline).join("<br>")}</p>`;
    }).join("");
  }

  function appendMessage(role, content, isError = false) {
    const msgEl = document.createElement("div");
    msgEl.className = `ai-message ${role === "user" ? "ai-user-message" : "ai-bot-message"}`;

    const avatar = document.createElement("div");
    avatar.className = "ai-message-avatar";
    avatar.textContent = role === "user" ? "YOU" : "AI";

    const contentEl = document.createElement("div");
    contentEl.className = `ai-message-content${isError ? " ai-message-error" : ""}`;
    contentEl.innerHTML = renderFormattedText(content);

    msgEl.appendChild(avatar);
    msgEl.appendChild(contentEl);
    messagesContainer.appendChild(msgEl);
    scrollToBottom();
  }

  function showTypingIndicator() {
    const ind = document.createElement("div");
    ind.id = "aiTypingIndicator";
    ind.className = "ai-message ai-bot-message";
    ind.innerHTML = `
      <div class="ai-message-avatar">AI</div>
      <div class="ai-message-content" style="padding: 6px 12px;">
        <div class="ai-typing-indicator"><span></span><span></span><span></span></div>
      </div>
    `;
    messagesContainer.appendChild(ind);
    scrollToBottom();
  }

  function hideTypingIndicator() {
    const el = document.getElementById("aiTypingIndicator");
    if (el) el.remove();
  }

  toggleBtn.addEventListener("click", () => {
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) {
      input.focus();
      checkStatus();
    }
  });

  closeBtn.addEventListener("click", () => {
    panel.classList.add("hidden");
  });

  clearBtn.addEventListener("click", () => {
    chatHistory.length = 0;
    messagesContainer.innerHTML = `
      <div class="ai-message ai-bot-message">
        <div class="ai-message-avatar">AI</div>
        <div class="ai-message-content">
          <p><strong>Conversation cleared.</strong></p>
          <p>How can I help you analyze bus demand or transit operations?</p>
        </div>
      </div>
    `;
    scrollToBottom();
  });

  suggestedPrompts.querySelectorAll(".ai-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const prompt = chip.getAttribute("data-prompt");
      if (prompt && !isSubmitting) {
        input.value = prompt;
        sendMessage();
      }
    });
  });

  async function sendMessage() {
    const text = input.value.trim();
    if (!text || isSubmitting) return;

    appendMessage("user", text);
    input.value = "";
    isSubmitting = true;
    sendBtn.disabled = true;
    showTypingIndicator();

    try {
      const payload = {
        message: text,
        history: chatHistory.slice(-6),
      };
      const routeSelect = $("#forecastRoute");
      if (routeSelect && routeSelect.value && routeSelect.value !== "all") {
        payload.route_id = routeSelect.value;
      }

      const res = await api("/api/ai/chat", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      hideTypingIndicator();

      if (res && res.status === "ok" && res.reply) {
        appendMessage("assistant", res.reply);
        chatHistory.push({ role: "user", content: text });
        chatHistory.push({ role: "assistant", content: res.reply });
      } else {
        const errMsg = res?.message || "Received an unexpected response format.";
        appendMessage("assistant", `⚠️ ${errMsg}`, true);
      }
    } catch (err) {
      hideTypingIndicator();
      const msg = err.message || "Failed to communicate with the AI assistant.";
      appendMessage("assistant", `⚠️ ${msg}`, true);
    } finally {
      isSubmitting = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage();
  });

  checkStatus();
}

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  setupAiAssistant();
  refreshStatus();
  window.setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, 60000);
}
document.addEventListener("DOMContentLoaded", setup);
