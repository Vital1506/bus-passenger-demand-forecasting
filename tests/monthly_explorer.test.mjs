import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  buildMonthlySeries,
  chartModel,
  describeError,
  escapeHtml,
  formatNumber,
  missingMonthRanges,
  monthIndex,
  monthLabel,
  normalizeHistoryPayload,
  normalizeRoutesPayload,
  searchRoutes,
  summarizeRoute,
} from "../web/monthly_explorer.mjs";

const ROUTES = [
  { route_id: "79", route_name: "79th", first_month: "2001-01-01", last_month: "2026-08-01", month_count: 24, total_boardings: 9000 },
  { route_id: "9", route_name: "Ashland", first_month: "2001-01-01", last_month: "2026-08-01", month_count: 24, total_boardings: 12000 },
  { route_id: "8A", route_name: "Halsted", first_month: "2001-01-01", last_month: "2026-08-01", month_count: 24, total_boardings: 4000 },
  { route_id: "1001", route_name: "Shuttle/Special Event Route", first_month: "2024-01-01", last_month: "2024-06-01", month_count: 4, total_boardings: 500 },
];

// Jan, Feb, (Mar missing), Apr, May (Jun missing), Jul
const HISTORY = {
  route_id: "79",
  route_name: "79th",
  first_month: "2024-01-01",
  last_month: "2024-07-01",
  month_count: 5,
  name_history: [
    { route_name: "79th", first_month: "2024-01-01", last_month: "2024-04-01", month_count: 4 },
    { route_name: "79th Street", first_month: "2024-05-01", last_month: "2024-07-01", month_count: 1 },
  ],
  records: [
    { month_start: "2024-01-01", boardings: 100, route_name: "79th" },
    { month_start: "2024-02-01", boardings: 120, route_name: "79th" },
    { month_start: "2024-04-01", boardings: 140, route_name: "79th" },
    { month_start: "2024-05-01", boardings: 160, route_name: "79th Street" },
    { month_start: "2024-07-01", boardings: 180, route_name: "79th Street" },
  ],
};

test("route search matches IDs and names, ranked by match quality", () => {
  assert.equal(searchRoutes(ROUTES, "79")[0].route_id, "79");
  assert.deepEqual(searchRoutes(ROUTES, "ash").map((route) => route.route_id), ["9"]);
  assert.deepEqual(searchRoutes(ROUTES, "8a").map((route) => route.route_id), ["8A"]);
  assert.deepEqual(searchRoutes(ROUTES, "shuttle").map((route) => route.route_id), ["1001"]);
  assert.deepEqual(searchRoutes(ROUTES, "79th street"), []);
});

test("route search handles an empty catalogue, blank query, and limits", () => {
  assert.deepEqual(searchRoutes([], "79"), []);
  assert.deepEqual(searchRoutes(undefined, "79"), []);
  assert.equal(searchRoutes(ROUTES, "").length, 4);
  assert.deepEqual(searchRoutes(ROUTES, "", 1).map((route) => route.route_id), ["9"]);
  assert.deepEqual(searchRoutes(ROUTES, "zzz"), []);
});

test("route payload normalization tolerates envelope, array, and junk entries", () => {
  assert.equal(normalizeRoutesPayload({ routes: ROUTES }).length, 4);
  assert.equal(normalizeRoutesPayload(ROUTES).length, 4);
  assert.deepEqual(normalizeRoutesPayload(null), []);
  assert.deepEqual(normalizeRoutesPayload({ routes: "nope" }), []);
  const cleaned = normalizeRoutesPayload({ routes: [{ route_id: "5" }, { route_id: " " }, { route_name: "no id" }] });
  assert.equal(cleaned.length, 1);
  assert.equal(cleaned[0].route_id, "5");
});

test("history normalization drops invalid dates and negative values, keeps order", () => {
  const payload = normalizeHistoryPayload({
    route_id: "79",
    records: [
      { month_start: "2024-03-01", boardings: 30, route_name: "79th" },
      { month_start: "2024-13-01", boardings: 40 },
      { month_start: "2024-01-01", boardings: -5 },
      { month_start: "2024-02-01", boardings: "20", route_name: "79th" },
    ],
  });
  assert.deepEqual(payload.records.map((record) => record.month_start), ["2024-02-01", "2024-03-01"]);
  assert.equal(payload.month_count, 2);
  assert.equal(normalizeHistoryPayload({ route_id: "79", records: [] }), null);
  assert.equal(normalizeHistoryPayload(null), null);
});

test("missing months become gaps, never zero boardings", () => {
  const series = buildMonthlySeries(HISTORY);
  assert.equal(series.points.length, 7);
  assert.equal(series.observedMonths, 5);
  const missing = series.points.filter((point) => !point.observed);
  assert.deepEqual(missing.map((point) => point.month), ["2024-03-01", "2024-06-01"]);
  assert.ok(missing.every((point) => point.boardings === null));
  assert.ok(series.points.every((point) => point.boardings !== 0));

  assert.deepEqual(missingMonthRanges(HISTORY), [
    { start: "2024-03-01", end: "2024-03-01", month_count: 1 },
    { start: "2024-06-01", end: "2024-06-01", month_count: 1 },
  ]);
});

