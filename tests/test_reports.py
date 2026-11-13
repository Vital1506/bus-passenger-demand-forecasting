"""Deterministic tests for the Phase 8 downloadable reports."""

import datetime as dt
import json
import unittest

import pandas as pd

from src import reports

NOW = dt.datetime(2025, 7, 8, 9, tzinfo=dt.timezone.utc)


def make_data(days: int = 30, end: dt.date = dt.date(2025, 6, 30)) -> pd.DataFrame:
    rows = []
    start = end - dt.timedelta(days=days - 1)
    for route, base in (("R1", 100.0), ("R2", 200.0)):
        for offset in range(days):
            rows.append(
                {
                    "service_date": start + dt.timedelta(days=offset),
                    "route_id": route,
                    "passengers": base + offset % 7,
                }
            )
    return pd.DataFrame(rows)


def make_forecast(values: dict[str, float], start: dt.date = dt.date(2025, 7, 1), days: int = 7) -> pd.DataFrame:
    rows = []
    for offset in range(days):
        for route, value in values.items():
            rows.append(
                {
                    "service_date": start + dt.timedelta(days=offset),
                    "route_id": route,
                    "predicted_passengers": value,
                }
            )
    return pd.DataFrame(rows)


def make_metrics() -> dict:
    return {
        "model": {"mae": 10.0, "rmse": 12.0, "wape": 0.10},
        "seasonal_naive": {"mae": 20.0, "rmse": 25.0, "wape": 0.18},
        "by_route": {
            "R1": {"model": {"mae": 8.0, "rmse": 9.0, "wape": 0.09}, "seasonal_naive": {"mae": 15.0, "rmse": 18.0, "wape": 0.15}},
            "R2": {"model": {"mae": 12.0, "rmse": 14.0, "wape": 0.22}, "seasonal_naive": {"mae": 22.0, "rmse": 27.0, "wape": 0.20}},
        },
        "final_holdout": {
            "days": 7,
            "start": "2025-06-24",
            "end": "2025-06-30",
            "model": {"mae": 11.0, "rmse": 13.0, "wape": 0.11},
            "seasonal_naive": {"mae": 21.0, "rmse": 26.0, "wape": 0.19},
            "by_route": {
                "R1": {"model": {"mae": 9.0, "rmse": 10.0, "wape": 0.10}, "seasonal_naive": {"mae": 16.0, "rmse": 19.0, "wape": 0.16}},
                "R2": {"model": {"mae": 13.0, "rmse": 15.0, "wape": 0.25}, "seasonal_naive": {"mae": 23.0, "rmse": 28.0, "wape": 0.21}},
            },
            "error_analysis": {
                "by_lead_day": {
                    "3": {"mae": 11.0, "rmse": 12.0, "wape": 0.11},
                    "1": {"mae": 8.0, "rmse": 9.0, "wape": 0.08},
                }
            },
        },
    }


def capacity_config() -> dict:
    return {
        "R1": {"seats_per_vehicle": 10, "peak_headway_minutes": 30, "off_peak_headway_minutes": 60},
        "R2": {"seats_per_vehicle": 500, "peak_headway_minutes": 30, "off_peak_headway_minutes": 60},
    }


class CatalogTests(unittest.TestCase):
    def test_six_reports_and_availability(self):
        items = reports.catalog(
            data=make_data(),
            forecast=make_forecast({"R1": 500.0, "R2": 210.0}),
            metrics=make_metrics(),
            disruptions=[{"start": "2025-07-01T00:00:00Z", "agency": "AgencyA", "route": "R1", "effect": "DETOUR", "cause": "CONSTRUCTION", "header": "Bridge works"}],
            capacity=capacity_config(),
            now=NOW,
        )
        self.assertEqual([item["id"] for item in items], list(reports.REPORT_IDS))
        self.assertTrue(all(item["available"] for item in items), items)
        self.assertTrue(all(item["rows"] > 0 for item in items))

    def test_unavailable_reasons_are_explicit(self):
        items = {item["id"]: item for item in reports.catalog(now=NOW)}
        self.assertFalse(items["daily_demand"]["available"])
        self.assertIn("No daily observations", items["daily_demand"]["unavailable_reason"])
        self.assertIn("forecast", items["capacity_risk"]["unavailable_reason"].lower())
        self.assertIn("GTFS-Realtime", items["service_disruption"]["unavailable_reason"])

    def test_unknown_report_id_raises(self):
        with self.assertRaises(ValueError):
            reports.build_report("everything", now=NOW)


