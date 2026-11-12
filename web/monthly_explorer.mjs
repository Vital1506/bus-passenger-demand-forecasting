/**
 * Smart Route Explorer data helpers.
 *
 * Pure functions only: no DOM, no network. They consume the public monthly
 * endpoints (`/api/public/monthly-data/...`) and never invent observations —
 * a month the source does not report becomes a gap (`boardings: null`), never 0.
 */

const MONTH_PATTERN = /^(\d{4})-(\d{2})-01$/;
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

export function monthIndex(value) {
  const match = MONTH_PATTERN.exec(String(value ?? ""));
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  if (!Number.isInteger(year) || month < 1 || month > 12) return null;
  return year * 12 + (month - 1);
}

export function monthFromIndex(index) {
  const year = Math.floor(index / 12);
  const month = (index % 12) + 1;
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-01`;
}

export function monthLabel(value, { withYear = true } = {}) {
  const index = monthIndex(value);
  if (index === null) return "";
  const name = MONTH_NAMES[index % 12];
  const year = Math.floor(index / 12);
  return withYear ? `${name} ${year}` : `${name} ’${String(year % 100).padStart(2, "0")}`;
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Math.round(number).toLocaleString("en-US");
}

export function formatCompact(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (Math.abs(number) >= 1_000_000) return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 0 : 1)}M`;
  if (Math.abs(number) >= 1_000) return `${(number / 1_000).toFixed(number >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(number));
}

function naturalCompare(left, right) {
  return String(left).localeCompare(String(right), "en", { numeric: true, sensitivity: "base" });
}

/** Normalize `GET /api/public/monthly-data/routes` into a clean route array. */
export function normalizeRoutesPayload(payload) {
  const raw = Array.isArray(payload) ? payload : payload?.routes;
  if (!Array.isArray(raw)) return [];
  const routes = [];
  const seen = new Set();
  for (const entry of raw) {
    const routeId = typeof entry?.route_id === "string" ? entry.route_id.trim() : "";
    if (!routeId || seen.has(routeId)) continue;
    seen.add(routeId);
    const total = Number(entry.total_boardings);
    const monthCount = Number(entry.month_count);
    routes.push({
      route_id: routeId,
      route_name: String(entry.route_name ?? routeId),
      first_month: monthIndex(entry.first_month) === null ? null : entry.first_month,
      last_month: monthIndex(entry.last_month) === null ? null : entry.last_month,
      month_count: Number.isFinite(monthCount) && monthCount > 0 ? monthCount : 0,
      total_boardings: Number.isFinite(total) && total > 0 ? total : 0,
    });
  }
  return routes;
}

function buildNameHistory(records) {
  const segments = [];
  for (const record of records) {
    const name = record.route_name;
    const last = segments[segments.length - 1];
    if (last && last.route_name === name) {
      last.last_month = record.month_start;
      last.month_count += 1;
    } else {
      segments.push({ route_name: name, first_month: record.month_start, last_month: record.month_start, month_count: 1 });
    }
  }
  return segments;
}

/** Normalize `GET /api/public/monthly-data/routes/{id}/history`. */
export function normalizeHistoryPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const routeId = typeof payload.route_id === "string" ? payload.route_id.trim() : "";
  if (!routeId) return null;
  const records = [];
  const seen = new Set();
  for (const record of Array.isArray(payload.records) ? payload.records : []) {
    const month = record?.month_start;
    if (monthIndex(month) === null || seen.has(month)) continue;
    const boardings = Number(record?.boardings);
    if (!Number.isFinite(boardings) || boardings < 0) continue;
    seen.add(month);
    records.push({
      month_start: month,
      boardings,
      route_name: String(record?.route_name ?? payload.route_name ?? routeId),
    });
  }
  records.sort((left, right) => monthIndex(left.month_start) - monthIndex(right.month_start));
  if (!records.length) return null;
  const nameHistory = Array.isArray(payload.name_history)
    ? payload.name_history
        .filter((segment) => typeof segment?.route_name === "string" && monthIndex(segment?.first_month) !== null)
        .map((segment) => ({
          route_name: segment.route_name,
          first_month: segment.first_month,
          last_month: monthIndex(segment.last_month) === null ? segment.first_month : segment.last_month,
          month_count: Number(segment.month_count) > 0 ? Number(segment.month_count) : 1,
        }))
    : buildNameHistory(records);
  return {
    route_id: routeId,
    route_name: String(payload.route_name ?? records[records.length - 1].route_name ?? routeId),
    first_month: records[0].month_start,
    last_month: records[records.length - 1].month_start,
    month_count: records.length,
    total_boardings: records.reduce((sum, record) => sum + record.boardings, 0),
    name_history: nameHistory.length ? nameHistory : buildNameHistory(records),
    records,
  };
}

/**
 * Search routes by ID or name. An empty query returns the busiest routes first,
 * which gives the explorer a useful starting view without inventing data.
 */
export function searchRoutes(routes, query, limit = 40) {
  const items = Array.isArray(routes) ? routes : [];
  const normalizedQuery = String(query ?? "").trim().toLocaleLowerCase("en");
  const cap = Math.max(0, Number(limit) || 0);
  if (!normalizedQuery) {
    return items
      .slice()
      .sort((left, right) => (right.total_boardings - left.total_boardings) || naturalCompare(left.route_id, right.route_id))
      .slice(0, cap || items.length);
  }
  const scored = [];
  for (const route of items) {
    const id = route.route_id.toLocaleLowerCase("en");
    const name = String(route.route_name ?? "").toLocaleLowerCase("en");
    let score = -1;
    if (id === normalizedQuery) score = 0;
    else if (id.startsWith(normalizedQuery)) score = 1;
    else if (name.startsWith(normalizedQuery)) score = 2;
    else if (id.includes(normalizedQuery)) score = 3;
    else if (name.includes(normalizedQuery)) score = 4;
    if (score >= 0) scored.push({ route, score, length: id.length });
  }
  scored.sort((left, right) =>
    left.score - right.score ||
    left.length - right.length ||
    naturalCompare(left.route.route_id, right.route.route_id));
  return scored.slice(0, cap || scored.length).map((entry) => entry.route);
}

/** Split records into observed points and explicit gaps across the full span. */
export function buildMonthlySeries(history) {
  const normalized = normalizeHistoryPayload(history);
  if (!normalized) return { points: [], segments: [], gaps: [], firstMonth: null, lastMonth: null, observedMonths: 0 };
  const observed = new Map(normalized.records.map((record) => [record.month_start, record]));
  const start = monthIndex(normalized.first_month);
  const end = monthIndex(normalized.last_month);
  const points = [];
  for (let index = start; index <= end; index += 1) {
    const month = monthFromIndex(index);
    const record = observed.get(month);
    points.push({
      month,
      index,
      observed: Boolean(record),
      boardings: record ? record.boardings : null,
      route_name: record ? record.route_name : null,
    });
  }
  const segments = [];
  const gaps = [];
  let run = null;
  let gap = null;
  for (const point of points) {
    if (point.observed) {
      if (gap) { gaps.push(gap); gap = null; }
      if (!run) { run = []; segments.push(run); }
      run.push(point);
    } else {
      run = null;
      if (!gap) { gap = { start: point.month, end: point.month, month_count: 0 }; }
      gap.end = point.month;
      gap.month_count += 1;
    }
  }
  if (gap) gaps.push(gap);
  return { points, segments, gaps, firstMonth: normalized.first_month, lastMonth: normalized.last_month, observedMonths: normalized.records.length };
}

/** Missing-month runs inside a route's observed span. */
export function missingMonthRanges(history) {
  const series = buildMonthlySeries(history);
  return series.gaps.map((gap) => ({ start: gap.start, end: gap.end, month_count: gap.month_count }));
}

export function summarizeRoute(history) {
  const normalized = normalizeHistoryPayload(history);
  if (!normalized) return null;
  const series = buildMonthlySeries(normalized);
  const latest = normalized.records[normalized.records.length - 1];
  const missing = series.gaps.reduce((sum, gap) => sum + gap.month_count, 0);
  return {
    routeId: normalized.route_id,
    routeName: normalized.route_name,
    firstMonth: normalized.first_month,
    lastMonth: normalized.last_month,
    observedMonths: normalized.month_count,
    missingMonths: missing,
    latestMonth: latest.month_start,
    latestBoardings: latest.boardings,
    totalBoardings: normalized.total_boardings,
    nameChanges: Math.max(0, normalized.name_history.length - 1),
    nameHistory: normalized.name_history,
  };
}

/**
 * Geometry for the monthly trend chart. Gaps stay gaps: each run of consecutive
 * observed months becomes its own path, so no line is drawn across missing months.
 */
export function chartModel(history, options = {}) {
  const width = Number(options.width) > 0 ? Number(options.width) : 720;
  const height = Number(options.height) > 0 ? Number(options.height) : 260;
  const padding = { left: 58, right: 18, top: 18, bottom: 38, ...(options.padding || {}) };
  const series = buildMonthlySeries(history);
  const values = series.points.filter((point) => point.observed).map((point) => point.boardings);
  const maxValue = values.length ? Math.max(...values) : 0;
  const domainMax = maxValue > 0 ? maxValue * 1.1 : 1;
  const innerWidth = Math.max(1, width - padding.left - padding.right);
  const innerHeight = Math.max(1, height - padding.top - padding.bottom);
  const lastIndex = Math.max(0, series.points.length - 1);
  const xFor = (position) => (series.points.length <= 1
    ? padding.left + innerWidth / 2
    : padding.left + (position / lastIndex) * innerWidth);
  const yFor = (value) => height - padding.bottom - (Number(value) / domainMax) * innerHeight;

  const pointGeometry = (point, position) => ({
    x: Number(xFor(position).toFixed(1)),
    y: Number(yFor(point.boardings).toFixed(1)),
    month: point.month,
    boardings: point.boardings,
    route_name: point.route_name,
  });

  const segments = series.segments.map((segment) => {
    const points = segment.map((point, position) => pointGeometry(point, series.points.indexOf(point)));
    const path = points.map((point, index) => `${index ? "L" : "M"}${point.x},${point.y}`).join(" ");
    return { path, points };
  });

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((part) => {
    const value = domainMax * part;
    return { value, y: Number(yFor(value).toFixed(1)), label: formatCompact(value) };
  });

  const step = Math.max(1, Math.ceil(series.points.length / 7));
  const xTicks = [];
  series.points.forEach((point, position) => {
    if (position === 0 || position === lastIndex || position % step === 0) {
      xTicks.push({
        x: Number(xFor(position).toFixed(1)),
        month: point.month,
        label: monthLabel(point.month, { withYear: series.points.length <= 84 }),
        anchor: position === 0 ? "start" : position === lastIndex ? "end" : "middle",
      });
    }
  });

  return {
    width,
    height,
    padding,
    domainMax,
    hasData: values.length > 0,
    series,
    segments,
    yTicks,
    xTicks,
  };
}

export function describeError(error, context = "dataset") {
  const status = Number(error?.status);
  const message = String(error?.message || "");
  if (status === 404) {
    return context === "route"
      ? "That route has no stored monthly records in this dataset."
      : "Monthly CTA data is not available from this server. Public monthly reads may be disabled by the operator.";
  }
  if (status === 503) return "The monthly data service is unavailable right now. Try again shortly.";
  if (Number.isFinite(status) && status >= 500) return "The monthly data service reported an error. Try again shortly.";
  if (/failed to fetch|networkerror|load failed/i.test(message) || error?.name === "TypeError") {
    return "Could not reach the monthly data service. Check your connection and try again.";
  }
  return message || "The monthly data could not be loaded.";
}
