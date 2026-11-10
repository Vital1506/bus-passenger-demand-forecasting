import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { canonicalRoute, matchDestinations, normalizePlaceLabel } from "../web/route_catalog.mjs";

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

