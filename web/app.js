const $ = (selector) => document.querySelector(selector);
const state = { data: null, installPrompt: null, busy: false, toastTimer: null, publicDemo: false, lastCheckedAt: null, view: "overview", opening: true, openingStartedAt: Date.now(), openingTimer: null, openingMessageTimer: null };

function accessKey() { return localStorage.getItem("busForecastApiKey") || ""; }
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
    if (!response.ok) throw new Error(payload?.detail || payload || `Request failed (${response.status})`);
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
  [$("#csvFile"), $("#trainButton"), $("#refreshForecast"), $("#demoButton"), $("#saveCountButton")].forEach((node) => { if (node) node.disabled = busy || (state.publicDemo && !state.operatorAccess); });
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
  svg.innerHTML = `${grid}<path d="${bandTop} ${bandBottom} Z" fill="#cbb2ed" opacity=".42"/><path d="${line}" fill="none" stroke="#8050d8" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>${dots}${xLabels}`;
  const total = points.reduce((sum, point) => sum + point.value, 0);
  $("#chartRange").textContent = `${points.length} days · ${Math.round(total).toLocaleString()} predicted boardings`;
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
  $("#workflowNote").textContent = state.publicDemo ? (state.operatorAccess ? "Synthetic preview shown because no operator data is loaded yet. Upload daily counts, or a timetable to create a labelled synthetic scenario." : "This public preview is read-only. An authorized operator key is required to upload data or train a model.") : data.source === "synthetic schedule demo data" ? "This timetable-based scenario is synthetic, not observed passenger demand. Replace it with actual passenger counts before operational use." : data.source === "synthetic demo data" ? "Demo numbers are synthetic. Replace them with approved company data before operational use." : "Forecasts refresh when daily counts arrive. Upload only data approved for this environment.";
  renderChart(data.forecasts);
  renderTable(data.forecasts);
  renderExtraPages(data);
}

