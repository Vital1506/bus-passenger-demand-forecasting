# Bus Passenger Demand Forecasting

An operationally oriented Python baseline for **daily passenger boardings per route**. The workflow validates input coverage, uses time-based rolling-origin backtests, compares a machine-learning model with a seasonal-naive baseline, saves evaluation artifacts, and produces recursive forecasts with approximate historical error bands.

This is a strong starting implementation, not a claim of production readiness or guaranteed accuracy. Company data, service calendars, operational definitions, and a shadow evaluation are required before use in planning.

## App clients

The repository now includes a small operator dashboard backed by the existing Python forecasting engine:

- **Windows desktop:** `desktop_launcher.py` starts a local API and opens the dashboard in a native WebView window. Daily counts and model artifacts are stored under the signed-in Windows user's local application data folder. By default, the service binds only to `127.0.0.1`.
- **Android:** the responsive dashboard is an installable Progressive Web App (PWA). Serve the API and dashboard on an approved HTTPS host, open that URL in Chrome on Android, and choose **Install app** or **Add to Home screen**. It is not currently a Play Store APK.
- **Shared service:** both clients can use the same API address. Open **Settings** in the app to enter the service URL and, on a protected host, the bearer access key. Hosting and a persistent data volume are required for cross-device shared state.

“Real time” here means a forecast is refreshed when new daily counts are uploaded. It is not a live vehicle or ticketing feed. The company data connector, service calendar, SSO/roles, monitoring, and production sign-off still depend on the operator's systems and policies.

### Try the dashboard locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-desktop.txt
python desktop_launcher.py
```

In the dashboard, select **Try the synthetic demo** for a complete walkthrough or upload a CSV with `service_date`, `route_id`, and `passengers`. The demo data is synthetic and its scores are not evidence of company model performance. New uploads are checked for invalid rows and missing route dates. After data arrives, an existing model refreshes the forecast; otherwise choose **Train / retrain model**. The first training run requires at least 90 continuous service dates per route for the default four-fold, seven-day backtest.

### Get the Windows app

The **Build Windows desktop app** GitHub Actions workflow packages a Windows build after relevant changes land on `main`. Open the repository's Actions tab, select the latest successful **Build Windows desktop app** run, and download its `BusDemandForecast-Windows` artifact. Extract the ZIP and run `BusDemandForecast.exe` from the included folder. The workflow artifact expires after 30 days; rebuild or retain it according to your release process.

**First launch on Windows:** This downloadable build is not code-signed, so SmartScreen may say “Windows protected your PC” because it cannot verify the publisher. Only use the ZIP downloaded from this repository’s [Windows build workflow](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/build-app.yml). In the ZIP’s **Properties**, select **Unblock** if Windows shows that option, apply it, and then extract the folder. If SmartScreen still appears, choose **More info** → **Run anyway** only after verifying the source and workflow. Managed company devices may block unsigned apps by policy. Removing the warning for all users requires an organization-owned, trusted Authenticode signing certificate; do not disable Defender or other security settings.

### Host for Android and shared use

The repository includes a Docker image for an approved Linux host. It requires a bearer key and should be served behind HTTPS. Mount persistent storage at `/var/lib/bus-forecast`; otherwise uploads and model files can be lost when the container is replaced.

#### Deploy a public demo website

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FVital1506%2Fbus-passenger-demand-forecasting)

The included `render.yaml` configures the API and dashboard as one HTTPS website and generates a bearer key during deployment. After the service is live, copy `BUS_FORECAST_API_KEY` from its Render environment settings, open the site’s **Settings**, and save that key to connect the dashboard. Use **Try the synthetic demo** to verify the forecasting workflow.

This free setup is for demonstration only: it can sleep when idle and its filesystem is temporary, so uploaded data and trained models can be lost after a restart or redeploy. Do not upload company data to this demo. For operational use, choose an approved paid host with persistent storage, HTTPS, access controls, backups, and company approval before importing data.

```bash
docker build -t bus-demand-forecast .
docker run --rm -p 8000:8000 \
  -e BUS_FORECAST_API_KEY='replace-with-a-long-random-secret' \
  -v bus-demand-data:/var/lib/bus-forecast \
  bus-demand-forecast
```

Configure the host's HTTPS domain and Android-device access with your infrastructure administrator before using operational data. If the Windows desktop client will connect to that hosted API, configure `BUS_FORECAST_CORS_ORIGINS` on the API for the desktop origins `http://127.0.0.1:8765` and `http://localhost:8765`. The hosted mode's shared bearer key is a prototype access gate, not company SSO or role-based authorization. Use only approved infrastructure and data until authentication, retention, backups, access controls, privacy review, and operational sign-off are complete.

