"""Unit tests for Open-Meteo WeatherAdapter and leakage prevention."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.weather_adapter import WeatherAdapter, DailyWeatherRecord


def test_weather_adapter_disabled_returns_nan_never_magic_numbers(tmp_path):
    adapter = WeatherAdapter(cache_dir=tmp_path, enabled=False)
    df = pd.DataFrame({
        "service_date": ["2026-11-01", "2026-11-02"],
        "route_id": ["R10", "R10"],
        "passengers": [500, 520],
    })
    res = adapter.attach_weather_features(df)
    assert np.isnan(res["temperature_celsius"].iloc[0])
    assert np.isnan(res["precipitation_mm"].iloc[0])
    assert res["weather_source"].iloc[0] == "missing"
    # Verify explicitly that neither 15.0 nor 0.0 was inserted
    assert res["temperature_celsius"].iloc[0] != 15.0


def test_weather_adapter_zero_leakage_cutoff(tmp_path, monkeypatch):
    """Test that future dates use forecast API and past dates use historical archive."""
    adapter = WeatherAdapter(cache_dir=tmp_path, enabled=True)
    calls = []

    def fake_fetch_weather_range(start_date, end_date, agency_id="default", is_forecast=False):
        calls.append({"start": start_date, "end": end_date, "is_forecast": is_forecast})
        result = {}
        for d in pd.date_range(start_date, end_date):
            d_str = d.strftime("%Y-%m-%d")
            result[d_str] = DailyWeatherRecord(
                service_date=d_str,
                temperature_celsius=10.0 if not is_forecast else 8.0,
                precipitation_mm=0.0 if not is_forecast else 2.5,
                weather_source="open_meteo_forecast" if is_forecast else "open_meteo_historical",
            )
        return result

    monkeypatch.setattr(adapter, "fetch_weather_range", fake_fetch_weather_range)

    df = pd.DataFrame({
        "service_date": ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"],
        "route_id": ["R1", "R1", "R1", "R1"],
        "passengers": [100, 100, 100, 100],
    })

    # Cutoff at 2026-11-02: Nov 1-2 must be historical, Nov 3-4 must be forecast
    res = adapter.attach_weather_features(df, cutoff_date="2026-11-02")
    assert len(calls) == 2
    assert calls[0]["is_forecast"] is False  # History
    assert calls[0]["end"] == "2026-11-02"
    assert calls[1]["is_forecast"] is True   # Forecast
    assert calls[1]["start"] == "2026-11-03"

    assert res["weather_source"].iloc[0] == "open_meteo_historical"
    assert res["weather_source"].iloc[1] == "open_meteo_historical"
    assert res["weather_source"].iloc[2] == "open_meteo_forecast"
    assert res["weather_source"].iloc[3] == "open_meteo_forecast"


def test_weather_cache_persistence(tmp_path):
    adapter1 = WeatherAdapter(cache_dir=tmp_path, enabled=True)
    # Manually inject cached record
    adapter1._cache["CTA:2026-11-10"] = {
        "temperature_celsius": 11.5,
        "precipitation_mm": 1.2,
        "weather_source": "cached",
    }
    adapter1._save_cache()

    adapter2 = WeatherAdapter(cache_dir=tmp_path, enabled=True)
    rec = adapter2.fetch_weather_range("2026-11-10", "2026-11-10", agency_id="CTA")
    assert "2026-11-10" in rec
    assert rec["2026-11-10"].temperature_celsius == 11.5
    assert rec["2026-11-10"].precipitation_mm == 1.2