const PAGE_INFO = {
  overview: ["Start here", "Choose the example or record a daily passenger count."],
  forecasts: ["Forecasts", "Explore the demand outlook and download route-level estimates."],
  routes: ["Routes", "Compare forecast totals and validation scores by route."],
  data: ["My data", "See which passenger counts are saved and when they were updated."],
  model: ["How it works", "A plain-language guide to the data and forecast."]
};
function setSplashStatus(message) { const label = $("#splashLoading"); if (label) label.textContent = message; }
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
async function finishSplash(connected) {
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
    setSplashStatus("The forecast service could not be reached. Check your connection and try again.");
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
  [$("#csvFile"), $("#trainButton"), $("#refreshForecast"), $("#demoButton"), $("#missingDays"), $("#forecastDays"), $("#trainDays"), $("#saveCountButton")].forEach((node) => { if (node) node.disabled = state.busy || (enabled && !allowOperatorEdits); });
  $("#operatorControls").classList.toggle("hidden", enabled && !allowOperatorEdits);
  $("#publicInfo").classList.toggle("hidden", !enabled);
  $("#operatorForecastTools").classList.toggle("hidden", enabled);
  $("#publicForecastBadge").classList.toggle("hidden", !enabled);
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
  let connected = false;
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
  } finally {
    if (state.opening) await finishSplash(connected);
  }
  return connected;
}
async function withBusy(label, operation) {
  if (state.busy) return;
  setBusy(true, label);
  try { const result = await operation(); if (result.message) toast(result.message); if (result.status) { setPublicDemoMode(Boolean(result.status.public_demo), Boolean(result.status.public_demo ? false : result.status.data_loaded)); render(result.status); } else await refreshStatus(); }
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
function setup() {
  const initialView = location.hash.slice(1);
  navigate(PAGE_INFO[initialView] ? initialView : "overview", false);
  $("#mainNav").addEventListener("click", (event) => { const button = event.target.closest("[data-view]"); if (button) navigate(button.dataset.view); });
  window.addEventListener("hashchange", () => { const view = location.hash.slice(1); if (PAGE_INFO[view]) navigate(view, false); });
  $("#openDashboard").addEventListener("click", dismissSplash);
  $("#offlineOpening").addEventListener("click", dismissSplash);
  $("#retryOpening").addEventListener("click", () => {
    state.opening = true;
    state.openingStartedAt = Date.now();
    $("#appSplash").classList.remove("is-error", "is-ready");
    $("#appSplash").setAttribute("aria-busy", "true");
    clearTimeout(state.openingMessageTimer);
    $("#retryOpening").classList.add("hidden");
    $("#offlineOpening").classList.add("hidden");
    setSplashStatus("Checking the forecast service…");
    refreshStatus();
  });
  $("#settingsButton").addEventListener("click", () => { $("#apiKey").value = accessKey(); $("#apiUrl").value = localStorage.getItem("busForecastApiUrl") || ""; $("#settingsDialog").showModal(); });
  $("#saveKey").addEventListener("click", () => { const key = $("#apiKey").value.trim(), rawUrl = $("#apiUrl").value.trim(); if (rawUrl) { try { const parsed = new URL(rawUrl); if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(); localStorage.setItem("busForecastApiUrl", parsed.origin); } catch (_) { toast("Enter a valid API address starting with http:// or https://", true); return; } } else localStorage.removeItem("busForecastApiUrl"); key ? localStorage.setItem("busForecastApiKey", key) : localStorage.removeItem("busForecastApiKey"); $("#settingsDialog").close(); refreshStatus(); });
  $("#clearKey").addEventListener("click", () => { localStorage.removeItem("busForecastApiKey"); localStorage.removeItem("busForecastApiUrl"); $("#apiKey").value = ""; $("#apiUrl").value = ""; toast("Connection reset on this device"); });
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
  if (countDate) {
    const now = new Date();
    const localToday = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    countDate.value = localToday;
    countDate.max = localToday;
  }
  $("#quickCountForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const origin = $("#countOrigin").value.trim().replace(/\s+/g, " "), destination = $("#countDestination").value.trim().replace(/\s+/g, " ");
    const serviceDate = countDate.value, passengers = Number($("#countPassengers").value);
    if (!origin || !destination || origin.toLocaleLowerCase() === destination.toLocaleLowerCase()) {
      toast("Enter two different places for this route", true);
      return;
    }
    if (!serviceDate || serviceDate > countDate.max || !Number.isInteger(passengers) || passengers < 0) {
      toast("Check the service date and enter a whole passenger count of zero or more", true);
      return;
    }
    const routeId = `${origin} → ${destination}`;
    withBusy("Saving count…", async () => {
      const result = await api("/api/data/ingest", {
        method: "POST",
        body: JSON.stringify({ source_system: "manual_daily_entry", missing_days: $("#missingDays").value, records: [{ service_date: serviceDate, route_id: routeId, passengers }] })
      });
      $("#countPassengers").value = "";
      return result;
    });
  });
  $("#demoButton").addEventListener("click", () => withBusy("Running demo…", async () => { await api("/api/data/demo", { method: "POST" }); return train({ horizon: 7, folds: 4 }); }));
  $("#refreshForecast").addEventListener("click", () => withBusy("Refreshing…", () => api("/api/forecast", { method: "POST", body: JSON.stringify({ days: Number($("#forecastDays").value), missing_days: $("#missingDays").value }) })));
  $("#downloadForecast").addEventListener("click", (event) => { event.preventDefault(); const publicDemo = state.publicDemo; downloadFile(publicDemo ? "/api/public-demo/forecast.csv" : "/api/forecast.csv", publicDemo ? "bus_demand_synthetic_demo_forecast.csv" : "bus_demand_forecast.csv").catch((error) => toast(error.message, true)); });
  $("#downloadSample").addEventListener("click", (event) => { event.preventDefault(); const publicDemo = state.publicDemo; downloadFile(publicDemo ? "/api/public-demo/sample.csv" : "/api/template.csv", publicDemo ? "synthetic_daily_route_counts.csv" : "daily_route_counts_sample.csv").catch((error) => toast(error.message, true)); });
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

