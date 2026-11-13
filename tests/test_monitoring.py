"""Deterministic synthetic tests for the Phase 7 model-monitoring module."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src import monitoring

NOW = dt.datetime(2025, 6, 1, 12, tzinfo=dt.timezone.utc)


def make_data(days: int = 60, routes=("R1", "R2"), end: dt.date = dt.date(2025, 5, 31)) -> pd.DataFrame:
    rows = []
    start = end - dt.timedelta(days=days - 1)
    for route_index, route in enumerate(routes):
        base = 100.0 + 100.0 * route_index
        for offset in range(days):
            day = start + dt.timedelta(days=offset)
            weekend = 0.6 if day.weekday() >= 5 else 1.0
            rows.append(
                {
                    "service_date": day,
                    "route_id": route,
                    "passengers": round(base * weekend + (offset % 7), 2),
                }
            )
    return pd.DataFrame(rows)


def make_bundle(**overrides) -> dict:
    bundle = {
        "model_version": "9.9.9",
        "created_at_utc": "2025-05-28T00:00:00+00:00",
        "trained_through": "2025-05-31",
        "horizon": 7,
        "routes": ["R1", "R2"],
        "features": ["lag_1", "lag_7"],
        "data_sha256": "abc123",
        "interval_method": "rolling-origin 90th percentile absolute residual",
        "package_versions": {"pandas": "2.2.3", "scikit_learn": "1.5.2"},
    }
    bundle.update(overrides)
    return bundle


def make_metrics(**overrides) -> dict:
    metrics = {
        "backtest_folds": 4,
        "horizon_days": 7,
        "model": {"mae": 10.0, "rmse": 12.0, "wape": 0.10},
        "seasonal_naive": {"mae": 20.0, "rmse": 25.0, "wape": 0.18},
        "by_route": {
            "R1": {"model": {"mae": 8.0, "rmse": 9.0, "wape": 0.09}, "seasonal_naive": {"mae": 15.0, "rmse": 18.0, "wape": 0.15}},
            "R2": {"model": {"mae": 12.0, "rmse": 14.0, "wape": 0.11}, "seasonal_naive": {"mae": 22.0, "rmse": 27.0, "wape": 0.20}},
        },
        "model_selection_period": {"start": "2025-04-03", "end": "2025-05-03"},
        "final_holdout": {
            "days": 28,
            "refresh_interval_days": 7,
            "start": "2025-05-04",
            "end": "2025-05-31",
            "model": {"mae": 11.0, "rmse": 13.0, "wape": 0.12},
            "seasonal_naive": {"mae": 21.0, "rmse": 26.0, "wape": 0.19},
            "by_route": {
                "R1": {"model": {"mae": 9.0, "rmse": 10.0, "wape": 0.10}, "seasonal_naive": {"mae": 16.0, "rmse": 19.0, "wape": 0.16}},
                "R2": {"model": {"mae": 13.0, "rmse": 15.0, "wape": 0.13}, "seasonal_naive": {"mae": 23.0, "rmse": 28.0, "wape": 0.21}},
            },
            "error_analysis": {
                "by_lead_day": {
                    "7": {"mae": 15.0, "rmse": 17.0, "wape": 0.15},
                    "1": {"mae": 8.0, "rmse": 9.0, "wape": 0.08},
                    "3": {"mae": 11.0, "rmse": 12.0, "wape": 0.11},
                },
                "largest_absolute_errors": [],
            },
        },
    }
    metrics.update(overrides)
    return metrics


def make_forecast(days: int = 7, start: dt.date = dt.date(2025, 6, 1), routes=("R1", "R2")) -> pd.DataFrame:
    rows = []
    for offset in range(days):
        for route in routes:
            rows.append(
                {
                    "service_date": start + dt.timedelta(days=offset),
                    "route_id": route,
                    "predicted_passengers": 110.0,
                }
            )
    return pd.DataFrame(rows)


def snapshot(**kwargs):
    params = {
        "data": make_data(),
        "model_bundle": make_bundle(),
        "metrics": make_metrics(),
        "forecast": make_forecast(),
        "forecast_updated_at": "2025-06-01T06:00:00+00:00",
        "now": NOW,
        "policy": {"min_observations_per_route": 30},
    }
    params.update(kwargs)
    return monitoring.build_monitoring_snapshot(**params)


class SnapshotSectionTests(unittest.TestCase):
    def test_sections_are_present(self):
        result = snapshot()
        for key in (
            "generated_at", "model", "training_coverage", "data", "forecast",
            "metrics", "baseline_comparison", "distribution", "warnings", "retraining",
        ):
            self.assertIn(key, result)

    def test_model_metadata_and_age(self):
        model = snapshot()["model"]
        self.assertEqual(model["version"], "9.9.9")
        self.assertEqual(model["trained_through"], "2025-05-31")
        self.assertEqual(model["horizon_days"], 7)
        self.assertEqual(model["route_count"], 2)
        self.assertEqual(model["feature_count"], 2)
        self.assertEqual(model["data_sha256"], "abc123")
        self.assertAlmostEqual(model["age_days"], 4.5, places=2)

    def test_training_coverage(self):
        coverage = snapshot()["training_coverage"]
        self.assertEqual(coverage["route_count"], 2)
        self.assertEqual(coverage["span_days"], 60)
        self.assertEqual(coverage["last_observed_date"], "2025-05-31")
        self.assertEqual(coverage["min_observations_per_route"], 60)

    def test_missing_data_percentage(self):
        complete = snapshot()["data"]
        self.assertEqual(complete["missing_percent"], 0.0)
        self.assertEqual(complete["missing_route_days"], 0)

        partial = make_data().drop(index=[0, 1, 2])
        result = snapshot(data=partial)["data"]
        self.assertEqual(result["expected_route_days"], 120)
        self.assertEqual(result["missing_route_days"], 3)
        self.assertAlmostEqual(result["missing_percent"], 2.5, places=2)
        self.assertTrue(result["routes_with_gaps"])

    def test_forecast_freshness(self):
        forecast = snapshot()["forecast"]
        self.assertTrue(forecast["available"])
        self.assertEqual(forecast["horizon_days"], 7)
        self.assertEqual(forecast["first_date"], "2025-06-01")
        self.assertEqual(forecast["last_date"], "2025-06-07")
        self.assertAlmostEqual(forecast["age_days"], 0.25, places=2)

    def test_baseline_comparison_uses_holdout(self):
        comparison = snapshot()["baseline_comparison"]
        self.assertEqual(comparison["verdict"], "model_leads")
        self.assertAlmostEqual(comparison["model_wape"], 0.12)
        self.assertAlmostEqual(comparison["baseline_wape"], 0.19)
        self.assertAlmostEqual(comparison["gap"], -0.07, places=4)

    def test_error_by_route_is_sorted_and_flagged(self):
        metrics = snapshot(data=make_data(), metrics=make_metrics())["metrics"]
        self.assertEqual([item["route"] for item in metrics["by_route"]], ["R1", "R2"])
        first = metrics["by_route"][0]
        self.assertTrue(first["model_better"])
        self.assertAlmostEqual(first["wape_gap"], -0.06, places=4)

    def test_error_by_horizon_is_numeric_and_ordered(self):
        by_horizon = snapshot()["metrics"]["by_horizon"]
        self.assertEqual([item["horizon_days"] for item in by_horizon], [1, 3, 7])
        self.assertAlmostEqual(by_horizon[0]["wape"], 0.08)
        self.assertAlmostEqual(by_horizon[-1]["mae"], 15.0)

    def test_warnings_are_sorted_by_severity(self):
        result = snapshot(now=dt.datetime(2025, 6, 30, tzinfo=dt.timezone.utc))
        severities = [monitoring.SEVERITY_ORDER[item["severity"]] for item in result["warnings"]]
        self.assertEqual(severities, sorted(severities))
        self.assertIn("critical", {item["severity"] for item in result["warnings"]})


class WarningTests(unittest.TestCase):
    def codes(self, result) -> set:
        return {item["code"] for item in result["warnings"]}

    def test_clean_snapshot_has_no_warnings(self):
        self.assertEqual(snapshot()["warnings"], [])

    def test_stale_model_and_data(self):
        result = snapshot(now=dt.datetime(2025, 6, 20, tzinfo=dt.timezone.utc))
        codes = self.codes(result)
        self.assertIn("model_stale", codes)
        self.assertIn("data_stale", codes)
        self.assertIn("forecast_stale", codes)

    def test_missing_data_warning(self):
        partial = make_data().drop(index=range(0, 20))
        result = snapshot(data=partial)
        self.assertIn("missing_data", self.codes(result))
        self.assertGreater(result["data"]["missing_percent"], 5.0)

    def test_underperforms_baseline_warning(self):
        metrics = make_metrics(
            model={"mae": 30.0, "rmse": 33.0, "wape": 0.30},
            seasonal_naive={"mae": 20.0, "rmse": 25.0, "wape": 0.18},
            final_holdout={
                "days": 28, "start": "2025-05-04", "end": "2025-05-31",
                "model": {"mae": 31.0, "rmse": 34.0, "wape": 0.31},
                "seasonal_naive": {"mae": 21.0, "rmse": 26.0, "wape": 0.19},
                "error_analysis": {"by_lead_day": {}, "largest_absolute_errors": []},
            },
        )
        result = snapshot(metrics=metrics)
        warnings = {item["code"]: item for item in result["warnings"]}
        self.assertIn("underperforms_baseline", warnings)
        self.assertEqual(warnings["underperforms_baseline"]["severity"], "critical")
        self.assertEqual(result["baseline_comparison"]["verdict"], "baseline_leads")

    def test_holdout_degradation_warning(self):
        metrics = make_metrics()
        metrics["final_holdout"]["model"] = {"mae": 25.0, "rmse": 30.0, "wape": 0.25}
        result = snapshot(metrics=metrics)
        self.assertIn("holdout_degradation", self.codes(result))

    def test_distribution_shift_is_flagged(self):
        data = make_data()
        dates = pd.to_datetime(data["service_date"])
        recent = dates >= pd.Timestamp("2025-05-18")
        data.loc[recent, "passengers"] = data.loc[recent, "passengers"] * 2.0
        distribution = snapshot(data=data)["distribution"]
        self.assertTrue(distribution["available"])
        self.assertEqual(distribution["flagged_count"], 2)
        self.assertGreater(distribution["overall"]["change_percent"], 30.0)
        self.assertIn("distribution_shift", self.codes(snapshot(data=data)))

    def test_stable_distribution_is_not_flagged(self):
        distribution = snapshot()["distribution"]
        self.assertEqual(distribution["flagged_count"], 0)
        self.assertFalse(distribution["overall"]["flagged"])

    def test_no_model_no_data_snapshot_is_graceful(self):
        result = monitoring.build_monitoring_snapshot(now=NOW)
        codes = {item["code"] for item in result["warnings"]}
        self.assertIn("no_model", codes)
        self.assertIn("no_data", codes)
        self.assertIn("no_forecast", codes)
        self.assertFalse(result["model"]["available"])
        self.assertIsNone(result["data"]["missing_percent"])
        self.assertEqual(result["retraining"]["recommendation"], "retrain_candidate")

    def test_forecast_stale_warning(self):
        result = snapshot(forecast_updated_at="2025-05-01T00:00:00+00:00")
        self.assertIn("forecast_stale", self.codes(result))


class RetrainingTests(unittest.TestCase):
    def test_hold_when_every_check_passes(self):
        retraining = snapshot()["retraining"]
        self.assertEqual(retraining["recommendation"], "hold")
        self.assertFalse(retraining["auto_retrain"])
        self.assertTrue(retraining["requires_manual_approval"])
        self.assertEqual(retraining["failing_checks"], [])
        self.assertEqual({check["status"] for check in retraining["required_checks"]}, {"pass"})

    def test_retrain_candidate_when_policy_checks_fail(self):
        partial = make_data().drop(index=range(0, 20))
        result = snapshot(data=partial, now=dt.datetime(2025, 6, 20, tzinfo=dt.timezone.utc))
        retraining = result["retraining"]
        self.assertEqual(retraining["recommendation"], "retrain_candidate")
        self.assertIn("data_validation", retraining["failing_checks"])
        self.assertIn("model_age", retraining["failing_checks"])
        self.assertFalse(retraining["auto_retrain"])
        self.assertTrue(retraining["reasons"])

    def test_coverage_check_uses_policy_minimum(self):
        result = snapshot(policy={"min_observations_per_route": 90})
        self.assertEqual(result["retraining"]["recommendation"], "retrain_candidate")
        self.assertIn("coverage", result["retraining"]["failing_checks"])

    def test_insufficient_information_when_metrics_missing(self):
        result = snapshot(metrics=None)
        self.assertEqual(result["retraining"]["recommendation"], "insufficient_information")


class PolicyTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(monitoring.normalize_policy(), monitoring.DEFAULT_POLICY)
        policy = monitoring.normalize_policy({"max_model_age_days": 7})
        self.assertEqual(policy["max_model_age_days"], 7.0)
        self.assertEqual(policy["max_missing_percent"], monitoring.DEFAULT_POLICY["max_missing_percent"])

    def test_invalid_policies_raise(self):
        with self.assertRaises(ValueError):
            monitoring.normalize_policy({"unknown_field": 1})
        with self.assertRaises(ValueError):
            monitoring.normalize_policy({"max_model_age_days": "soon"})
        with self.assertRaises(ValueError):
            monitoring.normalize_policy({"max_missing_percent": 150})
        with self.assertRaises(ValueError):
            monitoring.normalize_policy({"max_model_age_days": 0})

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            saved = monitoring.save_policy(path, {"max_data_age_days": 5})
            self.assertEqual(saved["max_data_age_days"], 5.0)
            self.assertEqual(json.loads(path.read_text())["max_data_age_days"], 5.0)
            self.assertEqual(monitoring.load_policy(path)["max_data_age_days"], 5.0)

    def test_load_missing_or_corrupt_policy_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            self.assertEqual(monitoring.load_policy(path), monitoring.DEFAULT_POLICY)
            path.write_text("{not json")
            self.assertEqual(monitoring.load_policy(path), monitoring.DEFAULT_POLICY)


if __name__ == "__main__":
    unittest.main()
