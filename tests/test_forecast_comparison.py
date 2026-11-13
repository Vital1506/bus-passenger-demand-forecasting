"""Experimental model-comparison framework tests.

These tests are standalone: each one builds its own small temp SQLite-free
in-memory panel and its own validated history, so the comparison module is
exercised without touching the existing operational training pipeline.
"""

import unittest

import pandas as pd

from src.compare import (
    COMPARISON_METRIC,
    MIN_WAPE_IMPROVEMENT,
    _candidate_names,
    _comparison_cutoffs,
    candidate_registry,
    compare_models,
)
from src.forecasting import complete_daily_series


class TestForecastComparisonRegistry(unittest.TestCase):
    def test_registry_lists_declared_candidates(self):
        registry = candidate_registry()
        self.assertIn("hist_gradient_boosting", registry)
        self.assertIn("random_forest", registry)

    def test_registry_is_read_only_view(self):
        registry = candidate_registry()
        entry = registry["hist_gradient_boosting"]
        self.assertEqual(entry["label"], "HistGradientBoostingRegressor")
        self.assertIn("notes", entry)


class TestForecastComparisonNames(unittest.TestCase):
    def test_defaults_include_all_declared_candidates(self):
        names = _candidate_names(None)
        self.assertIn("hist_gradient_boosting", names)
        self.assertIn("random_forest", names)

    def test_explicit_candidates_are_sorted(self):
        names = _candidate_names(["random_forest", "hist_gradient_boosting"])
        self.assertEqual(names, ["hist_gradient_boosting", "random_forest"])


class TestForecastComparisonCutoffs(unittest.TestCase):
    def test_cutoffs_need_continuous_history(self):
        short = pd.DataFrame({
            "service_date": pd.date_range("2025-01-01", periods=40, freq="D"),
            "route_id": ["R1"] * 40,
            "passengers": list(range(100, 140)),
        })
        with self.assertRaisesRegex(ValueError, "Not enough data for comparison"):
            _comparison_cutoffs(short, horizon=7)

    def test_cutoffs_allocate_holdout_at_the_end(self):
        history = pd.DataFrame({
            "service_date": pd.date_range("2025-01-01", periods=140, freq="D"),
            "route_id": ["R1"] * 140,
            "passengers": list(range(100, 240)),
        })
        training_start, training_end, holdout_start, holdout_end = _comparison_cutoffs(history, horizon=7)
        self.assertEqual((holdout_end - holdout_start).days, 6)
        self.assertEqual((training_end - training_start).days, 89)
        self.assertGreater(holdout_start, training_end)


class TestForecastComparisonOnEnoughHistory(unittest.TestCase):
    def setUp(self):
        multi_route = []
        start = pd.Timestamp("2025-01-01")
        for route, base in [("R1", 100), ("R2", 160), ("R3", 220)]:
            for day in range(140):
                multi_route.append({
                    "service_date": start + pd.Timedelta(days=day),
                    "route_id": route,
                    "passengers": float(base + day * 0.6 + (day % 7) * 2),
                })
        raw = pd.DataFrame(multi_route).sort_values(["route_id", "service_date"]).reset_index(drop=True)
        self.history = complete_daily_series(raw)

    def test_comparison_includes_seasonal_naive(self):
        result = compare_models(self.history, horizon=7)
        self.assertIn("seasonal_naive", result["ranking_by_wape"])
        self.assertEqual(result["baseline"]["name"], "seasonal_naive")
        self.assertIn("metric", result["baseline"])
        self.assertIn("wape", result["baseline"]["metric"])
        self.assertIn(result["comparison_metric"], result["baseline"]["metric"])

    def test_comparison_evaluates_all_default_candidates(self):
        result = compare_models(self.history, horizon=7)
        for name in _candidate_names(None):
            self.assertIn(name, result["candidates"])
            self.assertIn("wape", result["candidates"][name]["metric"])

    def test_comparison_carries_mae_and_rmse_too(self):
        result = compare_models(self.history, horizon=7)
        for name in _candidate_names(None):
            metrics = result["candidates"][name]["metric"]
            self.assertIn("mae", metrics)
            self.assertIn("rmse", metrics)

    def test_comparison_ranking_is_by_wape(self):
        result = compare_models(self.history, horizon=7)
        wapes = {
            name: (
                result["baseline"]["metric"]["wape"]
                if name == "seasonal_naive"
                else result["candidates"][name]["metric"]["wape"]
            )
            for name in result["ranking_by_wape"]
        }
        self.assertEqual(
            list(wapes.keys()),
            sorted(wapes, key=lambda name: wapes[name]),
        )

    def test_comparison_keeps_decision_note_readable(self):
        result = compare_models(self.history, horizon=7)
        note = result["decision_note"]
        self.assertIsInstance(note, str)
        self.assertGreater(len(note), 20)
        if result["preferred_over_baseline"] is None:
            self.assertIn("no model beat", note.lower()) or self.assertIn(
                "did not beat", note.lower()
            ) or self.assertIn("retained", note.lower())
        else:
            self.assertIn("preferred", note.lower())

    def test_comparison_can_prefer_a_candidate_over_baseline(self):
        result = compare_models(self.history, horizon=7)
        if result["preferred_over_baseline"] is not None:
            preferred = result["preferred_over_baseline"]
            self.assertNotEqual(preferred, "seasonal_naive")
            preferred_score = result["candidates"][preferred]["metric"]["wape"]
            baseline_score = result["baseline"]["metric"]["wape"]
            self.assertLess(preferred_score, baseline_score - MIN_WAPE_IMPROVEMENT)


class TestForecastComparisonOnTooLittleHistory(unittest.TestCase):
    def setUp(self):
        short = pd.DataFrame({
            "service_date": pd.date_range("2025-01-01", periods=40, freq="D"),
            "route_id": ["R1"] * 40,
            "passengers": list(range(100, 140)),
        })
        self.history = complete_daily_series(short)

    def test_comparison_refuses_when_history_is_too_short(self):
        with self.assertRaisesRegex(ValueError, "Not enough data for comparison"):
            compare_models(self.history, horizon=7)


class TestForecastComparisonNewRouteColdStart(unittest.TestCase):
    def setUp(self):
        history = []
        start = pd.Timestamp("2025-01-01")
        for route, base in [("R1", 100), ("R2", 160)]:
            for day in range(140):
                history.append({
                    "service_date": start + pd.Timedelta(days=day),
                    "route_id": route,
                    "passengers": float(base + day * 0.6),
                })
        self.history = complete_daily_series(pd.DataFrame(history))

    def test_comparison_refuses_when_holdout_has_a_route_absent_from_training(self):
        extended = pd.DataFrame([
            *self.history.to_dict("records"),
            {
                "service_date": self.history["service_date"].max() + pd.Timedelta(days=1),
                "route_id": "R3",
                "passengers": 300.0,
            },
        ])
        extended = complete_daily_series(extended, missing_days="zero")
        with self.assertRaisesRegex(ValueError, "Not enough data for comparison"):
            compare_models(extended, horizon=7)


if __name__ == "__main__":
    unittest.main()
