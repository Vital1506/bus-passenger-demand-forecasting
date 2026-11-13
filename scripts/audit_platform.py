"""Comprehensive system audit and validation script for Smart Bus Demand Intelligence platform."""

import json
import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app import app
from src.db.models import Base
from src.ai_assistant import get_ai_status, extract_system_context, format_system_prompt
from src.modeling import build_model, recursive_forecast, seasonal_naive_forecast
from src.train import rolling_backtest, metrics as calculate_metrics
from src.forecasting import load_data, complete_daily_series
from src.forecast import build_forecast
from src.occupancy_engine import compute_trip_occupancy, compute_route_capacity_profile
from src.service_planner import run_service_simulation, SimulationScenario
from src.db.session import init_db
from src.db.models import StopPassengerObservation, VehicleRecord
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from datetime import datetime, timezone


def audit_routes():
    print("\n--- 1. AUDITING BACKEND REST APIS ---")
    routes = []
    for r in app.routes:
        methods = sorted(list(getattr(r, "methods", [])))
        path = getattr(r, "path", "")
        routes.append((methods, path))
    print(f"Total registered FastAPI routes: {len(routes)}")
    
    # Verify critical routes exist
    critical_paths = [
        "/health",
        "/api/ready",
        "/api/gtfs/routes",
        "/api/forecast",
        "/api/forecast.csv",
        "/api/ai/status",
        "/api/ai/chat",
        "/api/gtfs/upload",
        "/api/realtime/vehicles",
        "/api/operations/alerts",
        "/api/operations/dispatch-recommendations",
        "/api/simulator/evaluate",
        "/api/occupancy/trip/{trip_id}",
        "/api/reports/daily",
        "/api/auth/roles",
    ]
    missing = []
    for cp in critical_paths:
        found = any(cp == path for _, path in routes)
        if not found:
            missing.append(cp)
    
    if missing:
        print(f"FAILED: Missing critical endpoints: {missing}")
        return False
    print("PASSED: All critical REST API routes verified.")
    return True


def audit_database_schema():
    print("\n--- 2. AUDITING DATABASE MODELS & MIGRATIONS ---")
    tables = Base.metadata.tables
    print(f"Total relational tables declared: {len(tables)}")
    table_names = sorted(tables.keys())
    print("Tables:", ", ".join(table_names))
    
    expected_tables = {
        "agencies",
        "gtfs_routes",
        "gtfs_stops",
        "gtfs_trips",
        "gtfs_stop_times",
        "gtfs_calendar",
        "gtfs_calendar_dates",
        "vehicles",
        "stop_passenger_observations",
        "operational_alert_records",
        "data_audit_events",
    }
    missing = expected_tables - set(table_names)
    if missing:
        print(f"FAILED: Missing database tables: {missing}")
        return False
    print("PASSED: Database tables match enterprise multi-agency schema.")
    return True


def audit_machine_learning():
    print("\n--- 3. AUDITING MACHINE LEARNING FORECASTING PIPELINE ---")
    data_path = Path("data/sample_daily_ridership.csv")
    if not data_path.exists():
        print("FAILED: Sample ridership data missing.")
        return False

    df = load_data(str(data_path))
    dense = complete_daily_series(df, missing_days="error")
    routes = dense["route_id"].unique()
    print(f"Loaded {len(dense)} records across {len(routes)} routes: {list(routes)}")
    
    # Run walk-forward backtest
    print("Running time-based walk-forward backtest...")
    preds, model_metrics, baseline_metrics = rolling_backtest(dense, horizon=7, folds=3)
    wape = model_metrics["wape"]
    base_wape = baseline_metrics["wape"]
    print(f"Model WAPE: {wape:.2%}, Baseline WAPE: {base_wape:.2%}")
    print(f"Model beats baseline: {wape <= base_wape}")
    
    # Train model on full data and generate multi-day forecast
    from src.forecasting import add_features, FEATURE_COLUMNS
    features = add_features(dense).dropna()
    X = features[FEATURE_COLUMNS]
    y = features["passengers"]
    model_obj = build_model()
    model_obj.fit(X, y)
    forecast_df = recursive_forecast(model_obj, dense, 7)
    
    # Verify predictions are nonnegative and have no NaN
    if forecast_df["predicted_passengers"].isna().any():
        print("FAILED: Forecast contains NaN values.")
        return False
    if (forecast_df["predicted_passengers"] < 0).any():
        print("FAILED: Forecast contains negative passenger values.")
        return False
        
    print(f"PASSED: Generated {len(forecast_df)} forecast records with nonnegative values and valid bounds.")
    return True


