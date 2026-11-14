const CACHE = "bus-demand-ui-v11";
const APP_SHELL = ["/", "/static/app.css", "/static/app.js?v=11", "/static/route_catalog.mjs", "/static/route_catalog.json", "/static/icon.svg", "/static/icon-192.png", "/static/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", (event) => event.waitUntil(
  caches.open(CACHE)
    .then((cache) => cache.addAll(APP_SHELL))
    .then(() => self.skipWaiting())
));

self.addEventListener("activate", (event) => event.waitUntil(
  caches.keys()
    .then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
    ))
    .then(() => self.clients.claim())
));

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Never cache health/readiness probes or API traffic. These responses must
  // reflect the live backend and may contain user-specific or operational data.
  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname === "/health" ||
    url.pathname === "/ready" ||
    url.pathname.startsWith("/api/")
  ) return;

  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok && response.type !== "opaque") {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    }).catch(async () => (await caches.match(request)) || Response.error())
  );
});
