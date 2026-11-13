import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [app, html] = await Promise.all([
  readFile(new URL("../web/app.js", import.meta.url), "utf8"),
  readFile(new URL("../web/index.html", import.meta.url), "utf8"),
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