class DailyDemandTests(unittest.TestCase):
    def test_rows_compare_observed_and_forecast(self):
        forecast = make_forecast({"R1": 300.0, "R2": 400.0}, start=dt.date(2025, 6, 28))
        report = reports.build_report("daily_demand", data=make_data(), forecast=forecast, now=NOW)
        self.assertTrue(report["available"])
        self.assertEqual(report["rows"][0]["service_date"], "2025-07-04")
        row = next(item for item in report["rows"] if item["service_date"] == "2025-06-28")
        self.assertEqual(row["routes_observed"], 2)
        self.assertAlmostEqual(row["forecast_boardings"], 700.0)
        self.assertAlmostEqual(row["difference"], 700.0 - row["observed_boardings"])
        self.assertAlmostEqual(
            row["difference_percent"], 100.0 * (700.0 - row["observed_boardings"]) / row["observed_boardings"]
        )
        self.assertAlmostEqual(report["totals"]["observed_boardings"], float(make_data()["passengers"].sum()))

    def test_unavailable_without_data(self):
        report = reports.build_report("daily_demand", data=None, forecast=make_forecast({"R1": 1.0}), now=NOW)
        self.assertFalse(report["available"])
        self.assertEqual(report["rows"], [])


class WeeklyAndMonthlyTests(unittest.TestCase):
    def test_weekly_groups_by_monday_and_route(self):
        report = reports.build_report("weekly_route", data=make_data(), now=NOW)
        self.assertTrue(report["available"])
        self.assertEqual(len(report["rows"]), 12)
        for row in report["rows"]:
            week_start = dt.date.fromisoformat(row["week_start"])
            self.assertEqual(week_start.weekday(), 0)
        first = report["rows"][0]
        self.assertEqual(first["week_start"], "2025-06-30")
        self.assertAlmostEqual(report["totals"]["observed_boardings"], float(make_data()["passengers"].sum()))

    def test_week_including_a_forecast_reports_difference(self):
        forecast = make_forecast({"R1": 1000.0}, start=dt.date(2025, 6, 30), days=1)
        report = reports.build_report("weekly_route", data=make_data(), forecast=forecast, now=NOW)
        row = next(item for item in report["rows"] if item["week_start"] == "2025-06-30" and item["route"] == "R1")
        self.assertAlmostEqual(row["forecast_boardings"], 1000.0)
        self.assertIsNotNone(row["difference_percent"])

    def test_monthly_totals_and_average(self):
        report = reports.build_report("monthly_ridership", data=make_data(), now=NOW)
        self.assertEqual(len(report["rows"]), 1)
        row = report["rows"][0]
        self.assertEqual(row["month"], "2025-06")
        self.assertEqual(row["days_observed"], 30)
        self.assertEqual(row["routes_observed"], 2)
        self.assertAlmostEqual(row["observed_boardings"], float(make_data()["passengers"].sum()))
        self.assertAlmostEqual(row["average_per_day"], row["observed_boardings"] / 30)


class AccuracyTests(unittest.TestCase):
    def test_route_and_horizon_rows(self):
        report = reports.build_report("forecast_accuracy", metrics=make_metrics(), now=NOW)
        self.assertTrue(report["available"])
        routes = [row for row in report["rows"] if row["route"] in {"R1", "R2"}]
        self.assertEqual([row["route"] for row in routes], ["R1", "R2"])
        self.assertAlmostEqual(routes[0]["model_wape_percent"], 10.0)
        self.assertAlmostEqual(routes[0]["baseline_wape_percent"], 16.0)
        self.assertEqual(routes[0]["better"], "Model")
        self.assertEqual(routes[1]["better"], "Baseline")
        horizons = [row["route"] for row in report["rows"] if row["route"].startswith("Day ")]
        self.assertEqual(horizons, ["Day 1", "Day 3"])
        self.assertEqual(report["window"]["days"], 7)

    def test_falls_back_to_overall_scores(self):
        metrics = {"model": {"mae": 10, "rmse": 12, "wape": 0.1}, "seasonal_naive": {"mae": 20, "rmse": 25, "wape": 0.2}}
        report = reports.build_report("forecast_accuracy", metrics=metrics, now=NOW)
        self.assertTrue(report["available"])
        self.assertEqual(report["rows"][0]["route"], "all routes")
        self.assertEqual(report["rows"][0]["better"], "Model")

    def test_unavailable_without_metrics(self):
        self.assertFalse(reports.build_report("forecast_accuracy", now=NOW)["available"])


