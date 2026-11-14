"""Core data-contract and time-series regression checks."""

import unittest
from unittest.mock import patch

import pandas as pd

from src.forecast import build_forecast
from src.forecasting import add_features, complete_daily_series, load_data, wape
from src.train import select_forecast_strategy
from src.modeling import seasonal_naive_forecast, weekday_average_forecast
from src.schedule_import import schedules_to_synthetic_counts


class ForecastingContractTests(unittest.TestCase):
    def setUp(self):
        self.start = pd.Timestamp("2025-01-01")
        self.history = pd.DataFrame({
            "service_date": pd.date_range(self.start, periods=40, freq="D"),
            "route_id": ["R1"] * 40,
            "passengers": list(range(100, 140)),
        })

    def test_missing_dates_fail_closed_by_default(self):
        sparse = self.history.drop(index=10).reset_index(drop=True)
        with self.assertRaisesRegex(ValueError, "Missing route/date observations"):
            complete_daily_series(sparse)

    def test_missing_dates_can_be_filled_only_with_explicit_zero_policy(self):
        sparse = self.history.drop(index=10).reset_index(drop=True)
        dense = complete_daily_series(sparse, missing_days="zero")
        self.assertEqual(len(dense), 40)
        self.assertEqual(dense.loc[10, "passengers"], 0)

    def test_lag_feature_uses_only_previous_observations(self):
        features = add_features(self.history)
        row = features.loc[features["service_date"].eq(self.start + pd.to_timedelta(29, unit="D"))].iloc[0]
        self.assertEqual(row["lag_1"], 128)
        self.assertEqual(row["lag_7"], 122)

    def test_seasonal_naive_recurses_beyond_one_week(self):
        baseline = seasonal_naive_forecast(self.history, 10)
        first = baseline.loc[baseline["service_date"].eq(self.start + pd.to_timedelta(40, unit="D")), "baseline"].iloc[0]
        eighth = baseline.loc[baseline["service_date"].eq(self.start + pd.to_timedelta(47, unit="D")), "baseline"].iloc[0]
        self.assertEqual(first, eighth)

    def test_weekday_average_uses_matching_weekday_and_returns_range(self):
        history = self.history.iloc[:28].copy()
        forecast = weekday_average_forecast(history, 7)
        self.assertEqual(len(forecast), 7)
        monday = forecast.iloc[0]
        matching = history.loc[history.service_date.dt.dayofweek.eq(pd.Timestamp(monday.service_date).dayofweek), "passengers"].mean()
        self.assertEqual(monday.predicted_passengers, round(matching, 2))
        self.assertLessEqual(monday.lower_90_approx, monday.predicted_passengers)
        self.assertGreaterEqual(monday.upper_90_approx, monday.predicted_passengers)

    def test_weekday_average_requires_28_observations_per_route(self):
        with self.assertRaisesRegex(ValueError, "at least 28"):
            weekday_average_forecast(self.history.iloc[:27], 7)


    def test_wape_returns_zero_for_perfect_zero_demand_predictions(self):
        self.assertEqual(wape(pd.Series([0, 0]).to_numpy(), pd.Series([0, 0]).to_numpy()), 0.0)

    def test_wape_rejects_nonzero_predictions_when_actual_total_is_zero(self):
        with self.assertRaisesRegex(ValueError, "WAPE is undefined"):
            wape(pd.Series([0, 0]).to_numpy(), pd.Series([1, 0]).to_numpy())

    def test_forecast_strategy_promotes_ml_only_when_it_beats_baseline(self):
        self.assertEqual(select_forecast_strategy({"wape": 0.10}, {"wape": 0.12}), "machine_learning")
        self.assertEqual(select_forecast_strategy({"wape": 0.12}, {"wape": 0.12}), "seasonal_naive")
        self.assertEqual(select_forecast_strategy({"wape": 0.14}, {"wape": 0.12}), "seasonal_naive")

    def test_build_forecast_uses_selected_seasonal_naive_strategy(self):
        data_path = "data/sample_daily_ridership.csv"
        history = complete_daily_series(load_data(data_path))
        bundle = {
            "horizon": 7,
            "trained_through": history["service_date"].max().date().isoformat(),
            "routes": sorted(history["route_id"].astype(str).unique()),
            "forecast_strategy": "seasonal_naive",
            "route_error_band_90": {},
            "global_error_band_90": 25.0,
            "model": None,
        }
        with patch("src.forecast.joblib.load", return_value=bundle):
            forecast = build_forecast(data_path, "unused-model.joblib", days=7)

        expected = seasonal_naive_forecast(history, 7)
        pd.testing.assert_series_equal(
            forecast["predicted_passengers"],
            expected["baseline"].round(2),
            check_names=False,
        )
        self.assertTrue((forecast["upper_90_approx"] >= forecast["predicted_passengers"]).all())

    def test_timetable_import_creates_a_labelled_synthetic_route_panel(self):
        timetable = b"From,To,Operator,Departure\nA,B,Transit Co,08:00\nA,B,Transit Co,09:00\nB,C,Transit Co,10:00\n"
        result = schedules_to_synthetic_counts(timetable, days=60)
        self.assertEqual(result["route_id"].nunique(), 2)
        self.assertEqual(len(result), 120)
        self.assertEqual(set(result.columns), {"service_date", "route_id", "passengers"})
        self.assertTrue((result["passengers"] >= 0).all())

    def test_timetable_import_requires_origin_and_destination_columns(self):
        with self.assertRaisesRegex(ValueError, "From and To"):
            schedules_to_synthetic_counts(b"Operator,Distance\nTransit Co,50\n", days=60)


if __name__ == "__main__":
    unittest.main()
