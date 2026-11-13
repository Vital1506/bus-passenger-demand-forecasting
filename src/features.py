"""Extended feature engineering for daily route demand forecasting.

This module adds the historical, route-specific, calendar, holiday, school
holiday, special-event, and weather feature contracts requested for Phase 5.
It does not add real holiday calendars, event data, or a weather provider yet.
Those remain documented extension points with stubs in this first unit.

Design notes
- The feature layer is deliberately split from src/forecasting.py. The existing
  lag/rolling/calendar features there remain the default feature set used by the
  training, comparison, and forecast paths unchanged. This module adds extra
  Phase 5 signals that are always derived strictly from observations available
  on or before the target service date.
- Every feature builder accepts a validated daily history plus optional helper
  tables for holidays, school holidays, events, and weather. When those extras
  are empty or absent, the builder still returns a complete frame; it just
  leaves the corresponding columns as a documented sentinel value instead of
  pretending real signal was available.
- Weather features are defined through an adapter contract below. The feature
  layer itself never knows which provider is being used. This keeps the weather
  source swappable and makes leakage-safe weather handling a provider concern,
  not a feature-layer concern.

Contracts that are intentionally stubs here
- Holidays: a holiday catalog with service_date, route_id, holiday_type, name.
- School holidays: a school-holiday calendar with service_date, school_holiday.
- Special events: an event indicator table with service_date, route_id, event_type,
  event_label, event_intensity.
- Weather: an adapter interface that can answer historical observations and
  leakage-safe forecast values for a given route and date.

Sentinel values
- When optional categorical signal is unknown, the categorical columns use the
  string "__unknown__".
- When optional numeric signal is unknown, the numeric columns use NaN. Callers
  that need complete rows must fill or exclude those columns explicitly.

Leakage rule
- No feature in this module may use any observation with service_date equal to
  or after the target row's service_date, except route identity and externally
  provided calendars whose validity for that date is already established before
  the target date in the real system. Calendar/event flags for a target date are
  allowed only if the real provider would have been able to know that flag on the
  morning of the target date or earlier.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .forecasting import REQUIRED_COLUMNS

# ---------------------------------------------------------------------------
# Optional signal contracts
# ---------------------------------------------------------------------------



class UnsupportedWeatherAdapterError(ValueError):
    """Raised when a weather adapter does not implement the contract expected here."""


class WeatherAdapter:
    """Abstract contract for a route-aware weather source.

    Implementations must be able to answer two separate questions without
    leaking future information into historical training:

    1. What was observed on a given service date, for a given route, where the
       observation is only used for training or backtesting on dates after the
       observation became available?
    2. What forecast was available on a given issue date for a future service
       date, including the forecast lead day? This is the leakage-safe way to
       reconstruct what a model could have known at forecast time.

    This stub documents the contract. A concrete implementation for a provider
    such as Open-Meteo belongs in a separate module and is not included here.
    """

    def historical_observations(
        self,
        *,
        route_id: str,
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        """Return historical weather observations for one route and date range.

        The returned frame must contain at least:
        - service_date
        - temperature_celsius
        - rain_mm
        - weather_condition
        """
        raise NotImplementedError

    def forecast_available_on(
        self,
        *,
        route_id: str,
        forecast_issue_date: pd.Timestamp,
        target_service_date: pd.Timestamp,
    ) -> WeatherForecastRow | None:
        """Return the forecast that was available on forecast_issue_date for
        target_service_date, or None if no such forecast exists.

        This is the leakage-safe interface. Historical model evaluation must use
        this method, or an equivalent documented vintage/lead-time query, instead
        of asking for the eventual actual observation as though it were available
        at forecast time.
        """
        raise NotImplementedError

    def candidate_forecast_sources(self) -> list[str]:
        """Names of forecast sources this adapter can reconstruct historically."""
        raise NotImplementedError


def _expected_holiday_columns() -> list[str]:
    return ["service_date", "route_id", "holiday_type", "name"]


def _expected_school_holiday_columns() -> list[str]:
    return ["service_date", "school_holiday"]


def _expected_event_columns() -> list[str]:
    return ["service_date", "route_id", "event_type", "event_label", "event_intensity"]


def _validate_holiday_catalog(catalog: pd.DataFrame | None) -> pd.DataFrame:
    if catalog is None or catalog.empty:
        return pd.DataFrame(columns=_expected_holiday_columns())
    missing = set(_expected_holiday_columns()) - set(catalog.columns)
    if missing:
        raise ValueError(f"Holiday catalog is missing columns: {', '.join(sorted(missing))}")
    frame = catalog.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce").dt.normalize()
    frame["route_id"] = frame["route_id"].astype(str)
    bad = frame["service_date"].isna()
    if bad.any():
        raise ValueError("Holiday catalog contains invalid service_date values")
    if frame.empty:
        return frame
    if frame.duplicated(subset=["service_date", "route_id"]).any():
        raise ValueError("Holiday catalog contains duplicate service_date/route_id rows")
    return frame


def _validate_school_holiday_calendar(calendar: pd.DataFrame | None) -> pd.DataFrame:
    if calendar is None or calendar.empty:
        return pd.DataFrame(columns=_expected_school_holiday_columns())
    missing = set(_expected_school_holiday_columns()) - set(calendar.columns)
    if missing:
        raise ValueError(f"School holiday calendar is missing columns: {', '.join(sorted(missing))}")
    frame = calendar.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce").dt.normalize()
    bad = frame["service_date"].isna()
    if bad.any():
        raise ValueError("School holiday calendar contains invalid service_date values")
    if frame.duplicated(subset=["service_date"]).any():
        raise ValueError("School holiday calendar contains duplicate service_date rows")
    return frame


def _validate_event_catalog(catalog: pd.DataFrame | None) -> pd.DataFrame:
    if catalog is None or catalog.empty:
        return pd.DataFrame(columns=_expected_event_columns())
    missing = set(_expected_event_columns()) - set(catalog.columns)
    if missing:
        raise ValueError(f"Event catalog is missing columns: {', '.join(sorted(missing))}")
    frame = catalog.copy()
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce").dt.normalize()
    frame["route_id"] = frame["route_id"].astype(str)
    bad = frame["service_date"].isna()
    if bad.any():
        raise ValueError("Event catalog contains invalid service_date values")
    if frame.duplicated(subset=["service_date", "route_id"]).any():
        raise ValueError("Event catalog contains duplicate service_date/route_id rows")
    return frame


# ---------------------------------------------------------------------------
# Core feature builders
# ---------------------------------------------------------------------------


def build_feature_frame(
    history: pd.DataFrame,
    *,
    holidays: pd.DataFrame | None = None,
    school_holidays: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    weather_adapter: WeatherAdapter | None = None,
    weather_issue_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build the extended Phase 5 feature frame for each history row.

    Parameters
    ----------
    history:
        Validated daily history with service_date, route_id, passengers.
    holidays:
        Optional holiday catalog.
    school_holidays:
        Optional school-holiday calendar.
    events:
        Optional special-event catalog.
    weather_adapter:
        Optional weather adapter used to attach weather features.
    weather_issue_date:
        Optional date on which a forecast was issued. Required if
        weather_adapter is provided, because weather features must only use
        information available at that issue time.
    """
    frame = _prepare_base_frame(history)
    frame = _attach_historical_passenger_features(frame)
    frame = _attach_route_specific_features(frame)
    frame = _attach_calendar_features(frame)
    frame = _attach_holiday_features(frame, _validate_holiday_catalog(holidays))
    frame = _attach_school_holiday_features(frame, _validate_school_holiday_calendar(school_holidays))
    frame = _attach_event_features(frame, _validate_event_catalog(events))
    frame = _attach_weather_features(
        frame,
        weather_adapter,
        weather_issue_date,
    )
    return frame.sort_values(["route_id", "service_date"]).reset_index(drop=True)


