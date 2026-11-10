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

