"""Core data-contract and time-series regression checks."""

import unittest

import pandas as pd

from src.forecasting import add_features, complete_daily_series, load_data
from src.modeling import seasonal_naive_forecast


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


if __name__ == "__main__":
    unittest.main()
