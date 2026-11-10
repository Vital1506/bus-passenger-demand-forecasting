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

