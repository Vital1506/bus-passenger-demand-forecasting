export function normalizePlaceLabel(value) {
  return String(value ?? "").normalize("NFKC").trim().replace(/\s+/gu, " ");
}

function findCanonical(value, candidates) {
  const key = normalizePlaceLabel(value).toLocaleLowerCase("en-IN");
  return candidates.find((candidate) => normalizePlaceLabel(candidate).toLocaleLowerCase("en-IN") === key) || "";
}

export function matchDestinations(origin, catalog) {
  if (!catalog?.origins || typeof catalog.origins !== "object") return [];
  const canonicalOrigin = findCanonical(origin, Object.keys(catalog.origins));
  return canonicalOrigin ? [...catalog.origins[canonicalOrigin]] : [];
}

const placeIndexCache = new WeakMap();

function placeIndex(catalog) {
  if (!catalog || typeof catalog !== "object") return { usage: new Map(), places: [] };
  const cached = placeIndexCache.get(catalog);
  if (cached) return cached;
  const origins = Object.keys(catalog.origins || {});
  const usage = new Map();
  for (const origin of origins) {
    usage.set(origin, (usage.get(origin) || 0) + 1);
    for (const destination of catalog.origins[origin] || []) {
      usage.set(destination, (usage.get(destination) || 0) + 1);
    }
  }
  const index = { usage, places: [...usage.keys()] };
  placeIndexCache.set(catalog, index);
  return index;
}

export function placeSuggestions(query, catalog, preferred = [], limit = 10) {
  const { usage, places } = placeIndex(catalog);
  const preferredKeys = new Set(preferred.map((value) => normalizePlaceLabel(value).toLocaleLowerCase("en-IN")));
  const normalizedQuery = normalizePlaceLabel(query).toLocaleLowerCase("en-IN");
  return places
    .filter((place) => !normalizedQuery || normalizePlaceLabel(place).toLocaleLowerCase("en-IN").includes(normalizedQuery))
    .sort((left, right) => {
      const leftKey = normalizePlaceLabel(left).toLocaleLowerCase("en-IN");
      const rightKey = normalizePlaceLabel(right).toLocaleLowerCase("en-IN");
      const leftPrefix = normalizedQuery && leftKey.startsWith(normalizedQuery) ? 0 : 1;
      const rightPrefix = normalizedQuery && rightKey.startsWith(normalizedQuery) ? 0 : 1;
      if (leftPrefix !== rightPrefix) return leftPrefix - rightPrefix;
      const leftPreferred = preferredKeys.has(leftKey) ? 0 : 1;
      const rightPreferred = preferredKeys.has(rightKey) ? 0 : 1;
      if (leftPreferred !== rightPreferred) return leftPreferred - rightPreferred;
      if (!normalizedQuery && usage.get(left) !== usage.get(right)) return usage.get(right) - usage.get(left);
      return left.localeCompare(right, "en-IN");
    })
    .slice(0, Math.max(0, limit));
}

export function canonicalRoute(origin, destination, catalog) {
  const cleanedOrigin = normalizePlaceLabel(origin);
  const cleanedDestination = normalizePlaceLabel(destination);
  const knownOrigins = Object.keys(catalog?.origins || {});
  const canonicalOrigin = findCanonical(cleanedOrigin, knownOrigins) || cleanedOrigin;
  const destinations = matchDestinations(canonicalOrigin, catalog);
  const canonicalDestination = findCanonical(cleanedDestination, destinations) || cleanedDestination;
  return {
    origin: canonicalOrigin,
    destination: canonicalDestination,
    routeId: `${canonicalOrigin} → ${canonicalDestination}`,
    listed: Boolean(findCanonical(cleanedDestination, destinations)),
  };
}

