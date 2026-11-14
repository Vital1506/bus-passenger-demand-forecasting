import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [app, html, sw] = await Promise.all([
  readFile(new URL("../web/app.js", import.meta.url), "utf8"),
  readFile(new URL("../web/index.html", import.meta.url), "utf8"),
  readFile(new URL("../web/sw.js", import.meta.url), "utf8"),
]);

test("every statically referenced UI element exists in the app shell", () => {
  const referencedIds = new Set([...app.matchAll(/\$\(["']#([\w-]+)["']\)/g)].map((match) => match[1]));
  const declaredIds = new Set([...html.matchAll(/\bid=["']([\w-]+)["']/g)].map((match) => match[1]));
  const missingIds = [...referencedIds].filter((id) => !declaredIds.has(id)).sort();

  assert.deepEqual(missingIds, [], `UI selectors without matching HTML elements: ${missingIds.join(", ")}`);
});

test("origin and destination pickers expose accessible searchable listboxes", () => {
  for (const [inputId, listId] of [["countOrigin", "originSuggestions"], ["countDestination", "destinationSuggestions"]]) {
    assert.match(html, new RegExp(`id="${inputId}"[^>]*role="combobox"`));
    assert.match(html, new RegExp(`id="${listId}"[^>]*role="listbox"`));
    assert.match(html, new RegExp(`aria-controls="${listId}"`));
  }
  assert.match(app, /ArrowDown/);
  assert.match(app, /aria-activedescendant/);
});

test("public dashboard uses same-origin API and never overwrites operator data for the example", () => {
  assert.match(app, /function apiBase\(\) \{ return window\.location\.origin; \}/);
  assert.match(app, /\/api\/public-demo\/status/);
  assert.match(app, /Read-only synthetic example loaded; your operator data was not changed/);
  assert.doesNotMatch(html, /id="apiUrl"/);
  assert.doesNotMatch(html, /\+8\.4%|\+3\.1%|\+6\.7%/);
});

test("opening screen identifies bounded server wake-up and health check", () => {
  assert.match(app, /Waking up the server \(up to 60s\)/);
  assert.match(app, /api\("\/health"\)/);
});

test("transit network explorer declares accessible search and detail components", () => {
  assert.match(html, /data-view="network"/);
  assert.match(html, /id="networkPage"/);
  assert.match(html, /id="networkRouteSearch"/);
  assert.match(html, /id="networkRouteList"/);
  assert.match(html, /id="networkShapePreview"/);
  assert.match(html, /id="networkStopsList"/);
  assert.match(app, /\/api\/gtfs\/routes/);
  assert.match(app, /\/api\/gtfs\/agencies/);
  assert.match(app, /\/api\/gtfs\/upload/);
});

test("live operations dashboard declares accessible fleet radar and dispatch components", () => {
  assert.match(html, /data-view="operations"/);
  assert.match(html, /id="operationsPage"/);
  assert.match(html, /id="opsFleetMap"/);
  assert.match(html, /id="opsAlertsList"/);
  assert.match(html, /id="opsRecommendationsList"/);
  assert.match(html, /id="opsVehiclesTableBody"/);
  assert.match(app, /\/api\/operations\/metrics/);
  assert.match(app, /\/api\/operations\/alerts/);
  assert.match(app, /\/api\/operations\/dispatch-recommendations/);
  assert.match(app, /\/api\/realtime\/vehicles/);
});

test("service planner and occupancy pages declare required interactive elements", () => {
  assert.match(html, /data-view="planner"/);
  assert.match(html, /id="plannerPage"/);
  assert.match(html, /id="runSimButton"/);
  assert.match(html, /id="simSummaryText"/);

  assert.match(html, /data-view="occupancy"/);
  assert.match(html, /id="occupancyPage"/);
  assert.match(html, /id="loadTripOccButton"/);
  assert.match(html, /id="occStopsTableBody"/);
});

test("ai assistant declares accessible toggle, chat panel, and interactive controls", () => {
  assert.match(html, /id="aiChatToggle"/);
  assert.match(html, /id="aiChatPanel"/);
  assert.match(html, /id="aiChatMessages"/);
  assert.match(html, /id="aiChatInput"/);
  assert.match(html, /id="aiSendBtn"/);
  assert.match(html, /id="aiSuggestedPrompts"/);
  assert.match(app, /\/api\/ai\/chat/);
  assert.match(app, /\/api\/ai\/status/);
});

test("service worker and app script employ cache-busting and bypass health/ready checks", () => {
  assert.match(html, /<script type="module" src="\/static\/app\.js\?v=\d+"><\/script>/);
  assert.match(sw, /const CACHE = "bus-demand-ui-v11";/);
  assert.match(sw, /url\.pathname\.startsWith\("\/health"\)/);
  assert.match(sw, /url\.pathname\.startsWith\("\/ready"\)/);
  assert.match(sw, /url\.pathname\.startsWith\("\/api\/"\)/);
});

test("startup waits for actual forecast readiness and retries when it is unavailable", () => {
  assert.match(app, /ready\.status === "ready" && ready\.forecasts_ready === true/);
  assert.match(app, /Forecast service is still initializing/);
  assert.match(app, /waiting for forecast readiness/);
});

test("service worker caches only successful non-opaque static responses", () => {
  assert.match(sw, /response\.ok && response\.type !== "opaque"/);
});
