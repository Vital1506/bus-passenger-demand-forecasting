"""Transit calendar, holiday, and event feature engineering with zero data leakage."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
import pandas as pd


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Find the nth occurrence of weekday (0=Monday..6=Sunday) in a given month."""
    first = date(year, month, 1)
    first_weekday = first.weekday()
    offset = (weekday - first_weekday) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Find the last occurrence of weekday in a given month."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    last_weekday = last.weekday()
    offset = (last_weekday - weekday) % 7
    return last - timedelta(days=offset)


def get_us_federal_holidays(year: int) -> dict[date, str]:
    """Return dictionary of observed US transit holidays for the given year."""
    holidays: dict[date, str] = {}

    def add_observed(actual_date: date, name: str) -> None:
        holidays[actual_date] = name
        # If Saturday, observed Friday; if Sunday, observed Monday
        if actual_date.weekday() == 5:
            holidays[actual_date - timedelta(days=1)] = f"{name} (Observed)"
        elif actual_date.weekday() == 6:
            holidays[actual_date + timedelta(days=1)] = f"{name} (Observed)"

    add_observed(date(year, 1, 1), "New Year's Day")
    add_observed(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add_observed(_nth_weekday(year, 2, 0, 3), "Presidents' Day")
    add_observed(_last_weekday(year, 5, 0), "Memorial Day")
    add_observed(date(year, 6, 19), "Juneteenth")
    add_observed(date(year, 7, 4), "Independence Day")
    add_observed(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add_observed(_nth_weekday(year, 10, 0, 2), "Columbus Day")
    add_observed(date(year, 11, 11), "Veterans Day")
    add_observed(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add_observed(date(year, 12, 25), "Christmas Day")

    return holidays


_HOLIDAY_CACHE: dict[int, dict[date, str]] = {}


def is_transit_holiday(d: date | str) -> tuple[int, str | None]:
    """Check if date is a recognized transit holiday. Zero leakage."""
    if isinstance(d, str):
        target_date = pd.to_datetime(d).date()
    elif isinstance(d, pd.Timestamp):
        target_date = d.date()
    else:
        target_date = d

    year = target_date.year
    if year not in _HOLIDAY_CACHE:
        _HOLIDAY_CACHE[year] = get_us_federal_holidays(year)

    year_holidays = _HOLIDAY_CACHE[year]
    if target_date in year_holidays:
        return 1, year_holidays[target_date]
    return 0, None


def attach_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    """Attach leakage-safe calendar and holiday features to a dataframe."""
    result = df.copy()
    if "service_date" not in result.columns or result.empty:
        result["is_holiday"] = 0
        result["holiday_name"] = None
        return result

    dates = pd.to_datetime(result["service_date"])
    is_hol = []
    names = []

    for d in dates:
        flag, name = is_transit_holiday(d.date())
        is_hol.append(flag)
        names.append(name)

    result["is_holiday"] = is_hol
    result["holiday_name"] = names
    return result
