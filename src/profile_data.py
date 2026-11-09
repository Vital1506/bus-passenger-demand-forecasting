"""Run strict schema and coverage checks before model training."""

from __future__ import annotations

import argparse

from .forecasting import complete_daily_series, load_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--missing-days", choices=["error", "zero"], default="error")
    args = parser.parse_args()
    raw = load_data(args.data)
    dense = complete_daily_series(raw, missing_days=args.missing_days)
    print("Data checks passed")
    print(f"Rows: {len(raw):,} | routes: {raw['route_id'].nunique()} | dates: {raw['service_date'].min().date()} to {raw['service_date'].max().date()}")
    print(f"Boardings: min={raw['passengers'].min():,.0f} median={raw['passengers'].median():,.0f} max={raw['passengers'].max():,.0f}")
    print(f"Route-day panel: {len(dense):,} rows after coverage policy '{args.missing_days}'")
    print("Review service calendars and canceled days before treating absent rows as zero demand.")


if __name__ == "__main__":
    main()