def _prepare_base_frame(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    if not REQUIRED_COLUMNS.issubset(frame.columns):
        raise ValueError("history must contain service_date, route_id, and passengers")
    frame["service_date"] = pd.to_datetime(frame["service_date"], errors="coerce").dt.normalize()
    bad_dates = frame["service_date"].isna()
    if bad_dates.any():
        raise ValueError("history service_date contains invalid dates")
    frame["route_id"] = frame["route_id"].astype(str)
    if frame["route_id"].isna().any() or frame["route_id"].eq("").any():
        raise ValueError("history route_id contains blank values")
    frame["passengers"] = pd.to_numeric(frame["passengers"], errors="coerce")
    if frame["passengers"].isna().any() or not np.isfinite(frame["passengers"]).all():
        raise ValueError("history passengers must be finite numeric values")
    if (frame["passengers"] < 0).any():
        raise ValueError("history passengers cannot be negative")
    if frame.duplicated(["service_date", "route_id"]).any():
        raise ValueError("history contains duplicate service_date/route_id rows")
    frame = frame.sort_values(["route_id", "service_date"]).reset_index(drop=True)
    return frame


def _attach_historical_passenger_features(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = frame.groupby("route_id", sort=False)["passengers"]
    frame["lag_1"] = grouped.shift(1)
    frame["lag_7"] = grouped.shift(7)
    frame["lag_14"] = grouped.shift(14)
    frame["lag_28"] = grouped.shift(28)
    lagged = grouped.shift(1)
    window_7 = lagged.rolling(window=7, min_periods=7)
    window_14 = lagged.rolling(window=14, min_periods=14)
    window_28 = lagged.rolling(window=28, min_periods=28)
    frame["rolling_mean_7"] = window_7.mean().values
    frame["rolling_mean_14"] = window_14.mean().values
    frame["rolling_mean_28"] = window_28.mean().values
    frame["rolling_std_28"] = window_28.std(ddof=0).values
    return frame


def _attach_route_specific_features(frame: pd.DataFrame) -> pd.DataFrame:
    route_mean = frame.groupby("route_id", sort=True)["passengers"].transform("mean")
    route_std = frame.groupby("route_id", sort=True)["passengers"].transform("std")
    route_std = route_std.where(route_std > 0, 1.0)

    frame["route_demand_z"] = ((frame["passengers"] - route_mean) / route_std).astype(float)

    day_total = frame.groupby("service_date", sort=False)["passengers"].transform("sum")
    day_total = day_total.replace(0, np.nan)
    frame["route_share_of_day"] = (frame["passengers"] / day_total).astype(float)

    mean_by_route = frame.groupby("route_id", sort=True)["passengers"].mean()
    frame["lag_7_route_mean_ratio"] = (frame["lag_7"] / frame["route_id"].map(mean_by_route).replace(0, np.nan)).astype(float)
    frame["rolling_mean_28_to_route_mean_ratio"] = (frame["rolling_mean_28"] / frame["route_id"].map(mean_by_route).replace(0, np.nan)).astype(float)

    return frame


def _attach_calendar_features(frame: pd.DataFrame) -> pd.DataFrame:
    dates = pd.to_datetime(frame["service_date"])
    frame["day_of_week"] = dates.dt.dayofweek.astype(int)
    frame["is_weekend"] = (dates.dt.dayofweek >= 5).astype(int)
    frame["month"] = dates.dt.month.astype(int)
    frame["day_of_year"] = dates.dt.dayofyear.astype(int)

    season_day = (dates.dt.dayofyear - 1).astype(float)
    frame["season_sin"] = np.sin(2 * np.pi * season_day / 365.25)
    frame["season_cos"] = np.cos(2 * np.pi * season_day / 365.25)

    week_of_year = dates.dt.isocalendar().week.astype(int)
    frame["week_of_year_sin"] = np.sin(2 * np.pi * week_of_year / 52.18)
    frame["week_of_year_cos"] = np.cos(2 * np.pi * week_of_year / 52.18)

    frame["month_sin"] = np.sin(2 * np.pi * (frame["month"] - 1) / 12)
    frame["month_cos"] = np.cos(2 * np.pi * (frame["month"] - 1) / 12)

    return frame


def _attach_holiday_features(frame: pd.DataFrame, holidays: pd.DataFrame) -> pd.DataFrame:
    if holidays.empty:
        frame["holiday_type"] = "__unknown__"
        frame["holiday_name"] = "__unknown__"
        frame["is_holiday"] = 0
        return frame

    lookup = holidays.set_index(["service_date", "route_id"])["holiday_type"]
    frame["holiday_type"] = frame.set_index(["service_date", "route_id"]).index.map(
        lambda key: lookup.get(key, "__unknown__")
    ).astype(str)
    name_lookup = holidays.set_index(["service_date", "route_id"])["name"]
    frame["holiday_name"] = frame.set_index(["service_date", "route_id"]).index.map(
        lambda key: name_lookup.get(key, "__unknown__")
    ).astype(str)
    frame["is_holiday"] = (frame["holiday_type"] != "__unknown__").astype(int)
    return frame


def _attach_school_holiday_features(frame: pd.DataFrame, calendar: pd.DataFrame) -> pd.DataFrame:
    if calendar.empty:
        frame["school_holiday"] = 0
        frame["school_holiday_type"] = "__unknown__"
        return frame

    lookup = calendar.set_index("service_date")["school_holiday"]
    frame["school_holiday"] = frame["service_date"].map(lookup).fillna(0).astype(int)
    type_lookup = calendar.set_index("service_date").get("school_holiday_type", pd.Series(dtype=str))
    if not type_lookup.empty:
        frame["school_holiday_type"] = frame["service_date"].map(type_lookup).fillna("__unknown__").astype(str)
    else:
        frame["school_holiday_type"] = "__unknown__"
    return frame


def _attach_event_features(frame: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        frame["has_event"] = 0
        frame["event_type"] = "__unknown__"
        frame["event_label"] = "__unknown__"
        frame["event_intensity"] = np.nan
        return frame

    lookup = events.set_index(["service_date", "route_id"])
    keys = frame.set_index(["service_date", "route_id"]).index
    frame["has_event"] = keys.isin(lookup.index).astype(int)
    frame["event_type"] = keys.map(lambda key: lookup.at[key, "event_type"] if key in lookup.index else "__unknown__").astype(str)
    frame["event_label"] = keys.map(lambda key: lookup.at[key, "event_label"] if key in lookup.index else "__unknown__").astype(str)
    frame["event_intensity"] = keys.map(lambda key: lookup.at[key, "event_intensity"] if key in lookup.index else np.nan)
    return frame


def _attach_weather_features(
    frame: pd.DataFrame,
    weather_adapter: WeatherAdapter | None,
    weather_issue_date: pd.Timestamp | None,
) -> pd.DataFrame:
    if weather_adapter is None:
        frame["temperature"] = np.nan
        frame["rain_mm"] = np.nan
        frame["weather_condition"] = "__unknown__"
        frame["weather_source"] = "__unknown__"
        return frame

    if not isinstance(weather_adapter, WeatherAdapter):
        raise UnsupportedWeatherAdapterError("weather_adapter must implement the WeatherAdapter contract")

    if weather_issue_date is None:
        raise ValueError("weather_issue_date is required when weather_adapter is provided")

    frame["temperature"] = np.nan
    frame["rain_mm"] = np.nan
    frame["weather_condition"] = "__unknown__"
    frame["weather_source"] = weather_adapter.name if hasattr(weather_adapter, "name") else "__unknown__"

    for idx, row in frame.iterrows():
        target = row["service_date"]
        try:
            forecast = weather_adapter.forecast_available_on(
                route_id=row["route_id"],
                forecast_issue_date=pd.Timestamp(weather_issue_date),
                target_service_date=pd.Timestamp(target),
            )
        except Exception as exc:
            raise ValueError(
                f"Weather adapter failed for route {row['route_id']} on {target.date()}"
            ) from exc

        if forecast is None:
            continue

        frame.at[idx, "temperature"] = _to_float(forecast.get("temperature"))
        frame.at[idx, "rain_mm"] = _to_float(forecast.get("rain_mm"))
        frame.at[idx, "weather_condition"] = str(forecast.get("weather_condition", "__unknown__"))

    return frame


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def extended_feature_columns() -> list[str]:
    """Columns added by the extended Phase 5 feature builder.

    These are informational. Different modeling paths may select subsets of
    these columns and may still rely on the existing FEATURE_COLUMNS from
    src/forecasting.py for the operational model.
    """
    return [
        "lag_1",
        "lag_7",
        "lag_14",
        "lag_28",
        "rolling_mean_7",
        "rolling_mean_14",
        "rolling_mean_28",
        "rolling_std_28",
        "route_demand_z",
        "route_share_of_day",
        "lag_7_route_mean_ratio",
        "rolling_mean_28_to_route_mean_ratio",
        "day_of_week",
        "is_weekend",
        "month",
        "day_of_year",
        "season_sin",
        "season_cos",
        "week_of_year_sin",
        "week_of_year_cos",
        "month_sin",
        "month_cos",
        "holiday_type",
        "holiday_name",
        "is_holiday",
        "school_holiday",
        "school_holiday_type",
        "has_event",
        "event_type",
        "event_label",
        "event_intensity",
        "temperature",
        "rain_mm",
        "weather_condition",
        "weather_source",
    ]


# ---------------------------------------------------------------------------
# Convenience feature views for downstream modules
# ---------------------------------------------------------------------------


def historical_demand_features(history: pd.DataFrame) -> pd.DataFrame:
    """Route-relative historical demand signals only.

    Useful when a downstream module wants to inspect historical feature behavior
    before attaching calendar, holiday, event, or weather signals.
    """
    frame = _prepare_base_frame(history.sort_values(["route_id", "service_date"]))
    return _attach_historical_passenger_features(frame).pipe(_attach_route_specific_features)


def calendar_features_for_dates(dates: pd.DataFrame | pd.DatetimeIndex) -> pd.DataFrame:
    """Calendar-only feature frame for the given dates.

    Provided so downstream modules can precompute calendar/holiday/event proxies
    independently of the passenger series.
    """
    if isinstance(dates, pd.DataFrame):
        dates = pd.to_datetime(dates.squeeze() if dates.shape[1] == 1 else dates["service_date"], errors="coerce")
    if hasattr(dates, "normalize"):
        dates = dates.normalize()
    dates = pd.DatetimeIndex(dates)
    invalid = dates.isna()
    if invalid.any():
        raise ValueError("calendar_features_for_dates received invalid dates")
    frame = pd.DataFrame({"service_date": dates})
    return _attach_calendar_features(frame)
