"""Unit tests for calendar and holiday feature extraction."""

from __future__ import annotations

from datetime import date
import pandas as pd
import pytest

from src.calendar_features import is_transit_holiday, attach_calendar_features


def test_known_transit_holidays():
    # 2026 New Year
    is_hol, name = is_transit_holiday("2026-01-01")
    assert is_hol == 1
    assert "New Year" in name

    # 2026 Independence Day (July 4 is Saturday -> observed July 3)
    is_hol, name = is_transit_holiday("2026-07-04")
    assert is_hol == 1

    # 2026 Thanksgiving (Nov 26)
    is_hol, name = is_transit_holiday("2026-11-26")
    assert is_hol == 1
    assert "Thanksgiving" in name

    # 2026 Christmas (Dec 25)
    is_hol, name = is_transit_holiday("2026-12-25")
    assert is_hol == 1
    assert "Christmas" in name

    # Normal Tuesday
    is_hol, name = is_transit_holiday("2026-11-10")
    assert is_hol == 0
    assert name is None


def test_attach_calendar_features_leakage_safety():
    df = pd.DataFrame({
        "service_date": ["2026-11-25", "2026-11-26", "2026-11-27"],
        "route_id": ["R1", "R1", "R1"],
        "passengers": [500, 200, 450],
    })
    res = attach_calendar_features(df)
    assert res["is_holiday"].tolist() == [0, 1, 0]
    assert res["holiday_name"].iloc[1] == "Thanksgiving Day"