test("chart splits the line at gaps and uses observed values only", () => {
  const model = chartModel(HISTORY, { width: 700, height: 260 });
  assert.equal(model.hasData, true);
  assert.equal(model.segments.length, 3, "three observed runs separated by two gaps");
  assert.deepEqual(model.segments.map((segment) => segment.points.length), [2, 2, 1]);
  assert.equal(model.domainMax, 180 * 1.1);
  const drawn = model.segments.flatMap((segment) => segment.points.map((point) => point.boardings));
  assert.deepEqual(drawn, [100, 120, 140, 160, 180]);
  assert.ok(model.segments.every((segment) => !segment.path.includes("NaN")));
  assert.equal(model.xTicks[0].month, "2024-01-01");
  assert.equal(model.xTicks.at(-1).month, "2024-07-01");
});

test("chart handles empty history and a single observation", () => {
  const empty = chartModel({ route_id: "79", records: [] });
  assert.equal(empty.hasData, false);
  assert.deepEqual(empty.segments, []);

  const single = chartModel({
    route_id: "1", records: [{ month_start: "2024-01-01", boardings: 50, route_name: "One" }],
  });
  assert.equal(single.segments.length, 1);
  assert.equal(single.segments[0].points.length, 1);
  assert.equal(Number.isFinite(single.segments[0].points[0].x), true);
});

test("summary reports span, observed months, gaps, and name changes", () => {
  const summary = summarizeRoute(HISTORY);
  assert.equal(summary.routeId, "79");
  assert.equal(summary.firstMonth, "2024-01-01");
  assert.equal(summary.lastMonth, "2024-07-01");
  assert.equal(summary.observedMonths, 5);
  assert.equal(summary.missingMonths, 2);
  assert.equal(summary.latestMonth, "2024-07-01");
  assert.equal(summary.latestBoardings, 180);
  assert.equal(summary.nameChanges, 1);
  assert.equal(summarizeRoute({ route_id: "79", records: [] }), null);
});

test("month helpers validate and format", () => {
  assert.equal(monthIndex("2024-02-01"), 2024 * 12 + 1);
  assert.equal(monthIndex("2024-02-15"), null);
  assert.equal(monthIndex("2024-13-01"), null);
  assert.equal(monthLabel("2024-02-01"), "Feb 2024");
  assert.equal(monthLabel("2024-02-01", { withYear: false }), "Feb ’24");
  assert.equal(monthLabel("nope"), "");
  assert.equal(formatNumber(1234567), "1,234,567");
  assert.equal(formatNumber(null), "—");
});

test("user and API text is escaped before HTML rendering", () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)">'), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  assert.equal(escapeHtml("O'Brien & Sons"), "O&#39;Brien &amp; Sons");
  assert.equal(escapeHtml(null), "");
});

test("API errors produce actionable, user-safe messages", () => {
  assert.match(describeError({ status: 404 }, "dataset"), /not available from this server/i);
  assert.match(describeError({ status: 404 }, "route"), /no stored monthly records/i);
  assert.match(describeError({ status: 503 }), /unavailable/i);
  assert.match(describeError({ status: 500 }), /reported an error/i);
  assert.match(describeError({ name: "TypeError", message: "Failed to fetch" }), /Could not reach/i);
  assert.equal(describeError({ message: "Something specific" }), "Something specific");
});

test("the explorer calls only public read-only monthly endpoints", async () => {
  const app = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../web/index.html", import.meta.url), "utf8");
  const explorerBlock = app.slice(app.indexOf("MONTHLY"), app.indexOf("function setup()"));

  assert.match(explorerBlock, /\/api\/public\/monthly-data\/routes/);
  assert.doesNotMatch(explorerBlock, /\/api\/monthly-data\/(upload|status)/);
  assert.doesNotMatch(explorerBlock, /Authorization/);
  assert.doesNotMatch(explorerBlock, /localStorage/);
  assert.doesNotMatch(explorerBlock, /busForecastApiKey/);

  // Transparency: dataset identity, attribution, and the City's required disclaimer.
  assert.match(html, /Chicago Transit Authority/);
  assert.match(html, /monthly boardings/i);
  assert.match(html, /not unique passengers/i);
  assert.match(
    html,
    /This site provides applications using data that has been modified for use from its original source, www\.cityofchicago\.org, the official website of the City of Chicago\./,
  );
  assert.match(html, /makes no claims as to the content, accuracy, timeliness, or completeness/);
  assert.match(html, /used at one’s own risk|used at one's own risk/);
  assert.doesNotMatch(html, /unique passengers reached/i);
  // The page must state that the data is historical, not a live feed.
  assert.match(html, /not real-time tracking/i);
  assert.match(html, /not live tracking/i);
});

test("monthly explorer UI ids referenced by the app exist in the shell", async () => {
  const [app, html] = await Promise.all([
    readFile(new URL("../web/app.js", import.meta.url), "utf8"),
    readFile(new URL("../web/index.html", import.meta.url), "utf8"),
  ]);
  const referenced = new Set([...app.matchAll(/\$\("#(monthly[\w-]*)"\)/g)].map((match) => match[1]));
  assert.ok(referenced.size >= 6, `expected several monthly ids, found ${[...referenced].join(", ")}`);
  const declared = new Set([...html.matchAll(/\bid="([\w-]+)"/g)].map((match) => match[1]));
  const missing = [...referenced].filter((id) => !declared.has(id));
  assert.deepEqual(missing, []);
});
