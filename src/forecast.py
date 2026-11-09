"""Generate recursive multi-day forecasts with approximate uncertainty bands."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import pandas as pd

from .forecasting import complete_daily_series, load_data
from .modeling import recursive_forecast


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="CSV with service_date, route_id, passengers")
    parser.add_argument("--model", required=True, help="Model bundle produced by src.train")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", default="outputs/forecast.csv")
    parser.add_argument("--missing-days", choices=["error", "zero"], default="error")
    args = parser.parse_args()
    if args.days < 1 or args.days > 90:
        parser.error("--days must be between 1 and 90")
    bundle = joblib.load(args.model)
    if args.days > bundle["horizon"]:
        parser.error(f"This model was backtested for {bundle['horizon']} days; retrain with a longer --horizon before forecasting farther ahead")
    history = complete_daily_series(load_data(args.data), missing_days=args.missing_days)
    last_date = history["service_date"].max()
    trained_through = pd.Timestamp(bundle["trained_through"])
    if last_date < trained_through:
        raise ValueError(f"Input history ends on {last_date.date()}, earlier than model training cutoff {trained_through.date()}")
    routes = set(history["route_id"].astype(str).unique())
    trained_routes = set(bundle["routes"])
    unknown = routes - trained_routes
    if unknown:
        raise ValueError(f"Input includes routes absent during training: {', '.join(sorted(unknown))}. Train with sufficient history for them first.")
    forecast = recursive_forecast(bundle["model"], history, args.days)
    forecast["service_date"] = pd.to_datetime(forecast["service_date"])
    bands = bundle["route_error_band_90"]
    forecast["error_band_90"] = forecast["route_id"].map(bands).fillna(bundle["global_error_band_90"])
    forecast["lower_90_approx"] = (forecast["predicted_passengers"] - forecast["error_band_90"]).clip(lower=0).round(2)
    forecast["upper_90_approx"] = (forecast["predicted_passengers"] + forecast["error_band_90"]).round(2)
    forecast["predicted_passengers"] = forecast["predicted_passengers"].round(2)
    forecast["service_date"] = forecast["service_date"].dt.strftime("%Y-%m-%d")
    forecast = forecast.drop(columns="error_band_90")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    forecast.to_csv(output, index=False)
    print(f"Saved {len(forecast)} route-date forecasts: {output}")
    print("Uncertainty bands are approximate historical residual bands, not guaranteed prediction intervals.")


if __name__ == "__main__":
    main()
