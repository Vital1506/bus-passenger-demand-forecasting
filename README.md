# Bus Passenger Demand Forecasting

**[Open the live website](https://bus-demand-forecast-4jmb.onrender.com/)** · [API health](https://bus-demand-forecast-4jmb.onrender.com/api/health)

The live site opens in a **public, read-only synthetic demo**. It does not require a key and cannot accept uploads or train models. Operator features require an authorized bearer key in Settings. Render's free service can sleep while idle, so allow a short cold start on first visit. Demo records and forecasts are synthetic and are not evidence of real-world accuracy.

An operationally oriented Python baseline for **daily passenger boardings per route**. The workflow validates input coverage, uses time-based rolling-origin backtests, compares a machine-learning model with a seasonal-naive baseline, saves evaluation artifacts, and produces recursive forecasts with approximate historical error bands.

This is a strong starting implementation, not a claim of production readiness or guaranteed accuracy. Company data, service calendars, operational definitions, and a shadow evaluation are required before use in planning.

## Reproducible model training and evaluation

The [Train forecasting model workflow](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/train-model.yml) runs on relevant source changes and can also be started manually from GitHub Actions. Choose **MTA** for a public historical dataset (default route: M15) or **sample** for the clearly labeled synthetic demo. Each run saves the trained model, data/source metadata, validation metrics, forecast-error analysis, and predictions as a downloadable artifact retained for 30 days.

### Public MTA data

The workflow uses the official [MTA Bus Hourly Ridership: 2020–2024 dataset](https://data.ny.gov/Transportation/MTA-Bus-Hourly-Ridership-2020-2024/kv7t-n8in). The downloader [src/fetch_mta_daily.py](src/fetch_mta_daily.py) sums hourly estimates across fare/payment categories by route and local calendar date. The source covers historical records from 2022 through 2024 and is not a live feed. The default portfolio run selects one route, M15; other MTA route IDs can be entered when manually starting the workflow.

Dates with no published route rows are explicitly zero-filled for this experiment to represent zero recorded boardings. That can also coincide with a day without service, so verify against a service calendar before interpreting the values operationally. The source does not provide evidence for how this prototype would perform on another operator, route, or future period.

Run the same fetch and train flow locally:

```powershell
python -m src.fetch_mta_daily --routes M15 --output artifacts/mta_daily_ridership.csv
python -m src.train --data artifacts/mta_daily_ridership.csv --missing-days zero --horizon 7 --folds 4 --holdout-days 28
```

### Time-based validation and error review

The workflow uses four expanding-window model-selection folds of seven days each, followed by a final 28-day walk-forward holdout. It scores the holdout in four successive seven-day blocks; actual counts from a block are added to training history only after that block is forecast. The report compares the ML model with a seasonal-naive benchmark and breaks down WAPE by route and forecast lead day. It also lists the five largest holdout errors for review. The downloadable artifact contains both the model-selection predictions and the final holdout predictions, so the results can be independently inspected.

The earlier [synthetic demonstration run](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/runs/42828632328) measured 11.21% model WAPE versus 14.19% for seasonal naive; those numbers describe only generated sample data. The latest [MTA M15 run](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/runs/42832698798) fetched 1,065 of 1,065 calendar dates in the selected 2022–2024 period. Its model-selection WAPE was 10.15% versus 10.17% for seasonal naive. On the final 28-day walk-forward holdout, WAPE was 11.92% versus 18.10% for the baseline. This is evidence for this one historical MTA route and evaluation setup only; it does not establish performance on other routes, operators, current data, or future periods. Download the run's **bus-demand-model-and-backtest** artifact to inspect the input data, fitted model, fold predictions, holdout predictions, and error report.

## Operator workspace

The web app now opens with a branded opening screen that waits for the forecast service before entering the dashboard and has dedicated **Overview**, **Forecasts**, **Routes**, **Data**, and **Model** pages. Route summaries aggregate the forecast window by route, the model page compares WAPE with the seasonal-naive baseline, and the data page shows source coverage and forecast freshness.

The dashboard checks its API every 60 seconds while the page is open. Forecasts update after new records are ingested and the model/forecast is refreshed. A live ticketing or vehicle telemetry feed is **not connected by default**; configure an approved data integration and update schedule before treating the app as real-time operations software.

## Interface design

The dashboard uses a persistent desktop navigation rail, compact mobile tabs, clear headings, carefully spaced forecast surfaces, responsive route tables, and visible keyboard focus. Its restrained teal and warm-neutral visual system uses color for status and emphasis without making color the only signal. The visual system is informed by [Material Design 3](https://m3.material.io/) and [Carbon's accessible data visualization palettes](https://carbondesignsystem.com/data-visualization/color-palettes/). Status labels accompany their colors, and sample data is clearly identified.

## App clients

The repository now includes a small operator dashboard backed by the existing Python forecasting engine:

- **Windows desktop:** `desktop_launcher.py` starts a local API and opens the dashboard in a native WebView window. Daily counts and model artifacts are stored under the signed-in Windows user's local application data folder. By default, the service binds only to `127.0.0.1`.
- **Android:** the responsive dashboard is an installable Progressive Web App (PWA). Serve the API and dashboard on an approved HTTPS host, open that URL in Chrome on Android, and choose **Install app** or **Add to Home screen**. It is not currently a Play Store APK.
- **Shared service:** both clients can use the same API address. Open **Settings** in the app to enter the service URL and, on a protected host, the bearer access key. Hosting and a persistent data volume are required for cross-device shared state.

“Real time” currently means the API refreshes a forecast when new daily counts arrive; the browser checks API status every 60 seconds. An authenticated JSON ingestion endpoint supports scheduled daily-count exports, but a provider-specific ticketing/AVL adapter and schedule still need to be configured. This is not live vehicle telemetry.

### Try the dashboard locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-desktop.txt
python desktop_launcher.py
```

The live website shows a read-only synthetic forecast without a key. Visitors can choose 3, 7, 14, or 28 days, filter the chart and table to one route, and download a matching CSV. These controls reuse the cached synthetic model and never write to operator data. It is an example, not a forecast for the visitor's town or route. An authorized operator key unlocks the editable workspace. Operators can enter a daily passenger count manually using a route's origin, destination, service date, and observed total boardings, so they do not need to prepare a file. Use consistent place names for a route. Counts are safely upserted by route and date; missing dates are flagged by default. Select zero-fill only when the operator confirms the service did not run or demand was zero. CSV import is available under **Have a file instead?** A timetable with `From` and `To` can be used only as a clearly labelled synthetic scenario derived from route frequency, not measured ridership. After observations arrive, an existing model refreshes the forecast; otherwise choose **Train my forecast**. For the default four-fold, seven-day model-selection backtest plus 28-day holdout, training requires at least 118 continuous calendar dates per route. One or a few manual entries do not create a meaningful forecast. Place names, public maps, and schedules alone do not tell the model how many passengers actually boarded. Use the synthetic demo until enough actual counts are available.

### Simple operator tutorial

1. Open **Start here**. Without an operator key, explore the read-only synthetic sample. Do not interpret it as local demand.
2. With an authorized key, record the origin, destination, date, and verified total boardings for that route-day. Enter counts as you collect them; a CSV is optional.
3. Keep the same route names and record daily observations. Missing dates are flagged unless you explicitly choose zero for a confirmed no-service/zero-demand day.
4. After at least 118 continuous dates per route are available, choose **Train my forecast** and a forecast length. Review the model's time-based backtest beside the simple weekday baseline.
5. Open **Forecasts** to review route/day estimates and approximate ranges. Compare later predictions with actual counts before using them to plan service.

This website forecasts **daily route boardings**; it does not predict an individual passenger, exact stop-to-stop load, or live crowding. The violet animated route graphic is decorative and does not represent real-time vehicle positions.

### Ingest from a scheduled daily export

A company-managed scheduler or adapter can post validated daily counts to the authenticated `POST /api/data/ingest` endpoint:

```json
{
  "source_system": "ticketing_daily_export",
  "missing_days": "error",
  "records": [
    {"service_date": "2026-11-09", "route_id": "R10", "passengers": 1240},
    {"service_date": "2026-11-09", "route_id": "R22", "passengers": 980}
  ]
}
```

Send the payload with `Content-Type: application/json` and `Authorization: Bearer <operator-key>`. Each request accepts up to 5,000 route-day records. A retry safely replaces matching route/date rows; duplicate keys inside one batch are rejected. Missing dates are flagged by default. Use `"missing_days": "zero"` only when the operator confirms those gaps mean zero service or zero demand. Ingestion refreshes forecasts with the existing model; it does not automatically retrain. Schedule retraining and a backtest review separately. The endpoint is an integration contract, not a plug-and-play connection to a vendor feed.

The data page shows source update time and a small aggregate activity log. It stores event counts and source labels, not passenger rows. Local app data and this log are stored under the app data folder. The public Render demo uses temporary storage, so its data and activity history can disappear after a restart. Use persistent, access-controlled, backed-up storage before company operations.

### Get the Windows app

The **Build Windows desktop app** GitHub Actions workflow packages a Windows build after relevant changes land on `main`. Open the repository's Actions tab, select the latest successful **Build Windows desktop app** run, and download its `BusDemandForecast-Windows` artifact. Extract the ZIP and run `BusDemandForecast.exe` from the included folder. The workflow artifact expires after 30 days; rebuild or retain it according to your release process.

**First launch on Windows:** This downloadable build is not code-signed, so SmartScreen may say “Windows protected your PC” because it cannot verify the publisher. Only use the ZIP downloaded from this repository’s [Windows build workflow](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/build-app.yml). In the ZIP’s **Properties**, select **Unblock** if Windows shows that option, apply it, and then extract the folder. If SmartScreen still appears, choose **More info** → **Run anyway** only after verifying the source and workflow. Managed company devices may block unsigned apps by policy. Removing the warning for all users requires an organization-owned, trusted Authenticode signing certificate; do not disable Defender or other security settings.

### Host for Android and shared use

The repository includes a Docker image for an approved Linux host. It requires a bearer key and should be served behind HTTPS. Mount persistent storage at `/var/lib/bus-forecast`; otherwise uploads and model files can be lost when the container is replaced.

#### Deploy a public demo website

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2FVital1506%2Fbus-passenger-demand-forecasting)

The included `render.yaml` configures the API and dashboard as one HTTPS website and generates a bearer key during deployment. Open the [live website](https://bus-demand-forecast-4jmb.onrender.com/) to see the public read-only synthetic preview. The public preview cannot upload company data or trigger model training. To use operator features, copy `BUS_FORECAST_API_KEY` from the Render environment settings and save it in **Settings**.

This free setup is for demonstration only: it can sleep when idle and its filesystem is temporary, so uploaded data and trained models can be lost after a restart or redeploy. Do not upload company data to this demo. For operational use, choose an approved paid host with persistent storage, HTTPS, access controls, backups, and company approval before importing data.

```bash
docker build -t bus-demand-forecast .
docker run --rm -p 8000:8000 \
  -e BUS_FORECAST_API_KEY='replace-with-a-long-random-secret' \
  -v bus-demand-data:/var/lib/bus-forecast \
  bus-demand-forecast
```

Configure the host's HTTPS domain and Android-device access with your infrastructure administrator before using operational data. If the Windows desktop client will connect to that hosted API, configure `BUS_FORECAST_CORS_ORIGINS` on the API for the desktop origins `http://127.0.0.1:8765` and `http://localhost:8765`. The hosted mode's shared bearer key is a prototype access gate, not company SSO or role-based authorization. Use only approved infrastructure and data until authentication, retention, backups, access controls, privacy review, and operational sign-off are complete.

## Current stage: prototype

Use the live synthetic demo to show the dashboard, forecasts, route scenarios, and exports and to gather feedback. It is designed for evaluation with generated sample counts; company data and a paid hosting upgrade are not part of this phase.

## Later phase: company pilot (after approval)

The separate [company pilot runbook](COMPANY_PILOT.md) defines data, access, forecast-acceptance, shadow-operation, backup/restore, and rollback gates. A separate [paid Render pilot blueprint](render.company.yaml) provides persistent storage at `/var/lib/bus-forecast`; it does not change the free demo configuration. This is a later phase. The pilot blueprint is optional and incurs hosting charges if created; do not create it during prototype review. Revisit the runbook only after your organization approves a company-data pilot.

The pilot blueprint is still a single-instance deployment with a shared bearer-key gate. It does not provide SSO, user roles, high availability, or an automated backup/restore service. Do not load company data until the company approves the access model and hosting environment and the pilot gates are signed off. Persistent storage alone is not a backup or enterprise security control.

## Forecast contract

- Grain: one row per route and calendar service date.
- Target: nonnegative passenger boarding count (`passengers`).
- Default forecast horizon: 7 days.
- Inputs: recent demand lags, trailing means/variation, day-of-week, weekend, month, and annual seasonality.
- Model: scikit-learn histogram gradient boosting with one-hot route encoding.
- Model-selection backtest: expanding-window, rolling-origin evaluation at the same multi-day horizon used for forecasting (4 folds × 7 days by default), followed by a separate 28-day walk-forward holdout in the training workflow.
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

This repository includes a generic authenticated daily-count ingestion endpoint and a prototype route-planning review signal. It does not include a vendor-specific connector, scheduler, live telemetry adapter, service-calendar logic, monitoring service, capacity optimizer, durable audit archive, SSO/roles, or privacy/security review. Those depend on company systems and requirements. No company data was provided; do not treat sample metrics as evidence of real-world performance.

