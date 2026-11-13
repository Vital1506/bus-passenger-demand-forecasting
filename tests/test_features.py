"""Feature engineering unit tests for the Phase 5 feature layer.

These tests are standalone and deterministic. They verify hand-verifiable
historical, route-specific, and calendar features, and they verify that the
documented holiday, school-holiday, event, and weather stubs are present and
leakage-safe by construction.
"""

import unittest

import numpy as np
import pandas as pd

from src.features import (
    WeatherAdapter,
    _attach_historical_passenger_features,
    _attach_route_specific_features,
    _attach_calendar_features,
    _validate_catalog,
    build_feature_frame,
    calendar_features_for_dates,
    extended_feature_columns,
    historical_demand_features,
)
from src.forecasting import complete_daily_series


class TestHistoricalDemandFeatures(unittest.TestCase):
    def setUp(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(35):
            rows.append({
                "service_date": start + pd.Timedelta(days=day),
                "route_id": "R1",
                "passengers": float(100 + day),
            })
        self.frame = pd.DataFrame(rows).sort_values(["route_id", "service_date"]).reset_index(drop=True)

    def test_lag_1_is_previous_day_same_route(self):
        features = _attach_historical_passenger_features(self.frame)
        self.assertTrue(pd.isna(features.loc[0, "lag_1"]))
        self.assertEqual(features.loc[1, "lag_1"], 100.0)
        self.assertEqual(features.loc[34, "lag_1"], 133.0)

    def test_roll_7_uses_only_prior_observations(self):
        features = _attach_historical_passenger_features(self.frame)
        after_six = features.loc[7]
        expected = float(np.mean([100, 101, 102, 103, 104, 105, 106]))
        self.assertAlmostEqual(after_six["rolling_mean_7"], expected, places=6)
        self.assertTrue(pd.isna(features.loc[6, "rolling_mean_7"]))

    def test_roll_28_uses_only_prior_observations(self):
        features = _attach_historical_passenger_features(self.frame)
        after_twenty_seven = features.loc[28]
        expected = float(np.mean(range(100, 128)))
        self.assertAlmostEqual(after_twenty_seven["rolling_mean_28"], expected, places=6)
        self.assertTrue(pd.isna(features.loc[27, "rolling_mean_28"]))

    def test_roll_28_std_is_prior_only(self):
        features = _attach_historical_passenger_features(self.frame)
        self.assertTrue(pd.isna(features.loc[27, "rolling_std_28"]))
        self.assertFalse(pd.isna(features.loc[28, "rolling_std_28"]))


class TestRouteSpecificFeatures(unittest.TestCase):
    def setUp(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for route, base in [("R1", 100), ("R2", 200)]:
            for day in range(35):
                rows.append({
                    "service_date": start + pd.Timedelta(days=day),
                    "route_id": route,
                    "passengers": float(base + day),
                })
        self.frame = pd.DataFrame(rows).sort_values(["route_id", "service_date"]).reset_index(drop=True)

    def test_route_demand_z_is_standardized_within_route(self):
        features = historical_demand_features(self.frame)
        r1 = features[features["route_id"] == "R1"]
        r2 = features[features["route_id"] == "R2"]
        self.assertAlmostEqual(r1["route_demand_z"].mean(), 0.0, places=6)
        self.assertAlmostEqual(r2["route_demand_z"].mean(), 0.0, places=6)

    def test_route_share_of_day_is_route_over_day_total(self):
        features = historical_demand_features(self.frame)
        first_day = features[features["service_date"] == self.frame["service_date"].min()]
        day_total = first_day["passengers"].sum()
        for row in first_day.itertuples():
            expected = row.passengers / day_total
            self.assertAlmostEqual(row.route_share_of_day, expected, places=9)

    def test_route_share_does_not_leak_future(self):
        features = historical_demand_features(self.frame)
        latest_date = self.frame["service_date"].max()
        latest_rows = features[features["service_date"] == latest_date]
        day_total_latest = self.frame[self.frame["service_date"] == latest_date]["passengers"].sum()
        for row in latest_rows.itertuples():
            expected = row.passengers / day_total_latest
            self.assertAlmostEqual(row.route_share_of_day, expected, places=9)


class TestCalendarFourierFeatures(unittest.TestCase):
    def test_day_of_week_matches_dates(self):
        frame = calendar_features_for_dates(pd.date_range("2025-01-06", periods=7, freq="D"))
        self.assertEqual(frame.loc[0, "day_of_week"], 0)
        self.assertEqual(frame.loc[6, "day_of_week"], 6)

    def test_is_weekend_is_correct(self):
        frame = calendar_features_for_dates(pd.DataFrame({"service_date": pd.date_range("2025-01-04", periods=3, freq="D")}))
        self.assertEqual(frame.loc[0, "is_weekend"], 1)
        self.assertEqual(frame.loc[2, "is_weekend"], 0)

    def test_seasonal_encoding_cycles_smoothly(self):
        frame = calendar_features_for_dates(pd.date_range("2025-01-01", periods=2, freq="D"))
        self.assertAlmostEqual(frame.loc[0, "season_sin"], 0.0, places=6)
        self.assertAlmostEqual(frame.loc[0, "season_cos"], 1.0, places=6)

    def test_month_encoding_cycles_across_year(self):
        frame = calendar_features_for_dates(pd.date_range("2025-01-01", periods=12, freq="MS"))
        self.assertAlmostEqual(frame.loc[0, "month_sin"], 0.0, places=6)
        self.assertAlmostEqual(frame.loc[0, "month_cos"], 1.0, places=6)
        self.assertAlmostEqual(frame.loc[6, "month_sin"], 0.0, places=6)


class TestBuildFeatureFrameBasics(unittest.TestCase):
    def setUp(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for route, base in [("R1", 100), ("R2", 200)]:
            for day in range(40):
                rows.append({
                    "service_date": start + pd.Timedelta(days=day),
                    "route_id": route,
                    "passengers": float(base + day),
                })
        self.raw = pd.DataFrame(rows).sort_values(["route_id", "service_date"]).reset_index(drop=True)
        self.history = complete_daily_series(self.raw)

    def test_extended_columns_are_present_when_extras_empty(self):
        frame = build_feature_frame(self.history)
        wanted = [
            "lag_1", "lag_7", "lag_14", "lag_28",
            "rolling_mean_7", "rolling_mean_14", "rolling_mean_28", "rolling_std_28",
            "route_demand_z", "route_share_of_day",
            "day_of_week", "is_weekend", "month", "season_sin", "season_cos",
            "holiday_type", "is_holiday",
            "school_holiday", "has_event",
            "temperature", "weather_condition",
        ]
        for col in wanted:
            self.assertIn(col, frame.columns)

    def test_unknown_categoricals_use_sentinel(self):
        frame = build_feature_frame(self.history)
        self.assertEqual(frame["holiday_type"].iloc[0], "__unknown__")
        self.assertEqual(frame["weather_condition"].iloc[0], "__unknown__")

    def test_windowed_passenger_features_are_leakage_safe(self):
        frame = build_feature_frame(self.history)
        latest = frame["service_date"].max()
        latest_row = frame[frame["service_date"] == latest].iloc[0]
        self.assertTrue(pd.isna(latest_row["lag_1"]) or latest_row["lag_1"] < latest_row["passengers"])


class TestHolidayContract(unittest.TestCase):
    def test_holiday_catalog_requires_expected_columns(self):
        bad = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=3)})
        with self.assertRaisesRegex(ValueError, "missing columns"):
            _validate_catalog(bad, columns=["service_date", "route_id", "holiday_type", "name"], route_column="route_id")

    def test_holiday_catalog_accepts_valid_rows(self):
        catalog = pd.DataFrame([
            {"service_date": pd.Timestamp("2025-01-01"), "route_id": "R1", "holiday_type": "public", "name": "New Year"},
            {"service_date": pd.Timestamp("2025-07-04"), "route_id": "R1", "holiday_type": "public", "name": "Independence Day"},
        ])
        validated = _validate_catalog(
            catalog,
            columns=["service_date", "route_id", "holiday_type", "name"],
            route_column="route_id",
        )
        self.assertIn("service_date", validated.columns)
        self.assertIn("holiday_type", validated.columns)

    def test_holiday_flag_attached_only_when_present(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(14):
            rows.append({"service_date": start + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)})
        history = complete_daily_series(pd.DataFrame(rows))
        holidays = pd.DataFrame([{
            "service_date": pd.Timestamp("2025-01-01"),
            "route_id": "R1",
            "holiday_type": "public",
            "name": "New Year",
        }])
        frame = build_feature_frame(history, holidays=holidays)
        self.assertEqual(frame[frame["service_date"] == pd.Timestamp("2025-01-01")]["is_holiday"].iloc[0], 1)
        self.assertEqual(frame[frame["service_date"] == pd.Timestamp("2025-01-02")]["is_holiday"].iloc[0], 0)
        self.assertEqual(frame["holiday_type"].iloc[1], "__unknown__")


