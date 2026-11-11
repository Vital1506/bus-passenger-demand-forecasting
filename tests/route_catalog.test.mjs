import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { canonicalRoute, matchDestinations, normalizePlaceLabel, placeSuggestions } from "../web/route_catalog.mjs";

const catalog = JSON.parse(await readFile(new URL("../web/route_catalog.json", import.meta.url), "utf8"));

test("bundled route catalogue contains usable origin and destination pairs", () => {
  const origins = Object.keys(catalog.origins);
  assert.ok(origins.length > 0);
  assert.ok(origins.includes("Sattur"));
  assert.ok(catalog.origins.Sattur.includes("Chennai"));
  assert.ok(Object.values(catalog.origins).every((destinations) => destinations.length > 0));
});

test("place labels normalize Unicode and repeated whitespace", () => {
  assert.equal(normalizePlaceLabel("  Sattur   →   Chennai  "), "Sattur → Chennai");
});

test("known route is matched case-insensitively and returns catalogue spelling", () => {
  assert.deepEqual(canonicalRoute(" sattur ", "CHENNAI", catalog), {
    origin: "Sattur", destination: "Chennai", routeId: "Sattur → Chennai", listed: true,
  });
});

test("unknown route remains a normalized custom label without false matching", () => {
  assert.deepEqual(canonicalRoute("  My   Town ", " New   Stop ", catalog), {
    origin: "My Town", destination: "New Stop", routeId: "My Town → New Stop", listed: false,
  });
  assert.deepEqual(matchDestinations("not in catalogue", catalog), []);
});

test("place suggestions search both route origins and destinations", () => {
  assert.ok(placeSuggestions("chen", catalog).includes("Chennai"));
  assert.ok(placeSuggestions("satt", catalog).includes("Sattur"));
  assert.ok(placeSuggestions("not a place", catalog).length === 0);
});

test("matching destinations rank ahead of other places when a start is known", () => {
  const suggestions = placeSuggestions("", catalog, matchDestinations("Sattur", catalog));
  assert.ok(matchDestinations("Sattur", catalog).includes(suggestions[0]));
  assert.ok(suggestions.length <= 10);
});

test("place suggestions prioritize prefix matches and honor the result limit", () => {
  const suggestions = placeSuggestions("chen", { origins: { "Fort Chennai": ["Chennai Gate"], "New Town": ["Chenab City"] } }, [], 3);
  assert.ok(suggestions.length <= 3);
  assert.ok(suggestions.every((place) => place.toLocaleLowerCase("en-IN").includes("chen")));
  assert.ok(suggestions[0].toLocaleLowerCase("en-IN").startsWith("chen"));
  assert.ok(suggestions.indexOf("Fort Chennai") > suggestions.indexOf("Chennai Gate"));
});

