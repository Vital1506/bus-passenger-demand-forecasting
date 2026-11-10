"""Stable identity helpers for manually entered origin/destination routes."""

from __future__ import annotations

import unicodedata


def _route_parts(route_id: str) -> tuple[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", str(route_id)).split())
    origin, separator, destination = normalized.partition("→")
    if not separator:
        return None
    origin = " ".join(origin.split())
    destination = " ".join(destination.split())
    if not origin or not destination:
        return None
    return origin, destination


def canonicalize_manual_route_id(route_id: str, known_route_ids: set[str]) -> str:
    """Normalize a manual route and reuse known spelling regardless of case."""
    parts = _route_parts(route_id)
    if parts is None:
        return " ".join(unicodedata.normalize("NFKC", str(route_id)).split())

    origin, destination = parts
    key = (origin.casefold(), destination.casefold())
    for known_route_id in sorted(known_route_ids, key=lambda value: (value.casefold(), value)):
        known_parts = _route_parts(known_route_id)
        if known_parts and tuple(part.casefold() for part in known_parts) == key:
            return known_route_id
    return f"{origin} → {destination}"