def audit_occupancy_and_simulation():
    print("\n--- 4. AUDITING OCCUPANCY & SERVICE PLANNER ENGINES ---")
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        v = VehicleRecord(
            agency_id="CTA",
            vehicle_id="BUS-101",
            seated_capacity=35,
            standing_capacity=15,
            total_capacity=50,
            is_active=True,
        )
        session.add(v)
        now = datetime.now(timezone.utc)
        stops = [
            StopPassengerObservation(
                agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S1", stop_sequence=1,
                boardings=20, alightings=0, vehicle_id="BUS-101", observation_timestamp_utc=now,
            ),
            StopPassengerObservation(
                agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S2", stop_sequence=2,
                boardings=15, alightings=5, vehicle_id="BUS-101", observation_timestamp_utc=now,
            ),
            StopPassengerObservation(
                agency_id="CTA", route_id="R1", trip_id="TRIP-1", stop_id="S3", stop_sequence=3,
                boardings=10, alightings=15, vehicle_id="BUS-101", observation_timestamp_utc=now,
            ),
        ]
        session.add_all(stops)
        session.commit()
        
        prof = compute_trip_occupancy(session, trip_id="TRIP-1", agency_id="CTA")
        assert prof is not None, "Profile should not be None"
        loads = [s.onboard_load for s in prof.stops]
        assert loads == [20, 30, 25], f"Expected loads [20, 30, 25], got {loads}"
        print(f"Stop-level passenger conservation verified: {loads}")
    
    # Test headway simulation
    scenario = SimulationScenario(
        route_id="R10",
        forecast_hourly_demand=280.0,
        baseline_trips_per_hour=4.0,
        modified_trips_per_hour=6.0,
        vehicle_capacity=55,
        operating_hours=16,
    )
    sim = run_service_simulation(scenario)
    assert sim.crowding_risk_baseline == "CRITICAL"
    assert sim.crowding_risk_modified == "MODERATE"
    assert sim.net_capacity_change_per_hour == 110
    print("PASSED: Occupancy engine and service planner simulator verified.")
    return True


def audit_ai_assistant():
    print("\n--- 5. AUDITING OPENROUTER AI ASSISTANT ---")
    status = get_ai_status()
    print(f"AI Assistant Status: configured={status['configured']}, model={status['model']}, provider={status['provider']}")
    
    ctx = extract_system_context(data_dir="data")
    prompt = format_system_prompt(ctx)
    assert "TRANSIT SYSTEM CONTEXT" in prompt
    assert "GROUNDING RULE" in prompt
    assert "DO NOT INVENT" in prompt
    print("PASSED: Grounding prompt, telemetry extractor, and configuration status verified.")
    return True


def audit_security():
    print("\n--- 6. AUDITING SECURITY & HYGIENE ---")
    gitignore = Path(".gitignore").read_text()
    assert ".env" in gitignore, ".env must be ignored in .gitignore"
    assert "*.db" in gitignore, "*.db must be ignored in .gitignore"
    
    import re
    secret_pattern = re.compile(r"sk-(?:or-v1|proj)-[a-zA-Z0-9]{20,}")
    for p in Path(".").glob("**/*.py"):
        if ".venv" in str(p) or "scripts" in str(p):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        match = secret_pattern.search(text)
        if match:
            print(f"FAILED: Potential live secret found in {p}")
            return False
            
    print("PASSED: Security checks, .gitignore exclusion, and credential protection verified.")
    return True


if __name__ == "__main__":
    t0 = time.time()
    ok = True
    ok = audit_routes() and ok
    ok = audit_database_schema() and ok
    ok = audit_machine_learning() and ok
    ok = audit_occupancy_and_simulation() and ok
    ok = audit_ai_assistant() and ok
    ok = audit_security() and ok
    
    elapsed = time.time() - t0
    print(f"\nAUDIT COMPLETE in {elapsed:.2f}s: {'ALL CHECKS PASSED' if ok else 'FAILURES DETECTED'}")
    sys.exit(0 if ok else 1)
