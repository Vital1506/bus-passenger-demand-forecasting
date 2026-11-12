/* GTFS route network search and render helpers */
import { searchRoutes } from "./monthly_explorer.mjs";

export function gtfsRouteSearch(routes, q, limit) {
  const list = Array.isArray(routes) ? routes : [];
  if (!q.trim()) {
    return list.slice().sort((a, b) => (Number(b.trip_count ?? 0)) - (Number(a.trip_count ?? 0)) || (Number(b.stop_count ?? 0)) - (Number(a.stop_count ?? 0))).slice(0, limit);
  }
  const needle = q.trim().toLocaleLowerCase("en");
  const scored = list.map((r) => {
    const fields = [
      r.route_id ? r.route_id.toLocaleLowerCase("en") : "",
      r.route_short_name ? r.route_short_name.toLocaleLowerCase("en") : "",
      r.route_long_name ? r.route_long_name.toLocaleLowerCase("en") : "",
      r.agency_code ? r.agency_code.toLocaleLowerCase("en") : "",
    ];
    const matched = fields.some((f) => f.includes(needle));
    const stopCount = Number(r.stop_count ?? 0);
    const tripCount = Number(r.trip_count ?? 0);
    return { route: r, matched, stopCount, tripCount };
  });
  const matched = scored.filter((s) => s.matched);
  matched.sort((a, b) => (b.tripCount - a.tripCount) || (b.stopCount - a.stopCount));
  const top = matched.slice(0, limit).map((s) => s.route);
  const topKeys = new Set(top.map((r) => r.route_id));
  const rest = scored.filter((s) => s.matched && !topKeys.has(s.route.route_id)).map((s) => s.route);
  rest.sort((a, b) => (b.tripCount - a.tripCount) || (b.stopCount - a.stopCount));
  return top.concat(rest);
}

export function routeTypeLabel(type) {
  if (type == null) return "—";
  const labels = {
    0: "tram",
    1: "rail",
    2: "subway",
    3: "bus",
    4: "ferry",
    5: "cableTram",
    6: "aerialLift",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
    13: "heavyRail",
  };
  const slug = labels[type];
  if (!slug) return `Unknown (${type})`;
  return `https://gtfs.org/1.0/transit#${slug}`;
}

export function gtfsStopSequence(stops) {
  const list = Array.isArray(stops) ? stops : [];
  const sorted = list.slice().sort((a, b) => (Number(a.stop_sequence ?? 0)) - (Number(b.stop_sequence ?? 0)));
  return sorted.map((s) => s.stop_id).filter(Boolean);
}

export function buildGtfsNetworkState(payload) {
  const list = Array.isArray(payload?.routes) ? payload.routes : [];
  const routes = list.filter((r) => r?.route_id && typeof r.route_id === "string" && r.route_id.trim());
  return {
    loaded: false,
    query: "",
    agencies: routes.map((r) => ({
      agency_code: r.agency_code || "",
      route_count: Number(r.trip_count ? 1 : 0) || 0,
      stop_count: Array.isArray(r.stops) ? r.stops.length : 0,
    })),
    routes,
  };
}
