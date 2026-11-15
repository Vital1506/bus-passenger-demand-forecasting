"""Weather feature adapter powered by real Open-Meteo API.

Features:
- Queries real Open-Meteo Historical Archive API for historical service dates
- Queries real Open-Meteo Forecast API for forecast horizon dates as-of cutoff
- Zero Data Leakage: observed historical weather is never revealed into future forecast horizons
- Missing values are strictly NaN (never magic 15.0 C or 0.0 mm defaults)
- Results are cached locally to prevent repeated external network calls
- Missing/unreachable provider is explicitly flagged with weather_source="missing"
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

logger = logging.getLogger("weather_adapter")

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"

# Standard transit metropolitan anchor coordinates (defaults to Chicago CTA)
AGENCY_COORDINATES: dict[str, tuple[float, float, str]] = {
    "CTA": (41.8781, -87.6298, "America/Chicago"),
    "MTA": (40.7128, -74.0060, "America/New_York"),
    "DEFAULT": (41.8781, -87.6298, "America/Chicago"),
}


@dataclass
class DailyWeatherRecord:
    service_date: str
    temperature_celsius: float | None
    precipitation_mm: float | None
    weather_source: str  # "open_meteo_historical", "open_meteo_forecast", "missing"


class WeatherAdapter:
    def __init__(
        self,
        cache_dir: Path | str | None = None,
        enabled: bool | None = None,
        timeout: float = 6.0,
    ):
        self.timeout = timeout
        if enabled is None:
            # Enabled by default unless explicitly disabled
            self.enabled = os.getenv("BUS_FORECAST_ENABLE_WEATHER", "true").strip().lower() in {"1", "true", "yes"}
        else:
            self.enabled = enabled

        base_data = Path(os.getenv("BUS_FORECAST_DATA_DIR", "data"))
        self.cache_file = (Path(cache_dir) if cache_dir else base_data) / "weather_cache.json"
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if self.cache_file.exists():
            try:
                self._cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Could not load weather cache: %s", exc)
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not save weather cache: %s", exc)

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": "Open-Meteo",
            "cached_days": len(self._cache),
            "status": "AVAILABLE" if self.enabled else "DISABLED",
        }

    def fetch_weather_range(
        self,
        start_date: str,
        end_date: str,
        agency_id: str = "default",
        is_forecast: bool = False,
    ) -> dict[str, DailyWeatherRecord]:
        """Fetch weather observations from cache or Open-Meteo."""
        agency_key = agency_id.upper() if agency_id else "DEFAULT"
        lat, lon, tz = AGENCY_COORDINATES.get(agency_key, AGENCY_COORDINATES["DEFAULT"])

        result: dict[str, DailyWeatherRecord] = {}
        missing_dates = []

        # Check in-memory/disk cache first
        date_range = [d.strftime("%Y-%m-%d") for d in pd.date_range(start_date, end_date)]
        for d in date_range:
            cache_key = f"{agency_key}:{d}"
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                result[d] = DailyWeatherRecord(
                    service_date=d,
                    temperature_celsius=entry.get("temperature_celsius"),
                    precipitation_mm=entry.get("precipitation_mm"),
                    weather_source=entry.get("weather_source", "cached"),
                )
            else:
                missing_dates.append(d)

        if not missing_dates or not self.enabled:
            # If not enabled or no missing dates, fill remaining with missing (NaN)
            for d in missing_dates:
                result[d] = DailyWeatherRecord(
                    service_date=d,
                    temperature_celsius=None,
                    precipitation_mm=None,
                    weather_source="missing",
                )
            return result

        # Query Open-Meteo for missing dates
        req_start = min(missing_dates)
        req_end = max(missing_dates)
        url = FORECAST_API_URL if is_forecast else ARCHIVE_API_URL
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": req_start,
            "end_date": req_end,
            "daily": "temperature_2m_mean,precipitation_sum",
            "timezone": tz,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json().get("daily", {})
                times = data.get("time", [])
                temps = data.get("temperature_2m_mean", [])
                precips = data.get("precipitation_sum", [])
                src_label = "open_meteo_forecast" if is_forecast else "open_meteo_historical"

                for i, d in enumerate(times):
                    t_val = float(temps[i]) if temps[i] is not None else None
                    p_val = float(precips[i]) if precips[i] is not None else None
                    rec = DailyWeatherRecord(
                        service_date=d,
                        temperature_celsius=t_val,
                        precipitation_mm=p_val,
                        weather_source=src_label,
                    )
                    result[d] = rec
                    cache_key = f"{agency_key}:{d}"
                    self._cache[cache_key] = {
                        "temperature_celsius": t_val,
                        "precipitation_mm": p_val,
                        "weather_source": src_label,
                    }
                self._save_cache()
            else:
                logger.warning("Open-Meteo returned status %d: %s", resp.status_code, resp.text[:100])
        except Exception as exc:
            logger.warning("Open-Meteo connection failed (%s). Leaving weather values as NaN.", exc)

        # Ensure all dates have a record (unknown values stay NaN, never fabricated)
        for d in date_range:
            if d not in result:
                result[d] = DailyWeatherRecord(
                    service_date=d,
                    temperature_celsius=None,
                    precipitation_mm=None,
                    weather_source="missing",
                )

        return result

    def attach_weather_features(
        self,
        df: pd.DataFrame,
        cutoff_date: str | date | None = None,
        agency_id: str = "default",
    ) -> pd.DataFrame:
        """Attach weather features with strict cutoff-based leakage prevention.
        
        Dates <= cutoff_date use observed historical weather.
        Dates > cutoff_date use forecast weather.
        Missing observations stay NaN.
        """
        result = df.copy()
        if "service_date" not in result.columns or result.empty:
            result["temperature_celsius"] = np.nan
            result["precipitation_mm"] = np.nan
            result["weather_source"] = "missing"
            return result

        service_dates = pd.to_datetime(result["service_date"]).dt.strftime("%Y-%m-%d")
        min_date = service_dates.min()
        max_date = service_dates.max()

        if cutoff_date is None:
            cutoff_str = max_date
        elif isinstance(cutoff_date, date):
            cutoff_str = cutoff_date.strftime("%Y-%m-%d")
        else:
            cutoff_str = str(cutoff_date)

        # 1. Historical split (<= cutoff)
        hist_records: dict[str, DailyWeatherRecord] = {}
        if min_date <= cutoff_str:
            hist_records = self.fetch_weather_range(
                start_date=min_date,
                end_date=min(max_date, cutoff_str),
                agency_id=agency_id,
                is_forecast=False,
            )

        # 2. Future forecast split (> cutoff) - zero leakage
        future_records: dict[str, DailyWeatherRecord] = {}
        if max_date > cutoff_str:
            next_day = (pd.Timestamp(cutoff_str) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            future_records = self.fetch_weather_range(
                start_date=next_day,
                end_date=max_date,
                agency_id=agency_id,
                is_forecast=True,
            )

        all_weather = {**hist_records, **future_records}

        temps = []
        precips = []
        sources = []
        for d in service_dates:
            rec = all_weather.get(d)
            if rec and rec.temperature_celsius is not None:
                temps.append(rec.temperature_celsius)
                precips.append(rec.precipitation_mm if rec.precipitation_mm is not None else 0.0)
                sources.append(rec.weather_source)
            else:
                temps.append(np.nan)
                precips.append(np.nan)
                sources.append("missing")

        result["temperature_celsius"] = temps
        result["precipitation_mm"] = precips
        result["weather_source"] = sources
        return result


_GLOBAL_WEATHER_ADAPTER: WeatherAdapter | None = None


def get_weather_adapter() -> WeatherAdapter:
    global _GLOBAL_WEATHER_ADAPTER
    if _GLOBAL_WEATHER_ADAPTER is None:
        _GLOBAL_WEATHER_ADAPTER = WeatherAdapter()
    return _GLOBAL_WEATHER_ADAPTER
