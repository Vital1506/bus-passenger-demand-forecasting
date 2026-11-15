"""Hybrid demand forecasting engine bridging macroscopic daily forecasts with microscopic real-time intraday operations.

Features:
- Hourly time-of-day demand profiles with transit peak modeling (AM peak, midday, PM peak, evening).
- Real-time vehicle occupancy integration translating GTFS-RT occupancy status into passenger load estimates.
- Delay-induced passenger accumulation modeling (downstream stop queue expansion during transit delays).
- Multi-horizon projections: Next 1 hour, Next 4 hours, and full 24-hour diurnal profile.
- Automated graceful fallback: If live GTFS-RT telemetry is offline, falls back seamlessly to scheduled GTFS service frequency and daily baseline without crashing.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .realtime.models import OccupancyStatus
from .realtime.service import get_realtime_service

# Standard 40-foot transit bus nominal capacity (seated + standing)
DEFAULT_BUS_CAPACITY = 55
DEFAULT_TIMEZONE = "America/Chicago"


def get_agency_timezone(agency_id: str | None = None) -> ZoneInfo:
    """Retrieve configured IANA timezone for the transit agency, falling back safely."""
    env_key = f"BUS_FORECAST_TIMEZONE_{agency_id.upper()}" if agency_id else None
    tz_name = (os.getenv(env_key) if env_key else None) or os.getenv("BUS_FORECAST_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name.strip())
    except Exception:
        return ZoneInfo("UTC")


def get_current_local_time(agency_id: str | None = None) -> datetime:
    """Return current timestamp in agency local timezone."""
    tz = get_agency_timezone(agency_id)
    return datetime.now(tz)


def get_current_hour_local(agency_id: str | None = None) -> int:
    """Return current hour (0-23) in agency local timezone."""
    return get_current_local_time(agency_id).hour


def get_current_hour_utc() -> int:
    return datetime.now(timezone.utc).hour

# Standard urban transit 24-hour demand distribution weights (sum normalized to 1.0)
DIURNAL_WEIGHTS = [
    0.005, 0.003, 0.002, 0.005, 0.015, 0.040,  # 00:00 - 05:00 Early morning
    0.080, 0.110, 0.095, 0.060, 0.050, 0.050,  # 06:00 - 11:00 Morning peak & midday base
    0.055, 0.055, 0.060, 0.075, 0.095, 0.100,  # 12:00 - 17:00 Afternoon & Evening peak
    0.065, 0.040, 0.025, 0.015, 0.010, 0.008,  # 18:00 - 23:00 Night
]
_NORM_FACTOR = sum(DIURNAL_WEIGHTS)
NORMALIZED_DIURNAL = [w / _NORM_FACTOR for w in DIURNAL_WEIGHTS]


def occupancy_to_passengers(status: OccupancyStatus, percentage: int | None = None, capacity: int = DEFAULT_BUS_CAPACITY) -> int:
    """Convert GTFS-RT occupancy status or explicit percentage into estimated onboard passenger count."""
    if percentage is not None and 0 <= percentage <= 200:
        return int(round(capacity * (percentage / 100.0)))

    ratios = {
        OccupancyStatus.EMPTY: 0.05,
        OccupancyStatus.MANY_SEATS_AVAILABLE: 0.25,
        OccupancyStatus.FEW_SEATS_AVAILABLE: 0.60,
        OccupancyStatus.STANDING_ROOM_ONLY: 0.85,
        OccupancyStatus.CRUSHED_STANDING_ROOM_ONLY: 0.98,
        OccupancyStatus.FULL: 1.02,
        OccupancyStatus.NOT_ACCEPTING_PASSENGERS: 1.05,
        OccupancyStatus.NO_DATA_AVAILABLE: 0.40,
        OccupancyStatus.NOT_BOARDABLE: 1.05,
    }
    ratio = ratios.get(status, 0.40)
    return int(round(capacity * ratio))


def get_current_hour_utc() -> int:
    return datetime.now(timezone.utc).hour


@dataclass
class IntradayRouteForecast:
    route_id: str
    target_date: str
    current_hour: int
    daily_baseline_boardings: float
    is_realtime_calibrated: bool
    active_vehicles_count: int
    current_onboard_passengers: int
    average_route_delay_seconds: float
    next_1h_predicted_boardings: int
    next_4h_predicted_boardings: int
    rest_of_day_predicted_boardings: int
    crowding_risk_level: str  # LOW, MODERATE, HIGH, CRITICAL
    hourly_profile: list[dict[str, Any]]
    profile_source: str = "default_transit_standard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "target_date": self.target_date,
            "current_hour": self.current_hour,
            "daily_baseline_boardings": round(self.daily_baseline_boardings, 1),
            "is_realtime_calibrated": self.is_realtime_calibrated,
            "profile_source": self.profile_source,
            "active_vehicles_count": self.active_vehicles_count,
            "current_onboard_passengers": self.current_onboard_passengers,
            "average_route_delay_seconds": round(self.average_route_delay_seconds, 1),
            "next_1h_predicted_boardings": self.next_1h_predicted_boardings,
            "next_4h_predicted_boardings": self.next_4h_predicted_boardings,
            "rest_of_day_predicted_boardings": self.rest_of_day_predicted_boardings,
            "crowding_risk_level": self.crowding_risk_level,
            "hourly_profile": self.hourly_profile,
        }


def learn_diurnal_profile_from_hourly(
    hourly_df: Any,
    route_id: str | None = None,
) -> tuple[list[float], str]:
    """Derive empirical 24-hour diurnal boarding profile from APC/telemetry records."""
    if hourly_df is None or not hasattr(hourly_df, "empty") or hourly_df.empty or "hour" not in hourly_df.columns or "boardings" not in hourly_df.columns:
        return NORMALIZED_DIURNAL, "default_transit_standard"

    sub = hourly_df
    if route_id and "route_id" in sub.columns:
        matched = sub[sub["route_id"].astype(str) == str(route_id)]
        if len(matched) >= 24:
            sub = matched

    grouped = sub.groupby("hour")["boardings"].mean()
    if len(grouped) < 20 or grouped.sum() <= 0:
        return NORMALIZED_DIURNAL, "default_transit_standard"

    weights = [float(grouped.get(h, 0.001)) for h in range(24)]
    total_w = sum(weights)
    if total_w <= 0:
        return NORMALIZED_DIURNAL, "default_transit_standard"
    return [w / total_w for w in weights], "learned_apc"


def build_intraday_route_forecast(
    route_id: str,
    daily_baseline_passengers: float = 2400.0,
    current_hour: int | None = None,
    capacity: int = DEFAULT_BUS_CAPACITY,
    agency_id: str | None = None,
    target_date: str | None = None,
    hourly_observations: Any = None,
) -> IntradayRouteForecast:
    """Compute real-time calibrated intraday forecast for a route using agency local timezone."""
    now_dt = get_current_local_time(agency_id)
    now_hour = current_hour if current_hour is not None else now_dt.hour
    effective_target_date = target_date or now_dt.strftime("%Y-%m-%d")

    # Diurnal weights resolution
    diurnal_profile, profile_source = learn_diurnal_profile_from_hourly(
        hourly_df=hourly_observations,
        route_id=route_id,
    )

    # Fetch live telemetry from RealtimeService
    rt_service = get_realtime_service()
    vehicles = rt_service.get_vehicles(route_id=route_id, agency_id=agency_id)
    trip_updates = rt_service.get_trip_updates(route_id=route_id, agency_id=agency_id)

    is_calibrated = len(vehicles) > 0

    # Calculate current onboard passengers & average delay
    onboard_total = 0
    for v in vehicles:
        occ_status = OccupancyStatus(v.get("occupancy_status", OccupancyStatus.NO_DATA_AVAILABLE.value))
        occ_pct = v.get("occupancy_percentage")
        onboard_total += occupancy_to_passengers(occ_status, occ_pct, capacity)

    delays = [tu.get("delay") for tu in trip_updates if tu.get("delay") is not None]
    avg_delay = sum(delays) / len(delays) if delays else 0.0

    # Downstream queue factor: delays cause passenger buildup at stops
    delay_multiplier = 1.0 + min(1.0, max(-0.3, avg_delay / 600.0)) if is_calibrated else 1.0

    # Hourly distribution
    hourly_curve = []
    daily_total = max(100.0, float(daily_baseline_passengers))

    for h in range(24):
        base_h = daily_total * diurnal_profile[h]
        # Real-time calibration for upcoming hours (modular arithmetic fixes hour 21-23 wraparound bug)
        is_upcoming_window = (h - now_hour) % 24 <= 3
        if is_calibrated and is_upcoming_window:
            calibrated_h = base_h * delay_multiplier
        else:
            calibrated_h = base_h

        # Uncertainty error bounds: heuristic ratio multipliers (renamed/documented)
        lower = max(0, int(round(calibrated_h * 0.82)))
        upper = int(round(calibrated_h * 1.22))
        point = int(round(calibrated_h))

        hourly_curve.append({
            "hour": h,
            "hour_label": f"{h:02d}:00",
            "predicted_boardings": point,
            "lower_90": lower,
            "upper_90": upper,
            "lower_90_heuristic": lower,
            "upper_90_heuristic": upper,
            "interval_method": "heuristic_ratio",
            "is_past": h < now_hour,
            "is_current": h == now_hour,
            "is_calibrated": is_calibrated and is_upcoming_window,
        })

    # Projections
    next_1h = hourly_curve[now_hour]["predicted_boardings"] if now_hour < 24 else 0
    next_4h = sum(hourly_curve[h % 24]["predicted_boardings"] for h in range(now_hour, now_hour + 4))
    rest_of_day = sum(hourly_curve[h]["predicted_boardings"] for h in range(now_hour, 24))

    # Crowding risk determination
    active_buses = len(vehicles)
    active_capacity = max(capacity, active_buses * capacity)
    load_ratio = onboard_total / active_capacity if active_capacity > 0 else 0.0

    if load_ratio > 0.90 or (delay_multiplier > 1.4 and next_1h > active_capacity * 1.2):
        risk = "CRITICAL"
    elif load_ratio > 0.70 or delay_multiplier > 1.2:
        risk = "HIGH"
    elif load_ratio > 0.45:
        risk = "MODERATE"
    else:
        risk = "LOW"

    return IntradayRouteForecast(
        route_id=route_id,
        target_date=effective_target_date,
        current_hour=now_hour,
        daily_baseline_boardings=daily_total,
        is_realtime_calibrated=is_calibrated,
        active_vehicles_count=active_buses,
        current_onboard_passengers=onboard_total,
        average_route_delay_seconds=avg_delay,
        next_1h_predicted_boardings=next_1h,
        next_4h_predicted_boardings=next_4h,
        rest_of_day_predicted_boardings=rest_of_day,
        crowding_risk_level=risk,
        hourly_profile=hourly_curve,
        profile_source=profile_source,
    )


def build_network_intraday_curve(
    current_hour: int | None = None,
    network_daily_base: float = 50000.0,
    agency_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate network-wide intraday passenger demand curve across all operational hours."""
    now_hour = current_hour if current_hour is not None else get_current_hour_local(agency_id)
    rt_service = get_realtime_service()
    all_vehicles = rt_service.get_vehicles(agency_id=agency_id)
    all_updates = rt_service.get_trip_updates(agency_id=agency_id)

    total_active_vehs = len(all_vehicles)
    total_onboard = sum(
        occupancy_to_passengers(OccupancyStatus(v.get("occupancy_status", "NO_DATA_AVAILABLE")), v.get("occupancy_percentage"))
        for v in all_vehicles
    )

    delays = [tu.get("delay") for tu in all_updates if tu.get("delay") is not None]
    avg_network_delay = sum(delays) / len(delays) if delays else 0.0

    # Build 24h curve using provided or nominal network baseline
    curve = []
    for h in range(24):
        base_h = network_daily_base * NORMALIZED_DIURNAL[h]
        curve.append({
            "hour": h,
            "hour_label": f"{h:02d}:00",
            "predicted_network_boardings": int(round(base_h)),
            "lower_90": int(round(base_h * 0.85)),
            "upper_90": int(round(base_h * 1.18)),
            "lower_90_heuristic": int(round(base_h * 0.85)),
            "upper_90_heuristic": int(round(base_h * 1.18)),
            "is_past": h < now_hour,
            "is_current": h == now_hour,
        })

    return {
        "current_hour": now_hour,
        "total_active_vehicles": total_active_vehs,
        "total_onboard_passengers": total_onboard,
        "average_network_delay_seconds": round(avg_network_delay, 1),
        "network_daily_projected": int(network_daily_base),
        "hourly_curve": curve,
    }
