"""Operator notifications: in-app records, email, and webhooks.

Design rules:

* Notification content is **aggregate only** — route and date level counts.
  No individual passenger, ticket, card, or contact record is ever included.
* Nothing here sends anything unless the operator enabled that channel and an
  evaluation run is explicitly triggered.
* Secrets (SMTP password, HTTP email token, webhook secret) are stored
  server-side and are replaced by ``has_*`` flags in every API response.
* Thresholds are configurable and validated; a notification that does not
  cross its configured threshold is not created.
"""

from __future__ import annotations

import datetime as dt
import json
import smtplib
import urllib.request
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

ALERT_SEVERITY = {
    "unusually_high_predicted_demand": "warning",
    "route_above_capacity_limit": "critical",
    "unusually_low_demand": "info",
    "sudden_passenger_count_variation": "warning",
    "missing_boarding_records": "warning",
    "stale_gps_or_weather_feed": "critical",
    "stale_forecasting_model": "warning",
    "significant_increase_in_forecast_error": "critical",
}

DEFAULT_ALERT_TYPES = tuple(ALERT_SEVERITY)

DEFAULT_POLICY: dict[str, Any] = {
    "in_app": {"enabled": True, "max_items": 200},
    "email": {
        "enabled": False,
        "provider": "smtp",
        "host": "",
        "port": 587,
        "use_tls": True,
        "username": "",
        "password": "",
        "sender": "",
        "recipients": [],
        "url": "",
        "token": "",
        "timeout_seconds": 10,
    },
    "webhook": {"enabled": False, "url": "", "secret": "", "timeout_seconds": 10},
    "thresholds": {
        "min_severity": "warning",
        "enabled_alert_types": list(DEFAULT_ALERT_TYPES),
        "min_predicted_passengers": 0,
        "capacity_utilization_percent": 90,
        "max_forecast_error_percent": 25,
        "demand_change_percent": 30,
        "max_notifications_per_run": 50,
    },
}

SECRET_FIELDS = {"email": ("password", "token"), "webhook": ("secret",)}

SCHEMA: dict[str, dict[str, tuple]] = {
    "in_app": {
        "enabled": ("bool",),
        "max_items": ("int", 1, 5000),
    },
    "email": {
        "enabled": ("bool",),
        "provider": ("enum", ("smtp", "http")),
        "host": ("str", 200),
        "port": ("int", 1, 65535),
        "use_tls": ("bool",),
        "username": ("str", 200),
        "password": ("str", 500),
        "sender": ("str", 320),
        "recipients": ("str_list", 20, 320),
        "url": ("str", 500),
        "token": ("str", 500),
        "timeout_seconds": ("float", 1, 120),
    },
    "webhook": {
        "enabled": ("bool",),
        "url": ("str", 500),
        "secret": ("str", 500),
        "timeout_seconds": ("float", 1, 120),
    },
    "thresholds": {
        "min_severity": ("enum", SEVERITIES),
        "enabled_alert_types": ("str_list", len(DEFAULT_ALERT_TYPES), 80),
        "min_predicted_passengers": ("float", 0, 10_000_000),
        "capacity_utilization_percent": ("float", 1, 1000),
        "max_forecast_error_percent": ("float", 0, 1000),
        "demand_change_percent": ("float", 1, 1000),
        "max_notifications_per_run": ("int", 1, 500),
    },
}


class NotificationConfigError(ValueError):
    """Raised when a notification policy or payload is invalid."""


def _validate_value(section: str, key: str, value: Any, rule: tuple):
    kind = rule[0]
    if kind == "bool":
        if not isinstance(value, bool):
            raise NotificationConfigError(f"{section}.{key} must be true or false")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise NotificationConfigError(f"{section}.{key} must be a whole number")
        if not rule[1] <= value <= rule[2]:
            raise NotificationConfigError(f"{section}.{key} must be between {rule[1]} and {rule[2]}")
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise NotificationConfigError(f"{section}.{key} must be a number")
        numeric = float(value)
        if not rule[1] <= numeric <= rule[2]:
            raise NotificationConfigError(f"{section}.{key} must be between {rule[1]} and {rule[2]}")
        return numeric
    if kind == "str":
        if not isinstance(value, str):
            raise NotificationConfigError(f"{section}.{key} must be text")
        if len(value) > rule[1]:
            raise NotificationConfigError(f"{section}.{key} is too long")
        return value
    if kind == "enum":
        if value not in rule[1]:
            raise NotificationConfigError(f"{section}.{key} must be one of: {', '.join(rule[1])}")
        return value
    if kind == "str_list":
        max_items, max_length = rule[1], rule[2]
        if not isinstance(value, (list, tuple)):
            raise NotificationConfigError(f"{section}.{key} must be a list")
        if len(value) > max_items:
            raise NotificationConfigError(f"{section}.{key} accepts at most {max_items} entries")
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise NotificationConfigError(f"{section}.{key} entries must be non-empty text")
            if len(item) > max_length:
                raise NotificationConfigError(f"{section}.{key} entry is too long")
            items.append(item.strip())
        return items
    raise NotificationConfigError(f"{section}.{key} has an unsupported rule")


