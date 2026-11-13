"""Deterministic synthetic tests for the Phase 6 decision-support module."""

import datetime as dt
import unittest

import pandas as pd

from src import decisions


def make_observations(route: str, start: dt.date, days: int, base: float, slope: float = 0.0) -> pd.DataFrame:
    rows = []
    for offset in range(days):
        rows.append(
            {
                "service_date": start + dt.timedelta(days=offset),
                "route_id": route,
                "passengers": float(base + slope * offset),
            }
        )
    return pd.DataFrame(rows)


def make_forecast(route: str, start: dt.date, days: int, values: list[float]) -> pd.DataFrame:
    rows = []
    for offset in range(days):
        rows.append(
            {
                "service_date": start + dt.timedelta(days=offset),
                "route_id": route,
                "predicted_passengers": float(values[offset]),
            }
        )
    return pd.DataFrame(rows)


def capacity_config(route: str, **overrides) -> dict:
    config = {
        "seats_per_vehicle": 55,
        "round_trip_minutes": 90,
        "peak_headway_minutes": 12,
        "off_peak_headway_minutes": 20,
    }
    config.update(overrides)
    return {route: config}


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = dt.date(2025, 1, 1)
        self.observed = make_observations("R1", self.start, 30, 100.0)

    def test_high_predicted_demand_is_explained(self):
        forecast = make_forecast("R1", dt.date(2025, 1, 31), 7, [400.0] * 7)
        alerts = decisions.explain_alerts(self.observed, forecast, agency="AgencyA")
        high = [a for a in alerts if a["type"] == decisions.ALERT_HIGH_DEMAND]
        self.assertEqual(len(high), 1)
        alert = high[0]
        self.assertEqual(alert["agency"], "AgencyA")
        self.assertEqual(alert["route_or_trip"], "R1")
        self.assertEqual(alert["relevant_forecast"]["days_ahead"], 1)
        self.assertIn("90th percentile", alert["threshold"]["method"])
        self.assertTrue(alert["suggested_action"])
        self.assertFalse(alert["acknowledged"])

    def test_above_capacity_uses_configured_assumptions(self):
        # Weekday: 55 seats * (240/12 + 840/20) = 3410 seats per day.
        # 2025-02-03 is a Monday, so the weekday capacity applies.
        forecast = make_forecast(
            "R1", dt.date(2025, 1, 31), 7, [100.0, 100.0, 100.0, 5000.0, 100.0, 100.0, 100.0]
        )
        alerts = decisions.explain_alerts(
            self.observed, forecast, agency="AgencyA", capacity_config=capacity_config("R1")
        )
        above = [a for a in alerts if a["type"] == decisions.ALERT_ABOVE_CAPACITY]
        self.assertEqual(len(above), 1)
        self.assertAlmostEqual(above[0]["threshold"]["value"], 3410.0)
        self.assertEqual(above[0]["relevant_forecast"]["service_date"], "2025-02-03")

    def test_low_demand_is_flagged(self):
        observed = make_observations("R1", self.start, 30, 100.0, slope=2.0)
        forecast = make_forecast("R1", dt.date(2025, 1, 31), 7, [5.0] * 7)
        alerts = decisions.explain_alerts(observed, forecast, agency="AgencyA")
        low = [a for a in alerts if a["type"] == decisions.ALERT_LOW_DEMAND]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["data_confidence"], "low")
        self.assertGreater(low[0]["threshold"]["value"], 5.0)

    def test_sudden_variation_uses_recent_days_only(self):
        observed = self.observed.copy()
        observed.loc[observed.index[-3], "passengers"] = 400.0
        alerts = decisions.explain_alerts(observed, agency="AgencyA")
        variations = [a for a in alerts if a["type"] == decisions.ALERT_SUDDEN_VARIATION]
        self.assertEqual(len(variations), 1)
        self.assertIn("400", variations[0]["what"])

    def test_stable_history_raises_no_demand_alerts(self):
        forecast = make_forecast("R1", dt.date(2025, 1, 31), 7, [100.0] * 7)
        alerts = decisions.explain_alerts(self.observed, forecast, agency="AgencyA")
        self.assertEqual(alerts, [])

    def test_missing_records_report_gaps(self):
        observed = self.observed.drop(self.observed.index[10:13])
        alerts = decisions.explain_alerts(observed, agency="AgencyA")
        missing = [a for a in alerts if a["type"] == decisions.ALERT_MISSING_RECORDS]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["threshold"]["value"], 1)
        self.assertIn("3 calendar day(s)", missing[0]["what"])

    def test_stale_feed_and_model_are_reported(self):
        alerts = decisions.explain_alerts(
            self.observed,
            agency="AgencyA",
            feed_freshness={"staleness_seconds": 900},
            weather_freshness={"staleness_seconds": 60},
            model_freshness={"trained_through": dt.date(2024, 1, 1)},
        )
        types = {a["type"] for a in alerts}
        self.assertIn(decisions.ALERT_STALE_FEED, types)
        self.assertIn(decisions.ALERT_STALE_MODEL, types)

    def test_fresh_inputs_raise_no_staleness_alerts(self):
        alerts = decisions.explain_alerts(
            self.observed,
            agency="AgencyA",
            feed_freshness={"staleness_seconds": 10},
            model_freshness={"trained_through": dt.date.today()},
        )
        self.assertEqual(alerts, [])

    def test_forecast_error_increase_is_reported(self):
        rows = []
        for offset in range(14):
            rows.append(
                {
                    "service_date": dt.date(2025, 1, 1) + dt.timedelta(days=offset),
                    "predicted_passengers": 100.0,
                    "actual_passengers": 105.0 if offset < 7 else 150.0,
                }
            )
        forecast = pd.DataFrame(rows)
        alerts = decisions.explain_alerts(self.observed, forecast, agency="AgencyA", route_filter="R1")
        errors = [a for a in alerts if a["type"] == decisions.ALERT_FORECAST_ERROR]
        self.assertEqual(len(errors), 1)
        self.assertAlmostEqual(errors[0]["relevant_forecast"], None)
        self.assertIn("deteriorated", errors[0]["explanation"])

    def test_route_filter_limits_scope(self):
        observed = pd.concat(
            [self.observed, make_observations("R2", self.start, 30, 100.0)], ignore_index=True
        )
        forecast = make_forecast("R2", dt.date(2025, 1, 31), 7, [900.0] * 7)
        alerts = decisions.explain_alerts(observed, forecast, agency="AgencyA", route_filter="R1")
        self.assertTrue(all(a["route_or_trip"] in {"R1", "all"} for a in alerts))
        self.assertNotIn(decisions.ALERT_HIGH_DEMAND, {a["type"] for a in alerts})

    def test_empty_observations_return_no_alerts(self):
        self.assertEqual(decisions.explain_alerts(pd.DataFrame()), [])
        self.assertEqual(
            decisions.explain_alerts(pd.DataFrame(columns=["service_date", "route_id", "passengers"])),
            [],
        )


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = dt.date(2025, 2, 1)
        self.forecast = make_forecast("R1", self.start, 7, [400.0] * 7)
        self.assumptions = {"seats_per_vehicle": 55, "fleet_size": 4}

    def run_scenario(self, scenario: dict) -> dict:
        return decisions.simulate_scenario(
            "R1",
            scenario,
            demand_forecast=self.forecast,
            agency="AgencyA",
            capacity_assumptions=self.assumptions,
        )

    def test_increase_frequency_scales_capacity(self):
        result = self.run_scenario({"type": "increase_trip_frequency", "frequency_factor": 1.25})
        self.assertAlmostEqual(result["base_capacity"]["per_day"], 3410.0)
        self.assertAlmostEqual(result["scenario_capacity"]["per_day"], 4262.5)
        self.assertAlmostEqual(result["scenario_capacity"]["total"], 4262.5 * 7)
        self.assertAlmostEqual(result["demand"]["total"], 2800.0)
        self.assertEqual(result["demand"]["days"], 7)
        self.assertTrue(result["review_required"])
        self.assertIn("Estimate only", result["planning_note"])

    def test_add_vehicle_raises_capacity(self):
        result = self.run_scenario({"type": "add_vehicle", "vehicles_added": 2})
        self.assertAlmostEqual(result["scenario_capacity"]["per_day"], 3410.0 * 1.5)

    def test_peak_frequency_change_only_affects_peak(self):
        result = self.run_scenario({"type": "peak_frequency_change", "peak_headway_minutes": 10})
        self.assertAlmostEqual(result["scenario_capacity"]["per_day"], 55.0 * (24 + 42))

    def test_low_demand_reduction_only_affects_off_peak(self):
        result = self.run_scenario({"type": "low_demand_reduction", "reduction_factor": 0.5})
        self.assertAlmostEqual(result["scenario_capacity"]["per_day"], 55.0 * (20 + 21))

    def test_over_capacity_status_and_gap(self):
        result = self.run_scenario({"type": "increase_trip_frequency", "frequency_factor": 1.0})
        self.assertEqual(result["base_capacity"]["status"], "within_capacity")
        self.assertLess(result["base_capacity"]["gap_to_demand"], 0)
        self.assertAlmostEqual(result["scenario_capacity"]["change_in_total"], 0.0)

    def test_missing_forecast_is_reported_not_guessed(self):
        result = decisions.simulate_scenario(
            "R1", {"type": "add_vehicle", "vehicles_added": 1}, agency="AgencyA"
        )
        self.assertFalse(result["demand"]["available"])
        self.assertIsNone(result["demand"]["total"])
        self.assertEqual(result["base_capacity"]["status"], "unknown")
        self.assertAlmostEqual(result["scenario_capacity"]["per_day"], 3410.0 * 1.25)

    def test_unsupported_and_invalid_scenarios_raise(self):
        with self.assertRaises(ValueError):
            self.run_scenario({"type": "reschedule_everything"})
        with self.assertRaises(ValueError):
            self.run_scenario({"type": "increase_trip_frequency", "frequency_factor": 0})
        with self.assertRaises(ValueError):
            self.run_scenario({"type": "low_demand_reduction", "reduction_factor": 1.5})
        with self.assertRaises(ValueError):
            self.run_scenario({"type": "peak_frequency_change", "peak_headway_minutes": -3})

    def test_every_documented_scenario_type_is_supported(self):
        for scenario_type, spec in decisions.SCENARIO_SCHEMA.items():
            self.assertIn(scenario_type, decisions.SUPPORTED_SCENARIO_TYPES)
            self.assertTrue(spec["required"])


if __name__ == "__main__":
    unittest.main()
