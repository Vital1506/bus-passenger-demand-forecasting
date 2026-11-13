"""Create a clearly synthetic demo time series from a route timetable."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd


def schedules_to_synthetic_counts(contents: bytes, days: int = 730, max_routes: int = 12) -> pd.DataFrame:
    """Build an illustrative daily demand panel from From/To schedule rows.

    Schedule frequency is used only to set relative scenario scale. The output
    is fabricated and must never be described as observed passenger demand.
    """
    if days < 60 or max_routes < 1:
        raise ValueError("days must be at least 60 and max_routes must be positive")
    try:
        schedules = pd.read_csv(io.BytesIO(contents), encoding="utf-8-sig")
    except (UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise ValueError("The CSV could not be read. Save it as UTF-8 CSV and try again.") from exc
    columns = {str(column).strip().casefold(): column for column in schedules.columns}
    origin_col = next((columns[name] for name in ("from", "origin", "source") if name in columns), None)
    destination_col = next((columns[name] for name in ("to", "destination", "dest") if name in columns), None)
    if origin_col is None or destination_col is None:
        raise ValueError("CSV format not recognized. Passenger counts need service_date, route_id, passengers. A timetable needs From and To columns.")
    routes = schedules[[origin_col, destination_col]].copy()
    routes.columns = ["origin", "destination"]
    for column in ("origin", "destination"):
        routes[column] = routes[column].astype("string").str.strip()
    routes = routes.dropna().loc[lambda frame: frame["origin"].ne("") & frame["destination"].ne("")]
    if routes.empty:
        raise ValueError("No non-empty From/To route rows were found in the timetable.")

    route_counts = routes.groupby(["origin", "destination"], sort=True).size().sort_values(ascending=False).head(max_routes)
    end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.to_timedelta(1, unit="D")
    dates = pd.date_range(end=end, periods=days, freq="D")
    rng = np.random.default_rng(7351)
    rows = []
    for (origin, destination), schedule_count in route_counts.items():
        route_id = f"{origin} → {destination}"
        # The scale is a scenario parameter, not an estimate of actual boardings.
        base = min(900, max(120, 180 + 45 * np.log1p(int(schedule_count))))
        route_noise = rng.normal(1.0, 0.055, len(dates))
        for index, date in enumerate(dates):
            weekday = 1.0 if date.dayofweek < 5 else 0.62
            annual = 1.0 + 0.08 * np.sin(2 * np.pi * date.dayofyear / 365.25)
            passengers = max(0, round(base * weekday * annual * route_noise[index]))
            rows.append({"service_date": date.date().isoformat(), "route_id": route_id, "passengers": passengers})
    return pd.DataFrame(rows)