class DisruptionAndCapacityTests(unittest.TestCase):
    def test_disruption_rows(self):
        disruptions = [
            {"start": "2025-07-02T06:00:00Z", "agency": "AgencyA", "route": "R1", "effect": "SUSPENDED", "cause": "ACCIDENT", "header": "Route suspended"},
            {"start": "2025-07-05T06:00:00Z", "agency": "AgencyA", "route": "R2", "effect": "DETOUR", "cause": "CONSTRUCTION", "header": "Detour"},
        ]
        report = reports.build_report("service_disruption", disruptions=disruptions, now=NOW)
        self.assertTrue(report["available"])
        self.assertEqual([row["route"] for row in report["rows"]], ["R2", "R1"])
        self.assertEqual(report["totals"]["alerts"], 2)
        self.assertIn("GTFS-Realtime", report["notes"][2])

    def test_disruption_unavailable_without_feed(self):
        report = reports.build_report("service_disruption", disruptions=[], now=NOW)
        self.assertFalse(report["available"])
        self.assertIsNotNone(report["unavailable_reason"])

    def test_capacity_risk_levels(self):
        report = reports.build_report(
            "capacity_risk", forecast=make_forecast({"R1": 500.0, "R2": 210.0}), capacity=capacity_config(), now=NOW
        )
        self.assertTrue(report["available"])
        rows = {row["route"]: row for row in report["rows"]}
        self.assertEqual(rows["R1"]["risk"], "critical")
        self.assertEqual(rows["R2"]["risk"], "within_capacity")
        self.assertAlmostEqual(rows["R1"]["daily_capacity"], 220.0)
        self.assertAlmostEqual(rows["R1"]["utilization_percent"], 500.0 / 220.0 * 100.0)
        self.assertEqual(report["totals"]["at_or_over_capacity"], 1)
        self.assertEqual(report["rows"][0]["route"], "R1")

    def test_capacity_needs_both_inputs(self):
        self.assertFalse(reports.build_report("capacity_risk", capacity=capacity_config(), now=NOW)["available"])
        self.assertFalse(reports.build_report("capacity_risk", forecast=make_forecast({"R1": 1.0}), now=NOW)["available"])


class ExportTests(unittest.TestCase):
    def report(self) -> dict:
        return reports.build_report(
            "capacity_risk", forecast=make_forecast({"R1": 500.0, "R2": 210.0}), capacity=capacity_config(), now=NOW
        )

    def test_csv_uses_labels_and_raw_numbers(self):
        csv_text = reports.report_to_csv(self.report())
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], "ROUTE,PEAK FORECAST DATE,FORECAST PEAK BOARDINGS,DAILY SEAT CAPACITY,UTILIZATION,GAP (PEAK - CAPACITY),RISK SIGNAL")
        self.assertTrue(lines[1].startswith("R1,"))
        self.assertNotIn("%", lines[1])

    def test_csv_escapes_quotes_and_commas(self):
        report = {
            "columns": [{"key": "summary", "label": "SUMMARY", "format": "text"}],
            "rows": [{"summary": 'Detour, "Main St"'}],
        }
        csv_text = reports.report_to_csv(report)
        self.assertEqual(csv_text.strip(), 'SUMMARY\r\n"Detour, ""Main St"""')

    def test_printable_html(self):
        html_text = reports.report_to_html(self.report())
        self.assertIn("Capacity-risk report", html_text)
        self.assertIn("@media print", html_text)
        self.assertIn("Aggregate counts only", html_text)
        self.assertIn("<table class='data'>", html_text)
        self.assertIn("R1", html_text)

    def test_printable_html_for_unavailable_report(self):
        html_text = reports.report_to_html(reports.build_report("capacity_risk", now=NOW))
        self.assertIn("Report unavailable", html_text)
        self.assertIn("No forecast is available", html_text)

    def test_html_escapes_content(self):
        report = {
            "title": "Report <script>",
            "columns": [{"key": "summary", "label": "SUMMARY", "format": "text"}],
            "rows": [{"summary": "<img src=x onerror=alert(1)>"}],
            "notes": [],
            "generated_at": NOW.isoformat(),
            "window": {"start": None, "end": None, "days": 0},
            "available": True,
        }
        html_text = reports.report_to_html(report)
        self.assertNotIn("<script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertNotIn("<img src=x", html_text)


class ConfidentialityTests(unittest.TestCase):
    def test_aggregate_guard_rejects_individual_records(self):
        with self.assertRaises(ValueError):
            reports._assert_aggregate([{"route": "R1", "passenger_id": "abc"}])
        with self.assertRaises(ValueError):
            reports._assert_aggregate([{"route": "R1", "card_number": "1234"}])

    def test_every_report_is_marked_aggregate_only(self):
        for report_id in reports.REPORT_IDS:
            report = reports.build_report(
                report_id,
                data=make_data(),
                forecast=make_forecast({"R1": 500.0, "R2": 210.0}),
                metrics=make_metrics(),
                disruptions=[{"start": "2025-07-01", "route": "R1", "effect": "DETOUR", "cause": "CONSTRUCTION", "header": "x"}],
                capacity=capacity_config(),
                now=NOW,
            )
            self.assertEqual(report["confidentiality"], "aggregate_only")
            self.assertIn(reports.NOTICE, report["notes"])
            serialized = json.dumps(report["rows"])
            for field in reports.FORBIDDEN_FIELDS:
                self.assertNotIn(f'"{field}"', serialized)


if __name__ == "__main__":
    unittest.main()