## Forecast contract

- Grain: one row per route and calendar service date.
- Target: nonnegative passenger boarding count (`passengers`).
- Default forecast horizon: 7 days.
- Inputs: recent demand lags, trailing means/variation, day-of-week, weekend, month, and annual seasonality.
- Model: scikit-learn histogram gradient boosting with one-hot route encoding.
- Backtest: expanding-window, rolling-origin evaluation at the same multi-day horizon used for forecasting (4 folds × 7 days by default).
- Comparator: seasonal naive, repeating each route's count from the same weekday one week earlier.
- Metrics: MAE, RMSE, and WAPE overall and by route.
- Forecast ranges: route-specific 90th-percentile absolute out-of-fold error, displayed as a symmetric approximate band. This is not a formally calibrated prediction interval.

Every model forecast is recursive: predictions earlier in the requested horizon feed lag features for later dates. Historical validation follows that same process. This avoids evaluating a one-step model as if it were a multi-day forecasting system.

## Setup

Python 3.10 or newer is required.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Data format

CSV required columns:

| Column | Meaning |
|---|---|
| `service_date` | Local service date in `YYYY-MM-DD` format |
| `route_id` | Stable route identifier |
| `passengers` | Nonnegative passenger boardings for that route and date |

Use one row per route/date; aggregate at the source or resolve duplicate rows. Counts must be finite and nonnegative. Dates need to be continuous for each route through the latest system date. Missing dates fail by default because an absent record may mean missing data, no service, or zero demand. Choose `--missing-days zero` only after operations confirms that interpretation. Canceled service should ideally be represented by an explicit service calendar instead of silently equated to zero demand.

The training workflow currently consumes daily counts only. Holidays, school terms, scheduled service, disruptions, weather, and major events are valuable future-known signals but are not yet features. They should be integrated only with an as-of-time rule so historical training does not see information unavailable when the forecast would have been made.

## Run the checks, backtest, and forecast

The included sample is synthetic and only demonstrates the workflow.

```powershell
python -m unittest discover -s tests
python -m src.profile_data --data data/sample_daily_ridership.csv
python -m src.train --data data/sample_daily_ridership.csv --horizon 7 --folds 4
python -m src.forecast --data data/sample_daily_ridership.csv --model artifacts/model.joblib --days 7 --output outputs/forecast.csv
```

The unit checks cover fail-closed missing-date handling, explicit zero filling, lag leakage, and recursive seasonal-naive behavior. Run them in the same Python environment used for training.

`train` writes:

- `artifacts/model.joblib`: fitted model, training cutoff, selected horizon, route list, calibration bands, and backtest metrics.
- `artifacts/backtest_metrics.json`: overall and route-level comparisons against the seasonal-naive baseline.
- `artifacts/backtest_predictions.csv`: each out-of-fold actual, model prediction, baseline prediction, and fold.

`forecast` writes `service_date`, `route_id`, `predicted_passengers`, `lower_90_approx`, and `upper_90_approx`. It rejects data older than the model cutoff, previously unseen routes, excessive requested horizons, and discontinuous history unless the zero-fill policy is explicitly chosen. Train with a longer `--horizon` before requesting a longer forecast.

## Interpretation and deployment path

1. Confirm boardings vs. alightings, service-day boundary/time zone, route variants, and route ID changes.
2. Reconcile passenger counts to the source system and document how APC/manual corrections are handled.
3. Review data gaps, canceled-service dates, route starts/ends, and abnormal counts before training.
4. Run the rolling backtest and inspect every route. The model should beat the baseline consistently enough for the business use case; investigate any regression.
5. Keep a final untouched time period for sign-off. The rolling folds are for model selection, so they should not be the sole final acceptance evidence.
6. Run in shadow mode and save each forecast, input cutoff, model version, actual outcome, and error. Agree thresholds, fallback behavior, ownership, and retraining cadence with operations.
7. Add service schedules, holidays, weather forecasts, school calendars, events, and disruptions as versioned, forecast-time-available features. Evaluate capacity planning needs separately from average demand forecasting.

This repository does not include a data connector, production scheduler/API, monitoring service, service-calendar logic, capacity optimizer, or privacy/security review. Those depend on company systems and requirements. No company data was provided; do not treat sample metrics as evidence of real-world performance.
