import { test, describe, it } from "node:test";
import assert from "node:assert";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = readFileSync(join(__dirname, "..", "web", "app.js"), "utf8");
const html = readFileSync(join(__dirname, "..", "web", "index.html"), "utf8");

// Capture GTFS UI ids referenced in app.js as both "#gtfsX" and "gtfsX" forms.
const GTFS_SELECTOR_RE = /\$?["'](#{0,1}(gtfs[A-Za-z0-9_-]+)["']){0,1}/g;

test("GTFS network page references at least 8 unique GTFS UI ids in app.js", () => {
  const referenced = [...app.matchAll(GTFS_SELECTOR_RE)].map((m) => m[2]).filter(Boolean);
  assert.ok(referenced.length >= 8, "expected at least 8 GTFS id references in app.js: found " + referenced.length);
  assert.ok(new Set(referenced).size >= 8, "expected at least 8 unique GTFS ids in app.js: found " + new Set(referenced).size);
});

test("every GTFS UI id referenced in app.js exists in the HTML shell", () => {
  const referenced = [...app.matchAll(GTFS_SELECTOR_RE)].map((m) => m[2]).filter(Boolean);
  const declared = new Set([...html.matchAll(/id=["']([^"']+)["']/g)].map((m) => m[1]));
  const missing = [...new Set(referenced)].filter((id) => !declared.has(id));
  assert.deepStrictEqual(missing, [], "UI selectors without matching HTML elements: " + missing.join(", "));
});

test("GTFS network page uses only public read-only endpoints", () => {
  assert.match(app, /\/api\/gtfs\/agencies/);
  assert.match(app, /\/api\/gtfs\/routes/);
  assert.doesNotMatch(app, /\/api\/gtfs\/upload/);
});

test("GTFS network HTML documents the feed source", () => {
  assert.match(html, /Imported GTFS Schedule feed/);
  assert.match(html, /agency\.txt/);
});

test("GTFS network nav button announces the page", () => {
  assert.match(app, /gtfsNetworkBadge/);
});

if (existsSync(join(__dirname, "..", "web", "gtfs_network.mjs"))) {
  const helpers = await import("./../web/gtfs_network.mjs");
  const state = helpers.buildGtfsNetworkState();
  test("buildGtfsNetworkState starts unloaded and unqueried", () => {
    assert.strictEqual(state.loaded, false);
    assert.strictEqual(state.query, "");
    assert.ok(Array.isArray(state.routes));
    assert.strictEqual(state.routes.length, 0);
  });

  test("gtfsRouteSearch matches on agency, number, and name", () => {
    const routes = [
      { agency_code: "LTD", route_id: "L1", route_short_name: "25", route_long_name: "Airport Express" },
      { agency_code: "LTD", route_id: "L2", route_short_name: "26", route_long_name: "Airport Local" },
      { agency_code: "BART", route_id: "A1", route_short_name: "1", route_long_name: "Central Line" },
    ];
    assert.deepStrictEqual(helpers.gtfsRouteSearch(routes, "airport"), routes.slice(0, 2));
    assert.deepStrictEqual(helpers.gtfsRouteSearch(routes, "26"), routes.slice(1, 2));
    assert.deepStrictEqual(helpers.gtfsRouteSearch(routes, "central"), routes.slice(2, 3));
  });

  test("gtfsStopSequence orders stops by stop_sequence", () => {
    const stops = [
      { stop_id: "S3", stop_name: "C", stop_sequence: 3, stop_lat: 1, stop_lon: 1 },
      { stop_id: "S1", stop_name: "A", stop_sequence: 1, stop_lat: 0, stop_lon: 0 },
      { stop_id: "S2", stop_name: "B", stop_sequence: 2, stop_lat: 0, stop_lon: 0 },
    ];
    assert.deepStrictEqual(helpers.gtfsStopSequence(stops), ["S1", "S2", "S3"]);
  });

  test("routeTypeLabel maps zero and known types", () => {
    assert.strictEqual(helpers.routeTypeLabel(0), "https://gtfs.org/1.0/transit#tram");
    assert.strictEqual(helpers.routeTypeLabel(2), "https://gtfs.org/1.0/transit#subway");
    assert.strictEqual(helpers.routeTypeLabel(100), "Unknown (100)");
  });
}

describe("GTFS Route Network DOM contract", () => {
  it("renders the loading badge before the page is ready", () => {
    assert.match(app, /gtfsNetworkBadge/);
  });

  it("declares a searchable GTFS route network section", () => {
    assert.match(html, /id="routesPage-v2"/);
    assert.match(html, /id="gtfsAgencySearch"/);
  });
});
