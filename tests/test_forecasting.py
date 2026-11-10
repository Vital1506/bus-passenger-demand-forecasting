"""Core data-contract and time-series regression checks."""

import unittest

import pandas as pd

from src.forecasting import add_features, complete_daily_series, load_data
from src.modeling import seasonal_naive_forecast
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
        row = features.loc[features["service_date"].eq(self.start + pd.Timedelta(days=29))].iloc[0]
        self.assertEqual(row["lag_1"], 128)
        self.assertEqual(row["lag_7"], 122)

    def test_seasonal_naive_recurses_beyond_one_week(self):
        baseline = seasonal_naive_forecast(self.history, 10)
        first = baseline.loc[baseline["service_date"].eq(self.start + pd.Timedelta(days=40)), "baseline"].iloc[0]
        eighth = baseline.loc[baseline["service_date"].eq(self.start + pd.Timedelta(days=47)), "baseline"].iloc[0]
        self.assertEqual(first, eighth)


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
