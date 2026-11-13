"""Deterministic tests for the Phase 8 notification framework."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from src import notifications

NOW = dt.datetime(2025, 6, 1, 12, tzinfo=dt.timezone.utc)


def high_demand_alert(predicted: float = 900.0) -> dict:
    return {
        "type": "unusually_high_predicted_demand",
        "agency": "AgencyA",
        "route_or_trip": "R1",
        "time": NOW.isoformat(),
        "what": "Forecast demand for route R1 on 2025-06-02 is 900.0 passengers, above the 90th percentile.",
        "supporting_observations": ["Observed days used: 30"],
        "relevant_forecast": {"service_date": "2025-06-02", "route_id": "R1", "predicted_passengers": predicted, "days_ahead": 1},
        "threshold": {"method": "observed 90th percentile", "value": 400.0},
        "explanation": "Above the recent band.",
        "suggested_action": "Review vehicle allocation.",
        "data_confidence": "medium",
        "acknowledged": False,
    }


def capacity_alert(predicted: float = 5000.0, capacity: float = 3410.0) -> dict:
    alert = high_demand_alert(predicted)
    alert.update(
        {
            "type": "route_above_capacity_limit",
            "what": "Forecast demand is above the configured daily capacity.",
            "threshold": {"method": "configured daily capacity", "value": capacity},
        }
    )
    return alert


def monitoring_warning(code: str, severity: str = "critical", title: str = "Warning") -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": "Aggregate signal detail.",
        "suggested_action": "Review the dashboard.",
    }


class PolicyTests(unittest.TestCase):
    def test_default_policy_sections(self):
        policy = notifications.normalize_policy()
        self.assertEqual(set(policy), {"in_app", "email", "webhook", "thresholds"})
        self.assertTrue(policy["in_app"]["enabled"])
        self.assertFalse(policy["email"]["enabled"])
        self.assertFalse(policy["webhook"]["enabled"])
        self.assertEqual(policy["thresholds"]["min_severity"], "warning")

    def test_override_merges_with_defaults(self):
        policy = notifications.normalize_policy({"thresholds": {"min_severity": "info", "max_notifications_per_run": 5}})
        self.assertEqual(policy["thresholds"]["min_severity"], "info")
        self.assertEqual(policy["thresholds"]["max_notifications_per_run"], 5)
        self.assertEqual(policy["thresholds"]["demand_change_percent"], 30.0)

    def test_unknown_and_invalid_fields_raise(self):
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"slack": {"enabled": True}})
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"email": {"unknown": 1}})
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"thresholds": {"min_severity": "loud"}})
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"email": {"port": 70000}})
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"email": {"provider": "carrier-pigeon"}})
        with self.assertRaises(notifications.NotificationConfigError):
            notifications.normalize_policy({"thresholds": {"enabled_alert_types": [""]}})

    def test_public_policy_redacts_secrets(self):
        policy = notifications.normalize_policy(
            {
                "email": {"enabled": True, "password": "smtp-secret", "token": "api-token", "recipients": ["ops@example.com"]},
                "webhook": {"enabled": True, "url": "https://hooks.example.com/x", "secret": "hook-secret"},
            }
        )
        public = notifications.public_policy(policy)
        self.assertEqual(public["email"]["password"], "")
        self.assertEqual(public["webhook"]["secret"], "")
        self.assertTrue(public["email"]["has_password"])
        self.assertTrue(public["email"]["has_token"])
        self.assertTrue(public["webhook"]["has_secret"])
        self.assertEqual(public["email"]["recipients"], ["ops@example.com"])
        self.assertNotIn("smtp-secret", json.dumps(public))

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            notifications.save_policy(path, {"thresholds": {"min_severity": "critical"}})
            notifications.save_policy(path, {"webhook": {"url": "https://hooks.example.com/a"}})
            loaded = notifications.load_policy(path)
            self.assertEqual(loaded["thresholds"]["min_severity"], "critical")
            self.assertEqual(loaded["webhook"]["url"], "https://hooks.example.com/a")

    def test_partial_section_update_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            notifications.save_policy(
                path,
                {"email": {"enabled": True, "password": "keep-me", "recipients": ["ops@example.com"]}},
            )
            notifications.save_policy(path, {"email": {"enabled": False}})
            loaded = notifications.load_policy(path)
            self.assertEqual(loaded["email"]["password"], "keep-me")
            self.assertEqual(loaded["email"]["recipients"], ["ops@example.com"])
            self.assertFalse(loaded["email"]["enabled"])

    def test_corrupt_policy_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            self.assertEqual(notifications.load_policy(path)["thresholds"]["min_severity"], "warning")
            path.write_text("{broken")
            self.assertEqual(notifications.load_policy(path)["thresholds"]["min_severity"], "warning")


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = notifications.normalize_policy()

    def test_alert_severity_and_shape(self):
        items = notifications.build_notifications([capacity_alert()], [], self.policy, now=NOW)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["severity"], "critical")
        self.assertEqual(item["code"], "route_above_capacity_limit")
        self.assertEqual(item["route"], "R1")
        self.assertEqual(item["source"], "alert")
        self.assertEqual(item["confidentiality"], "aggregate_only")
        self.assertFalse(item["acknowledged"])
        self.assertTrue(item["id"])

    def test_min_severity_filters_low_severity(self):
        policy = notifications.normalize_policy({"thresholds": {"min_severity": "critical"}})
        items = notifications.build_notifications([high_demand_alert()], [], policy, now=NOW)
        self.assertEqual(items, [])

    def test_enabled_alert_types_filter(self):
        policy = notifications.normalize_policy(
            {"thresholds": {"enabled_alert_types": ["missing_boarding_records"]}}
        )
        items = notifications.build_notifications([high_demand_alert()], [], policy, now=NOW)
        self.assertEqual(items, [])

    def test_min_predicted_passengers_threshold(self):
        policy = notifications.normalize_policy({"thresholds": {"min_predicted_passengers": 1000}})
        self.assertEqual(notifications.build_notifications([high_demand_alert(900)], [], policy, now=NOW), [])
        self.assertEqual(len(notifications.build_notifications([high_demand_alert(1200)], [], policy, now=NOW)), 1)

    def test_capacity_utilization_threshold(self):
        policy = notifications.normalize_policy({"thresholds": {"capacity_utilization_percent": 95}})
        # 3500 / 3410 = 102.6% utilization -> passes; 3000 / 3410 = 88% -> filtered
        self.assertEqual(len(notifications.build_notifications([capacity_alert(3500)], [], policy, now=NOW)), 1)
        self.assertEqual(notifications.build_notifications([capacity_alert(3000)], [], policy, now=NOW), [])

    def test_monitoring_thresholds(self):
        policy = notifications.normalize_policy(
            {"thresholds": {"max_forecast_error_percent": 20, "demand_change_percent": 50}}
        )
        monitoring = {
            "baseline_comparison": {"model_wape": 0.10},
            "distribution": {"overall": {"change_percent": 20.0}},
        }
        warnings = [
            monitoring_warning("underperforms_baseline"),
            monitoring_warning("distribution_shift", severity="warning"),
            monitoring_warning("data_stale"),
        ]
        items = notifications.build_notifications([], warnings, policy, monitoring=monitoring, now=NOW)
        # WAPE 10% is under the 20% ceiling and the 20% drift is under the 50% ceiling.
        self.assertEqual([item["code"] for item in items], ["data_stale"])

        monitoring["baseline_comparison"]["model_wape"] = 0.35
        monitoring["distribution"]["overall"]["change_percent"] = 80.0
        items = notifications.build_notifications([], warnings, policy, monitoring=monitoring, now=NOW)
        self.assertEqual({item["code"] for item in items}, {"underperforms_baseline", "distribution_shift", "data_stale"})

    def test_dedupe_and_max_per_run(self):
        policy = notifications.normalize_policy({"thresholds": {"max_notifications_per_run": 2}})
        alerts = [high_demand_alert(900), high_demand_alert(950), capacity_alert()]
        items = notifications.build_notifications(alerts, [], policy, now=NOW)
        self.assertEqual(len(items), 2)
        self.assertEqual(len({(item["code"], item["route"]) for item in items}), 2)

    def test_notifications_contain_no_individual_records(self):
        items = notifications.build_notifications([capacity_alert()], [monitoring_warning("data_stale")], self.policy, now=NOW)
        serialized = json.dumps(items)
        for field in ("passenger_id", "card_number", "ticket_id", "email", "phone"):
            self.assertNotIn(field, serialized)


class StorageTests(unittest.TestCase):
    def test_append_dedupe_window_and_read_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.jsonl"
            first = notifications.build_notifications([high_demand_alert()], [], notifications.normalize_policy(), now=NOW)
            result = notifications.append_notifications(path, first, now=NOW)
            self.assertEqual(result["added"], 1)
            again = notifications.build_notifications([high_demand_alert()], [], notifications.normalize_policy(), now=NOW)
            self.assertEqual(notifications.append_notifications(path, again, now=NOW)["skipped"], 1)

            later = NOW + dt.timedelta(days=2)
            fresh = notifications.build_notifications([high_demand_alert()], [], notifications.normalize_policy(), now=later)
            self.assertEqual(notifications.append_notifications(path, fresh, now=later)["added"], 1)

            records = notifications.read_notifications(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["created_at"], later.isoformat())

    def test_acknowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.jsonl"
            items = notifications.build_notifications([high_demand_alert()], [], notifications.normalize_policy(), now=NOW)
            notifications.append_notifications(path, items, now=NOW)
            self.assertTrue(notifications.acknowledge_notification(path, items[0]["id"]))
            self.assertTrue(notifications.read_notifications(path)[0]["acknowledged"])
            self.assertFalse(notifications.acknowledge_notification(path, "missing-id"))

    def test_max_items_trims_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.jsonl"
            for offset in range(4):
                items = notifications.build_notifications(
                    [capacity_alert()], [], notifications.normalize_policy(), now=NOW + dt.timedelta(days=offset)
                )
                items[0]["id"] = f"id-{offset}"
                notifications.append_notifications(path, items, now=NOW + dt.timedelta(days=offset), max_items=2)
            self.assertEqual(len(notifications.read_notifications(path)), 2)

    def test_summarize_counts(self):
        items = notifications.build_notifications(
            [capacity_alert(), high_demand_alert()], [monitoring_warning("data_stale")], notifications.normalize_policy(), now=NOW
        )
        summary = notifications.summarize(items)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["unacknowledged"], 3)
        self.assertEqual(summary["critical"], 2)


class DeliveryTests(unittest.TestCase):
    def test_in_app_only_by_default(self):
        items = notifications.build_notifications([high_demand_alert()], [], notifications.normalize_policy(), now=NOW)
        result = notifications.deliver(items, notifications.normalize_policy())
        self.assertEqual(result["in_app"], 1)
        self.assertEqual(result["email"]["sent"], 0)
        self.assertEqual(result["webhook"]["sent"], 0)
        self.assertIn("in_app", items[0]["delivered"])

    def test_email_delivery_uses_configured_transport(self):
        sent = []
        policy = notifications.normalize_policy(
            {"email": {"enabled": True, "recipients": ["ops@example.com"], "sender": "alerts@example.com"}}
        )
        items = notifications.build_notifications([high_demand_alert()], [], policy, now=NOW)
        result = notifications.deliver(items, policy, transports={"email": lambda config, message: sent.append(message)})
        self.assertEqual(result["email"]["sent"], 1)
        self.assertEqual(sent[0]["To"], "ops@example.com")
        self.assertIn("[WARNING]", sent[0]["Subject"])
        self.assertIn("no individual passenger records", sent[0].get_content())
        self.assertIn("email", items[0]["delivered"])

    def test_email_without_recipients_is_reported_not_raised(self):
        policy = notifications.normalize_policy({"email": {"enabled": True}})
        items = notifications.build_notifications([high_demand_alert()], [], policy, now=NOW)
        result = notifications.deliver(items, policy, transports={"email": lambda config, message: None})
        self.assertEqual(result["email"]["sent"], 0)
        self.assertTrue(result["email"]["errors"])

    def test_transport_failures_are_collected(self):
        def boom(config, message):
            raise OSError("smtp unreachable")

        policy = notifications.normalize_policy(
            {"email": {"enabled": True, "recipients": ["ops@example.com"]}, "webhook": {"enabled": True, "url": "https://hooks.example.com/x"}}
        )
        items = notifications.build_notifications([high_demand_alert()], [], policy, now=NOW)
        result = notifications.deliver(items, policy, transports={"email": boom, "webhook": boom})
        self.assertEqual(result["email"]["failed"], 1)
        self.assertEqual(result["webhook"]["failed"], 1)
        self.assertIn("smtp unreachable", result["email"]["errors"][0])

    def test_webhook_payload_is_aggregate(self):
        payloads = []
        policy = notifications.normalize_policy({"webhook": {"enabled": True, "url": "https://hooks.example.com/x", "secret": "s3cret"}})
        items = notifications.build_notifications([capacity_alert()], [], policy, now=NOW)
        result = notifications.deliver(items, policy, transports={"webhook": lambda config, payload: payloads.append((config, payload))})
        self.assertEqual(result["webhook"]["sent"], 1)
        config, payload = payloads[0]
        self.assertEqual(config["secret"], "s3cret")
        self.assertEqual(payload["event"], "bus_demand_notification")
        self.assertEqual(payload["confidentiality"], "aggregate_only")
        self.assertNotIn("passengers", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
