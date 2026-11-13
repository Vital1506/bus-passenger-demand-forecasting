"""Automated data quality, distribution drift monitoring, and self-healing pipeline engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


def calculate_psi(expected: np.ndarray, actual: np.ndarray, num_bins: int = 5) -> float:
    """Calculate Population Stability Index (PSI) between baseline (expected) and current (actual) distributions."""
    if len(expected) < 10 or len(actual) < 10:
        return 0.0

    # Adapt bin count dynamically based on sample size (min 3, max num_bins)
    effective_bins = min(num_bins, max(3, len(actual) // 20))

    # Determine quantile bins based on expected distribution
    percentiles = np.linspace(0, 100, effective_bins + 1)
    bins = np.percentile(expected, percentiles)
    bins[0] = -np.inf
    bins[-1] = np.inf
    # Ensure strictly increasing bins to avoid zero-width intervals
    for i in range(1, len(bins)):
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1e-5

    expected_counts, _ = np.histogram(expected, bins=bins)
    actual_counts, _ = np.histogram(actual, bins=bins)

    ep = np.where(expected_counts == 0, 1e-4, expected_counts / len(expected))
    ap = np.where(actual_counts == 0, 1e-4, actual_counts / len(actual))

    psi_val = np.sum((ap - ep) * np.log(ap / ep))
    return float(max(0.0, psi_val))


@dataclass
class DriftReport:
    assessed_at_utc: str
    psi_score: float
    drift_level: str  # STABLE (PSI < 0.1), MODERATE (0.1 <= PSI < 0.25), SIGNIFICANT (PSI >= 0.25)
    model_degraded: bool
    retraining_recommended: bool
    self_healing_action_taken: str | None
    baseline_records_count: int
    recent_records_count: int
    outlier_spikes_detected: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessed_at_utc": self.assessed_at_utc,
            "psi_score": round(self.psi_score, 4),
            "drift_level": self.drift_level,
            "model_degraded": self.model_degraded,
            "retraining_recommended": self.retraining_recommended,
            "self_healing_action_taken": self.self_healing_action_taken,
            "baseline_records_count": self.baseline_records_count,
            "recent_records_count": self.recent_records_count,
            "outlier_spikes_detected": self.outlier_spikes_detected,
        }


class DataQualityMonitor:
    def __init__(self):
        self._audit_log: list[dict[str, Any]] = []

    def assess_drift(self, df: pd.DataFrame | None = None) -> DriftReport:
        now_iso = datetime.now(timezone.utc).isoformat()
        if df is None or len(df) < 28 or "passengers" not in df.columns:
            # Insufficient observations for multi-period drift
            report = DriftReport(
                assessed_at_utc=now_iso,
                psi_score=0.0,
                drift_level="STABLE",
                model_degraded=False,
                retraining_recommended=False,
                self_healing_action_taken=None,
                baseline_records_count=len(df) if df is not None else 0,
                recent_records_count=0,
                outlier_spikes_detected=0,
            )
            self._log_event("drift_check", report.to_dict())
            return report

        # Sort chronologically by date
        sorted_df = df.sort_values(by="service_date").copy()
        values = sorted_df["passengers"].to_numpy(dtype=float)

        # Detect outlier spikes (> 4 standard deviations from median)
        median_val = np.median(values)
        std_val = np.std(values) or 1.0
        outlier_count = int(np.sum(values > (median_val + 4.0 * std_val)))

        # Split into historical baseline (older 75%) and recent operations (latest 25%)
        split_idx = int(len(values) * 0.75)
        baseline = values[:split_idx]
        recent = values[split_idx:]

        psi = calculate_psi(baseline, recent)

        if psi >= 0.25:
            drift_level = "SIGNIFICANT"
            retrain = True
            action = "Flagged model retraining requirement; suggest weekday-average fallback until retrained"
        elif psi >= 0.10:
            drift_level = "MODERATE"
            retrain = False
            action = "Distribution shift noted; monitoring rolling backtest accuracy"
        else:
            drift_level = "STABLE"
            retrain = False
            action = None

        report = DriftReport(
            assessed_at_utc=now_iso,
            psi_score=psi,
            drift_level=drift_level,
            model_degraded=(psi >= 0.25),
            retraining_recommended=retrain,
            self_healing_action_taken=action,
            baseline_records_count=len(baseline),
            recent_records_count=len(recent),
            outlier_spikes_detected=outlier_count,
        )

        self._log_event("drift_assessment", report.to_dict())
        return report

    def _log_event(self, event_type: str, details: dict[str, Any]) -> None:
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "details": details,
        }
        self._audit_log.append(entry)
        if len(self._audit_log) > 100:
            self._audit_log.pop(0)

    def get_audit_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._audit_log[-limit:]))


_GLOBAL_QUALITY_MONITOR = DataQualityMonitor()


def get_quality_monitor() -> DataQualityMonitor:
    return _GLOBAL_QUALITY_MONITOR