def normalize_policy(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deep-merge overrides into the defaults, rejecting unknown or invalid fields."""
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    if not overrides:
        return policy
    if not isinstance(overrides, dict):
        raise NotificationConfigError("Notification policy must be an object")
    for section, values in overrides.items():
        if section not in SCHEMA:
            raise NotificationConfigError(f"Unknown notification policy section: {section}")
        if not isinstance(values, dict):
            raise NotificationConfigError(f"{section} must be an object")
        for key, value in values.items():
            if key not in SCHEMA[section]:
                raise NotificationConfigError(f"Unknown notification policy field: {section}.{key}")
            policy[section][key] = _validate_value(section, key, value, SCHEMA[section][key])
    return policy


def load_policy(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return json.loads(json.dumps(DEFAULT_POLICY))
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        return normalize_policy(payload if isinstance(payload, dict) else None)
    except (OSError, json.JSONDecodeError, NotificationConfigError):
        return json.loads(json.dumps(DEFAULT_POLICY))


def save_policy(path: str | Path, overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overrides`` field by field into the stored policy and persist it."""
    file_path = Path(path)
    current = load_policy(file_path)
    for section, values in _merge_sections(current, _deep_sections(overrides)).items():
        current[section] = values
    merged = normalize_policy(current)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(file_path)
    return merged


def _deep_sections(overrides: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(overrides, dict):
        return {}
    return {section: values for section, values in overrides.items() if isinstance(values, dict)}


def _merge_sections(current: dict[str, Any], overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Field-level merge so a partial update never wipes the rest of a section."""
    merged: dict[str, dict[str, Any]] = {}
    for section, values in overrides.items():
        base = current.get(section)
        merged[section] = {**(base if isinstance(base, dict) else {}), **values}
    return merged


def public_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return a copy safe to send to a browser: secrets become flags."""
    public = json.loads(json.dumps(policy))
    for section, fields in SECRET_FIELDS.items():
        for field in fields:
            value = public.get(section, {}).get(field)
            public[section][f"has_{field}"] = bool(value)
            public[section][field] = ""
    return public


# ---------------------------------------------------------------------------
# Building notifications
# ---------------------------------------------------------------------------


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _fmt(value: float | None, digits: int = 1) -> str:
    return "unknown" if value is None else f"{value:,.{digits}f}"


def _notification(
    *,
    source: str,
    code: str,
    severity: str,
    title: str,
    message: str,
    action: str,
    route: str | None,
    created_at: str,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "created_at": created_at,
        "source": source,
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "action": action,
        "route": route,
        "acknowledged": False,
        "delivered": [],
        "confidentiality": "aggregate_only",
    }


def _alert_passes_thresholds(alert: dict[str, Any], thresholds: dict[str, Any]) -> bool:
    forecast = alert.get("relevant_forecast") or {}
    predicted = _number(forecast.get("predicted_passengers"))
    if alert["type"] == "unusually_high_predicted_demand" and predicted is not None:
        if predicted < thresholds["min_predicted_passengers"]:
            return False
    if alert["type"] == "route_above_capacity_limit":
        capacity = _number((alert.get("threshold") or {}).get("value"))
        if predicted is not None and capacity:
            utilization = 100.0 * predicted / capacity
            if utilization < thresholds["capacity_utilization_percent"]:
                return False
    return True


def notifications_from_alerts(
    alerts: list[dict[str, Any]],
    thresholds: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Convert decision-support alerts into notification candidates."""
    timestamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    minimum = SEVERITY_RANK[thresholds["min_severity"]]
    enabled = set(thresholds["enabled_alert_types"])
    items: list[dict[str, Any]] = []
    for alert in alerts or []:
        alert_type = str(alert.get("type") or "")
        if alert_type not in enabled:
            continue
        severity = ALERT_SEVERITY.get(alert_type, "warning")
        if SEVERITY_RANK[severity] < minimum:
            continue
        if not _alert_passes_thresholds(alert, thresholds):
            continue
        items.append(
            _notification(
                source="alert",
                code=alert_type,
                severity=severity,
                title=str(alert.get("what") or alert_type).split(".")[0][:180],
                message=str(alert.get("what") or ""),
                action=str(alert.get("suggested_action") or ""),
                route=str(alert.get("route_or_trip") or "") or None,
                created_at=timestamp,
            )
        )
    return items


def notifications_from_monitoring_warnings(
    warnings: list[dict[str, Any]],
    thresholds: dict[str, Any],
    monitoring: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Convert model-monitoring warnings into notification candidates."""
    timestamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    minimum = SEVERITY_RANK[thresholds["min_severity"]]
    snapshot = monitoring or {}
    comparison = snapshot.get("baseline_comparison") or {}
    model_wape = _number(comparison.get("model_wape"))
    distribution = snapshot.get("distribution") or {}
    overall_change = _number((distribution.get("overall") or {}).get("change_percent"))

    items: list[dict[str, Any]] = []
    for warning in warnings or []:
        severity = str(warning.get("severity") or "warning")
        if severity not in SEVERITY_RANK or SEVERITY_RANK[severity] < minimum:
            continue
        code = str(warning.get("code") or "monitoring_warning")
        if code in {"underperforms_baseline", "holdout_degradation"} and model_wape is not None:
            if model_wape * 100.0 < thresholds["max_forecast_error_percent"]:
                continue
        if code == "distribution_shift":
            if overall_change is None or abs(overall_change) < thresholds["demand_change_percent"]:
                continue
        items.append(
            _notification(
                source="monitoring",
                code=code,
                severity=severity,
                title=str(warning.get("title") or code)[:180],
                message=str(warning.get("detail") or ""),
                action=str(warning.get("suggested_action") or ""),
                route=None,
                created_at=timestamp,
            )
        )
    return items


def build_notifications(
    alerts: list[dict[str, Any]] | None,
    warnings: list[dict[str, Any]] | None,
    policy: dict[str, Any],
    monitoring: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Build the deduplicated notification list for one evaluation run."""
    thresholds = policy["thresholds"]
    candidates = notifications_from_alerts(alerts or [], thresholds, now=now)
    candidates.extend(notifications_from_monitoring_warnings(warnings or [], thresholds, monitoring, now=now))
    candidates.sort(key=lambda item: (-SEVERITY_RANK.get(item["severity"], 0), item["created_at"]))
    seen: set[tuple[str, str | None]] = set()
    unique: list[dict[str, Any]] = []
    for item in candidates:
        key = (item["code"], item["route"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[: int(thresholds["max_notifications_per_run"])]


# ---------------------------------------------------------------------------
# In-app storage
# ---------------------------------------------------------------------------


def read_notifications(path: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "id" in record:
                items.append(record)
    except OSError:
        return []
    items.reverse()
    return items[: max(int(limit), 1)]


def append_notifications(
    path: str | Path,
    items: list[dict[str, Any]],
    *,
    window_hours: float = 24.0,
    now: dt.datetime | None = None,
    max_items: int | None = None,
) -> dict[str, int]:
    """Append new notifications, skipping ones already raised inside the window."""
    file_path = Path(path)
    timestamp = now or dt.datetime.now(dt.timezone.utc)
    cutoff = timestamp - dt.timedelta(hours=window_hours)
    existing = read_notifications(file_path, limit=500)
    recent_keys = set()
    for record in existing:
        try:
            created = dt.datetime.fromisoformat(str(record.get("created_at")))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        if created >= cutoff:
            recent_keys.add((record.get("code"), record.get("route")))
    added: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        key = (item.get("code"), item.get("route"))
        if key in recent_keys:
            skipped += 1
            continue
        recent_keys.add(key)
        added.append(item)
    if not added:
        return {"added": 0, "skipped": skipped}
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        for item in added:
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    if max_items:
        _trim(file_path, max_items)
    return {"added": len(added), "skipped": skipped}


def _trim(path: Path, max_items: int) -> None:
    records = read_notifications(path, limit=100_000)
    if len(records) <= max_items:
        return
    kept = list(reversed(records[:max_items]))
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in kept), encoding="utf-8")
    temporary.replace(path)


def acknowledge_notification(path: str | Path, notification_id: str) -> bool:
    file_path = Path(path)
    records = list(reversed(read_notifications(file_path, limit=100_000)))
    found = False
    for record in records:
        if record.get("id") == notification_id:
            record["acknowledged"] = True
            record["acknowledged_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            found = True
    if not found:
        return False
    temporary = file_path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records), encoding="utf-8")
    temporary.replace(file_path)
    return True


def summarize(notifications: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for item in notifications:
        severity = item.get("severity")
        if severity in counts:
            counts[severity] += 1
    counts["total"] = len(notifications)
    counts["unacknowledged"] = sum(1 for item in notifications if not item.get("acknowledged"))
    return counts


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def email_message(notification: dict[str, Any], policy: dict[str, Any]) -> EmailMessage:
    config = policy["email"]
    message = EmailMessage()
    message["Subject"] = f"[{str(notification['severity']).upper()}] Bus demand alert: {notification['title']}"
    message["From"] = config["sender"] or config["username"]
    message["To"] = ", ".join(config["recipients"])
    body = [
        notification["message"],
        "",
        f"Severity: {notification['severity']}",
        f"Code: {notification['code']}",
        f"Route: {notification['route'] or 'all routes'}",
        f"Suggested action: {notification['action'] or 'Review the dashboard.'}",
        "",
        "Aggregate counts only. This message contains no individual passenger records.",
        "Generated by the Bus Demand Forecast prototype. Forecasts are advisory.",
    ]
    message.set_content("\n".join(body))
    return message


def webhook_payload(notification: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "bus_demand_notification",
        "id": notification["id"],
        "created_at": notification["created_at"],
        "severity": notification["severity"],
        "code": notification["code"],
        "route": notification["route"],
        "title": notification["title"],
        "message": notification["message"],
        "action": notification["action"],
        "confidentiality": "aggregate_only",
    }


def _smtp_transport(config: dict[str, Any], message: EmailMessage) -> None:
    with smtplib.SMTP(config["host"], int(config["port"]), timeout=float(config["timeout_seconds"])) as client:
        if config["use_tls"]:
            client.starttls()
        if config["username"]:
            client.login(config["username"], config["password"])
        client.send_message(message)


def _http_email_transport(config: dict[str, Any], message: EmailMessage) -> None:
    payload = json.dumps(
        {
            "from": message["From"],
            "to": message["To"],
            "subject": message["Subject"],
            "text": message.get_content(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        config["url"],
        data=payload,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {config['token']}"} if config["token"] else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(config["timeout_seconds"])):
        return None


def _webhook_transport(config: dict[str, Any], payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        config["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"X-Webhook-Secret": config["secret"]} if config["secret"] else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(config["timeout_seconds"])):
        return None


Transport = Callable[[dict[str, Any], Any], None]


def deliver(
    notifications: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    transports: dict[str, Transport] | None = None,
) -> dict[str, Any]:
    """Send notifications through the enabled channels.

    ``transports`` allows tests (and alternate deployments) to inject senders
    for ``email`` and ``webhook``. Failures are collected, never raised.
    """
    senders: dict[str, Transport] = dict(transports or {})
    email_config = policy["email"]
    webhook_config = policy["webhook"]
    if "email" not in senders:
        senders["email"] = _http_email_transport if email_config["provider"] == "http" else _smtp_transport
    if "webhook" not in senders:
        senders["webhook"] = _webhook_transport

    result: dict[str, Any] = {
        "in_app": len(notifications) if policy["in_app"]["enabled"] else 0,
        "email": {"enabled": bool(email_config["enabled"]), "sent": 0, "failed": 0, "errors": []},
        "webhook": {"enabled": bool(webhook_config["enabled"]), "sent": 0, "failed": 0, "errors": []},
    }

    if email_config["enabled"]:
        if not email_config["recipients"]:
            result["email"]["errors"].append("No email recipients are configured")
        else:
            for notification in notifications:
                try:
                    senders["email"](email_config, email_message(notification, policy))
                    result["email"]["sent"] += 1
                    notification.setdefault("delivered", []).append("email")
                except Exception as exc:  # network and provider errors must not break the run
                    result["email"]["failed"] += 1
                    result["email"]["errors"].append(f"{type(exc).__name__}: {exc}")

    if webhook_config["enabled"]:
        if not webhook_config["url"]:
            result["webhook"]["errors"].append("No webhook URL is configured")
        else:
            for notification in notifications:
                try:
                    senders["webhook"](webhook_config, webhook_payload(notification))
                    result["webhook"]["sent"] += 1
                    notification.setdefault("delivered", []).append("webhook")
                except Exception as exc:
                    result["webhook"]["failed"] += 1
                    result["webhook"]["errors"].append(f"{type(exc).__name__}: {exc}")

    if policy["in_app"]["enabled"]:
        for notification in notifications:
            notification.setdefault("delivered", []).append("in_app")
    return result