class TestSchoolHolidayContract(unittest.TestCase):
    def test_school_holiday_calendar_requires_expected_columns(self):
        bad = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=3)})
        with self.assertRaisesRegex(ValueError, "missing columns"):
            _validate_catalog(bad, columns=["service_date", "school_holiday"], route_column=None)

    def test_school_holiday_flag_attached_only_when_present(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(14):
            rows.append({"service_date": start + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)})
        history = complete_daily_series(pd.DataFrame(rows))
        school = pd.DataFrame([
            {"service_date": pd.Timestamp("2025-01-01"), "school_holiday": 1},
            {"service_date": pd.Timestamp("2025-01-02"), "school_holiday": 1},
        ])
        frame = build_feature_frame(history, school_holidays=school)
        self.assertEqual(frame[frame["service_date"] == pd.Timestamp("2025-01-01")]["school_holiday"].iloc[0], 1)
        self.assertEqual(frame[frame["service_date"] == pd.Timestamp("2025-01-03")]["school_holiday"].iloc[0], 0)


class TestEventContract(unittest.TestCase):
    def test_event_catalog_requires_expected_columns(self):
        bad = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=3)})
        with self.assertRaisesRegex(ValueError, "missing columns"):
            _validate_catalog(bad, columns=["service_date", "route_id", "event_type", "event_label", "event_intensity"], route_column="route_id")

    def test_event_flag_attached_only_when_present(self):
        rows = [{"service_date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)} for day in range(14)]
        history = complete_daily_series(pd.DataFrame(rows))
        events = pd.DataFrame([{"service_date": pd.Timestamp("2025-01-02"), "route_id": "R1", "event_type": "festival", "event_label": "Downtown festival", "event_intensity": 0.8}])
        frame = build_feature_frame(history, events=events)
        self.assertEqual(frame.loc[frame["service_date"] == pd.Timestamp("2025-01-02"), "has_event"].iloc[0], 1)
        self.assertEqual(frame.loc[frame["service_date"] == pd.Timestamp("2025-01-03"), "has_event"].iloc[0], 0)


class TestWeatherContract(unittest.TestCase):
    def test_weather_adapter_is_an_abstract_contract(self):
        adapter = WeatherAdapter()
        with self.assertRaises(NotImplementedError):
            adapter.historical_observations(route_id="R1", start_date=pd.Timestamp("2025-01-01"), end_date=pd.Timestamp("2025-01-02"))
        with self.assertRaises(NotImplementedError):
            adapter.forecast_available_on(route_id="R1", forecast_issue_date=pd.Timestamp("2025-01-01"), target_service_date=pd.Timestamp("2025-01-02"))
        with self.assertRaises(NotImplementedError):
            adapter.candidate_forecast_sources()

    def test_weather_is_empty_when_no_adapter(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(14):
            rows.append({"service_date": start + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)})
        history = complete_daily_series(pd.DataFrame(rows))
        frame = build_feature_frame(history)
        self.assertTrue(frame["temperature"].isna().all())
        self.assertTrue(frame["weather_condition"].eq("__unknown__").all())

    def test_weather_requires_issue_date_when_adapter_provided(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(14):
            rows.append({"service_date": start + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)})
        history = complete_daily_series(pd.DataFrame(rows))
        class StubWeatherAdapter(WeatherAdapter):
            pass
        with self.assertRaisesRegex(ValueError, "weather_issue_date is required"):
            build_feature_frame(history, weather_adapter=StubWeatherAdapter(), weather_issue_date=None)

    def test_weather_fails_when_adapter_missing_method(self):
        rows = []
        start = pd.Timestamp("2025-01-01")
        for day in range(14):
            rows.append({"service_date": start + pd.Timedelta(days=day), "route_id": "R1", "passengers": float(100 + day)})
        history = complete_daily_series(pd.DataFrame(rows))

        class BrokenWeatherAdapter(WeatherAdapter):
            def historical_observations(self, *, route_id, start_date, end_date):  # type: ignore[override]
                return pd.DataFrame()

            def forecast_available_on(self, *, route_id, forecast_issue_date, target_service_date):  # type: ignore[override]
                return None

            def candidate_forecast_sources(self):  # type: ignore[override]
                return []

        frame = build_feature_frame(
            history,
            weather_adapter=BrokenWeatherAdapter(),
            weather_issue_date=pd.Timestamp("2025-01-01"),
        )
        self.assertTrue(frame["temperature"].isna().all())


class TestExtendedFeatureColumns(unittest.TestCase):
    def test_extended_feature_columns_is_explicit(self):
        cols = extended_feature_columns()
        self.assertIsInstance(cols, list)
        self.assertTrue(len(cols) > 20)
        for expected in ["lag_1", "rolling_mean_28", "route_demand_z", "season_sin", "is_holiday", "temperature"]:
            self.assertIn(expected, cols)


if __name__ == "__main__":
    unittest.main()
