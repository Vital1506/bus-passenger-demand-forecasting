"""Service layer for GTFS Transit Network Explorer.

Provides:
- Multi-agency discovery and filtering
- Route catalogs with direction and trip summaries
- Stop catalogs with real coordinates
- Schedule bounds (first and last service times)
- Service calendars (operating days and date spans)
- High-fidelity route alignment shapes
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import (
    GTFSAgency,
    GTFSCalendar,
    GTFSFeed,
    GTFSRoute,
    GTFSShape,
    GTFSStop,
    GTFSStopTime,
    GTFSTrip,
)


def _get_active_feed_id(session: Session, agency_id: str | None = None) -> str | None:
    stmt = select(GTFSFeed.feed_id).where(GTFSFeed.is_active.is_(True))
    if agency_id:
        stmt = stmt.where(GTFSFeed.agency_id == agency_id)
    stmt = stmt.order_by(GTFSFeed.imported_at_utc.desc()).limit(1)
    res = session.execute(stmt).scalar_one_or_none()
    if not res:
        # Fallback to any latest feed
        res = session.execute(select(GTFSFeed.feed_id).order_by(GTFSFeed.imported_at_utc.desc()).limit(1)).scalar_one_or_none()
    return res


def list_feeds(session: Session) -> list[dict[str, Any]]:
    """List all imported GTFS feeds."""
    feeds = session.execute(
        select(GTFSFeed).order_by(GTFSFeed.imported_at_utc.desc())
    ).scalars().all()

    result = []
    for f in feeds:
        summary_data = {}
        if f.summary_json:
            try:
                summary_data = json.loads(f.summary_json)
            except Exception:
                pass
        result.append({
            "feed_id": f.feed_id,
            "agency_id": f.agency_id,
            "imported_at_utc": f.imported_at_utc.isoformat(),
            "sha256": f.sha256,
            "is_active": f.is_active,
            "summary": summary_data,
        })
    return result


def list_agencies(session: Session, feed_id: str | None = None) -> list[dict[str, Any]]:
    """List distinct transit operators."""
    f_id = feed_id or _get_active_feed_id(session)
    if not f_id:
        return []

    rows = session.execute(
        select(GTFSAgency).where(GTFSAgency.feed_id == f_id).order_by(GTFSAgency.agency_name)
    ).scalars().all()

    return [
        {
            "agency_id": a.agency_id,
            "agency_name": a.agency_name,
            "agency_url": a.agency_url,
            "agency_timezone": a.agency_timezone,
            "agency_phone": a.agency_phone,
        }
        for a in rows
    ]


def list_routes(
    session: Session,
    agency_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    feed_id: str | None = None,
) -> list[dict[str, Any]]:
    """Search and list network routes with trip counts."""
    f_id = feed_id or _get_active_feed_id(session, agency_id=agency_id)
    if not f_id:
        return []

    stmt = select(GTFSRoute).where(GTFSRoute.feed_id == f_id)
    if agency_id:
        stmt = stmt.where(GTFSRoute.agency_id == agency_id)

    routes = session.execute(stmt).scalars().all()

    q_clean = (query or "").strip().casefold()
    filtered = []
    for r in routes:
        name_str = f"{r.route_id} {r.route_short_name or ''} {r.route_long_name or ''}".casefold()
        if not q_clean or q_clean in name_str:
            filtered.append(r)

    filtered.sort(key=lambda r: (r.route_short_name or r.route_id, r.route_id))
    selected = filtered[:limit]

    # Batch count trips per route
    route_ids = [r.route_id for r in selected]
    trip_counts = dict(
        session.execute(
            select(GTFSTrip.route_id, func.count(GTFSTrip.trip_id))
            .where(GTFSTrip.feed_id == f_id, GTFSTrip.route_id.in_(route_ids))
            .group_by(GTFSTrip.route_id)
        ).all()
    )

    return [
        {
            "route_id": r.route_id,
            "agency_id": r.agency_id,
            "route_short_name": r.route_short_name or r.route_id,
            "route_long_name": r.route_long_name or "",
            "route_type": r.route_type,
            "route_color": r.route_color,
            "route_text_color": r.route_text_color,
            "trips_count": trip_counts.get(r.route_id, 0),
        }
        for r in selected
    ]


def get_route_details(
    session: Session,
    route_id: str,
    feed_id: str | None = None,
) -> dict[str, Any] | None:
    """Return comprehensive route intelligence: stops, calendar, bounds, and geometry."""
    f_id = feed_id or _get_active_feed_id(session)
    if not f_id:
        return None

    route = session.execute(
        select(GTFSRoute).where(GTFSRoute.feed_id == f_id, GTFSRoute.route_id == route_id)
    ).scalar_one_or_none()

    if not route:
        return None

    # 1. Trips on this route
    trips = session.execute(
        select(GTFSTrip).where(GTFSTrip.feed_id == f_id, GTFSTrip.route_id == route_id)
    ).scalars().all()

    trip_ids = [t.trip_id for t in trips]
    service_ids = {t.service_id for t in trips}
    shape_ids = {t.shape_id for t in trips if t.shape_id}

    # 2. Operating calendar details
    calendars = session.execute(
        select(GTFSCalendar).where(GTFSCalendar.feed_id == f_id, GTFSCalendar.service_id.in_(service_ids))
    ).scalars().all()

    calendar_info = []
    for c in calendars:
        days = []
        if c.monday: days.append("Mon")
        if c.tuesday: days.append("Tue")
        if c.wednesday: days.append("Wed")
        if c.thursday: days.append("Thu")
        if c.friday: days.append("Fri")
        if c.saturday: days.append("Sat")
        if c.sunday: days.append("Sun")
        calendar_info.append({
            "service_id": c.service_id,
            "days": days,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat(),
        })

    # 3. Schedule Bounds (Earliest and Latest departure times)
    time_bounds = session.execute(
        select(
            func.min(GTFSStopTime.departure_time),
            func.max(GTFSStopTime.departure_time),
        ).where(GTFSStopTime.feed_id == f_id, GTFSStopTime.trip_id.in_(trip_ids))
    ).first()

    first_departure, last_departure = time_bounds if time_bounds else (None, None)

    # 4. Ordered Stops along the representative trip
    # Pick the trip with the most stops as the representative alignment
    rep_trip_id = None
    if trip_ids:
        trip_stop_counts = session.execute(
            select(GTFSStopTime.trip_id, func.count(GTFSStopTime.id))
            .where(GTFSStopTime.feed_id == f_id, GTFSStopTime.trip_id.in_(trip_ids))
            .group_by(GTFSStopTime.trip_id)
            .order_by(func.count(GTFSStopTime.id).desc())
            .limit(1)
        ).first()
        if trip_stop_counts:
            rep_trip_id = trip_stop_counts[0]

    ordered_stops = []
    if rep_trip_id:
        stop_times = session.execute(
            select(GTFSStopTime, GTFSStop)
            .join(GTFSStop, (GTFSStop.feed_id == GTFSStopTime.feed_id) & (GTFSStop.stop_id == GTFSStopTime.stop_id))
            .where(GTFSStopTime.feed_id == f_id, GTFSStopTime.trip_id == rep_trip_id)
            .order_by(GTFSStopTime.stop_sequence)
        ).all()

        for st, s in stop_times:
            ordered_stops.append({
                "stop_id": s.stop_id,
                "stop_name": s.stop_name,
                "stop_lat": s.stop_lat,
                "stop_lon": s.stop_lon,
                "stop_sequence": st.stop_sequence,
                "arrival_time": st.arrival_time,
                "departure_time": st.departure_time,
            })

    # 5. Route Geometry from shapes.txt
    shape_points = []
    if shape_ids:
        # Pick first shape_id
        primary_shape_id = sorted(shape_ids)[0]
        points = session.execute(
            select(GTFSShape)
            .where(GTFSShape.feed_id == f_id, GTFSShape.shape_id == primary_shape_id)
            .order_by(GTFSShape.shape_pt_sequence)
        ).scalars().all()
        shape_points = [[p.shape_pt_lat, p.shape_pt_lon] for p in points]

    return {
        "route_id": route.route_id,
        "agency_id": route.agency_id,
        "route_short_name": route.route_short_name or route.route_id,
        "route_long_name": route.route_long_name or "",
        "route_type": route.route_type,
        "route_color": route.route_color,
        "route_text_color": route.route_text_color,
        "trips_count": len(trips),
        "first_service_time": first_departure,
        "last_service_time": last_departure,
        "calendar": calendar_info,
        "stops": ordered_stops,
        "stops_count": len(ordered_stops),
        "shape_points": shape_points,
        "has_shape": len(shape_points) > 0,
    }


def list_stops(
    session: Session,
    query: str | None = None,
    limit: int = 150,
    feed_id: str | None = None,
) -> list[dict[str, Any]]:
    """List network stops with geographic coordinates."""
    f_id = feed_id or _get_active_feed_id(session)
    if not f_id:
        return []

    stmt = select(GTFSStop).where(GTFSStop.feed_id == f_id)
    stops = session.execute(stmt).scalars().all()

    q_clean = (query or "").strip().casefold()
    filtered = []
    for s in stops:
        if not q_clean or q_clean in s.stop_name.casefold() or q_clean in s.stop_id.casefold():
            filtered.append(s)

    filtered.sort(key=lambda s: s.stop_name)
    return [
        {
            "stop_id": s.stop_id,
            "stop_name": s.stop_name,
            "stop_code": s.stop_code,
            "stop_lat": s.stop_lat,
            "stop_lon": s.stop_lon,
            "zone_id": s.zone_id,
        }
        for s in filtered[:limit]
    ]
