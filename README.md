<div align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:0B1F2A,45:123B46,100:137F83&height=170&section=header&text=TRANSIT%20DEMAND%20INTELLIGENCE&fontSize=30&fontColor=F4FFFF&fontAlignY=38&animation=twinkling" width="100%" alt="Animated Transit Demand Intelligence banner" />
  <br />
  <img src="web/icon.svg" width="116" height="116" alt="Bus Passenger Demand Forecasting application logo" />
  <h1>Bus Passenger Demand Forecasting</h1>
  <p><strong>Forecast route-level passenger demand. Evaluate model performance. Support better transit planning.</strong></p>
  <a href="https://readme-typing-svg.demolab.com"><img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=19&duration=2800&pause=850&color=47D2CD&center=true&vCenter=true&width=760&lines=Daily+Passenger+Demand+Forecasting;Machine+Learning+%2B+Seasonal+Naive+Benchmarking;GTFS+Network+%2B+Transit+Operations+Tools;Forecasts+with+Transparent+Backtesting" alt="Animated project capability text" /></a>
  <br /><br />
  <a href="https://bus-passenger-demand-forecasting-xgx2.onrender.com"><img src="https://img.shields.io/badge/OPEN%20LIVE%20DEMO-137F83?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Open live demo" /></a>
  <a href="https://github.com/Vital1506/bus-passenger-demand-forecasting"><img src="https://img.shields.io/badge/SOURCE%20CODE-181717?style=for-the-badge&logo=github&logoColor=white" alt="Source code" /></a>
  <br /><br />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10 plus" />
  <img src="https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/ML-scikit--learn-F7931E?style=flat-square&logo=scikitlearn&logoColor=white" alt="scikit-learn" />
  <img src="https://img.shields.io/badge/Database-SQLite%20%7C%20PostgreSQL-336791?style=flat-square&logo=postgresql&logoColor=white" alt="SQLite and PostgreSQL" />
  <img src="https://img.shields.io/badge/Deployment-Render-46E3B7?style=flat-square&logo=render&logoColor=black" alt="Render" />
  <br />
  <a href="https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/train-model.yml"><img src="https://img.shields.io/github/actions/workflow/status/Vital1506/bus-passenger-demand-forecasting/train-model.yml?branch=main&label=model%20training&logo=githubactions&style=flat-square" alt="Model training workflow status" /></a>
  <a href="https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/python-package-conda.yml"><img src="https://img.shields.io/github/actions/workflow/status/Vital1506/bus-passenger-demand-forecasting/python-package-conda.yml?branch=main&label=python%20checks&logo=githubactions&style=flat-square" alt="Python workflow status" /></a>
</div>

---

## Overview

**Bus Passenger Demand Forecasting** is a Python-powered transit analytics application for estimating **daily passenger boardings for each bus route**. It combines a machine-learning forecasting pipeline with a seasonal-naive benchmark, time-based backtesting, route-level analysis, data-quality checks, and a responsive operator dashboard.

The repository also contains transit-network and operations modules for GTFS schedules, optional GTFS-Realtime integrations, occupancy analysis, service-planning scenarios, operational alerts, reports, and an AI assistant. These capabilities depend on the relevant datasets, provider configuration, and authorized access; they should not be interpreted as proof that a live transit operator is connected.

> **Live demo:** [bus-passenger-demand-forecasting-xgx2.onrender.com](https://bus-passenger-demand-forecasting-xgx2.onrender.com)  
> The public demo is a **read-only synthetic demonstration**. Its sample passenger records and forecasts are generated example data, not real ridership measurements or predictions for a visitor's city. The public demo does not accept company uploads or allow public model training.

## Contents

- [Project goals](#project-goals)
- [Main capabilities](#main-capabilities)
- [System architecture](#system-architecture)
- [Forecasting methodology](#forecasting-methodology)
- [Model evaluation](#model-evaluation)
- [Data format and validation](#data-format-and-validation)
- [Transit network and GTFS](#transit-network-and-gtfs)
- [GTFS-Realtime and operations](#gtfs-realtime-and-operations)
- [AI transit assistant](#ai-transit-assistant)
- [API reference](#api-reference)
- [Technology stack](#technology-stack)
- [Run locally](#run-locally)
- [Configuration](#configuration)
- [Desktop and mobile app](#desktop-and-mobile-app)
- [Deployment](#deployment)
- [Tests and model training](#tests-and-model-training)
- [Limitations and responsible use](#limitations-and-responsible-use)

## Project goals

The application is designed to help a transit analyst or operator explore questions such as:

- How many passengers may board each route over the next several days?
- Does the machine-learning model outperform a simple weekly seasonal baseline?
- Which routes contribute most to the forecast total?
- Is the input dataset complete, current, and consistent enough to train on?
- What do route schedules and stop sequences look like when a GTFS feed is available?
- What operational risks or capacity scenarios should be reviewed by a human planner?

The forecasting target is **daily route boardings**. It is not an individual passenger prediction, a verified stop-to-stop load forecast, or a live GPS tracking product by itself.

## Main capabilities

| Area | What the project provides |
|---|---|
| Demand forecasting | Recursive multi-day forecasts for daily boardings, with a default seven-day horizon |
| Model comparison | Machine-learning evaluation against a seasonal-naive weekly baseline |
| Backtesting | Expanding-window rolling-origin validation and a separate walk-forward holdout |
| Data quality | Required-column checks, date validation, duplicate detection, finite/non-negative count checks, and missing-date policy |
| Route analytics | Route summaries, forecast tables, filters, visualizations, and CSV export in the dashboard |
| GTFS Schedule | Import and explore agencies, routes, stops, trips, stop times, calendars, and optional shapes |
| GTFS-Realtime | Provider/file ingestion paths for vehicle positions, trip updates, and service alerts when configured |
| Operations | Occupancy calculations, service-planning scenarios, operational alerts, and dispatch recommendations |
| Data monitoring | Drift status, audit events, source freshness, and database/storage health checks |
| AI assistant | Optional OpenRouter-backed chat grounded in available local forecast and transit context |
| App clients | Browser dashboard, installable Android PWA, and a Windows desktop launcher/build workflow |

## System architecture

The application separates the web interface, API, data persistence, and forecasting logic. Optional transit feeds and AI services connect through their own integration paths.

```mermaid
flowchart TD
    A[Operator or Demo User] --> B[Responsive Web Dashboard]
    B --> C[FastAPI Application]
    C --> D[Authentication and Input Validation]
    D --> E[(SQLite or PostgreSQL)]
    D --> F[Daily Ridership Dataset]
    F --> G[Feature Engineering]
    G --> H[ML Model and Seasonal-Naive Benchmark]
    H --> I[Backtesting and Error Analysis]
    H --> J[Forecast Output and Approximate Error Bands]
    J --> B
    K[GTFS Schedule Feed] --> L[GTFS Import and Route Explorer]
    M[Optional GTFS-Realtime Provider] --> N[Vehicle, Trip and Alert Services]
    L --> C
    N --> C
    O[Optional OpenRouter API] <--> P[AI Transit Assistant]
    P <--> C
```

### Request and forecasting lifecycle

1. A visitor opens the dashboard or an authorized operator accesses protected functions.
2. The API loads the available route-day observations and checks their format and coverage.
3. The forecasting pipeline creates calendar and historical-demand features without using future passenger counts.
4. Model-selection backtests compare the ML estimator with the seasonal-naive benchmark.
5. The selected forecast strategy produces recursive multi-day route forecasts.
6. Forecasts, approximate error ranges, source metadata, and model metrics are made available to the dashboard and CSV endpoints.

## Forecasting methodology

### Prediction grain

One training observation represents one `route_id` on one `service_date`, with the target column `passengers` containing the number of boardings recorded for that route and date.

### Feature engineering

The forecasting pipeline derives features from information available up to the forecast origin, including:

- Calendar features: day of week, month, weekend indicator, and annual seasonal sine/cosine terms.
- Historical lags: 1, 7, 14, and 28 days.
- Rolling statistics: trailing seven-day mean, 28-day mean, and 28-day standard deviation.
- Route identity: route ID is encoded as a categorical/model feature.

### Model and fallback strategy

The machine-learning candidate is a scikit-learn `HistGradientBoostingRegressor` within the project's feature-processing pipeline. It is compared with a **seasonal-naive** forecast that repeats the matching weekday's latest available value from the previous week.

The training workflow selects the ML model only when its rolling-origin WAPE is lower than the seasonal-naive WAPE. Otherwise, the saved model bundle selects the seasonal-naive strategy. This makes the simple baseline an explicit fallback rather than assuming a more complex model is always better.

### Recursive forecasting

Forecasts are generated recursively. Earlier predictions in the requested forecast horizon become lag inputs for later forecast dates. Historical backtesting uses the same multi-step approach so that the evaluation resembles the actual forecasting workflow.

### Forecast ranges

The output includes lower and upper approximate bands based on historical forecast residuals. These are useful for visual context, but they are **not formal probabilistic prediction intervals** and should not be interpreted as guaranteed confidence bounds.

## Model evaluation

The training workflow is designed around time order rather than a random train/test split:

1. Build expanding-window model-selection folds.
2. Forecast each fold across the selected horizon.
3. Compare model predictions with the seasonal-naive baseline.
4. Reserve a final chronological holdout period for a separate walk-forward evaluation.
5. Report MAE, RMSE, and WAPE overall and by route/forecast lead day.
6. Save model-selection and holdout predictions so the evaluation can be inspected independently.

### Historical reference experiment

The repository's earlier documented MTA M15 experiment used historical 2022–2024 ridership data. In that particular run, model-selection WAPE was **10.15%** versus **10.17%** for seasonal naive; the final 28-day walk-forward holdout WAPE was **11.92%** versus **18.10%** for the baseline.

These figures describe **one historical route and one evaluation setup only**. They do not establish accuracy for other routes, transit agencies, current dates, or future periods. The synthetic demo's metrics are generated-data examples and must not be presented as real-world performance. Run a fresh evaluation with the target operator's verified data before making planning decisions.

## Data format and validation

### Required CSV columns

| Column | Type | Meaning | Example |
|---|---|---|---|
| `service_date` | Date | Local service date in `YYYY-MM-DD` format | `2026-11-09` |
| `route_id` | String | Stable route identifier | `R10` |
| `passengers` | Number | Non-negative passenger boardings for that route/date | `1240` |

Example:

```csv
service_date,route_id,passengers
2026-11-09,R10,1240
2026-11-09,R22,980
2026-11-10,R10,1315
2026-11-10,R22,1012
```

### Validation rules

- All three required columns must exist.
- Dates must be valid and are normalized to calendar dates.
- Route IDs cannot be blank.
- Passenger counts must be finite, numeric, and non-negative.
- Duplicate route/date rows are rejected; aggregate them before training or ingestion.
- Each route should have a continuous daily history through the latest date in the dataset.
- Missing dates fail closed by default. Use zero-fill only after confirming that a missing record truly means no service or zero demand.

**Important:** a missing row is not automatically evidence of zero passengers. It may mean the service did not run, data was not collected, or an export is incomplete. Use an operator-verified service calendar and documented data rules before treating missing days as zero.

### Minimum history guidance

The application can use a weekday-average baseline after enough continuous observations are present. The README's operator workflow describes 28 continuous days for that baseline and at least 118 continuous days per route before requesting the ML training workflow. Actual model/backtest requirements also depend on forecast horizon and fold configuration; the training command will validate the supplied dataset.

## Transit network and GTFS

The GTFS Schedule importer supports standard feed files including:

- `agency.txt`
- `routes.txt`
- `stops.txt`
- `trips.txt`
- `stop_times.txt`
- `calendar.txt` and `calendar_dates.txt`
- Optional `shapes.txt`

The importer validates archive entries and feed relationships, checks coordinate ranges, and supports GTFS extended times after midnight (for example, `25:30:00`). The dashboard's Transit Network area can be used to inspect imported agencies, route details, service calendars, stop order, schedules, and available route shapes.

GTFS schedules provide network and timetable context; they do **not** provide actual passenger boarding counts. They cannot replace ridership observations for supervised demand forecasting.

## GTFS-Realtime and operations

When a transit agency has a supported feed or a compatible integration configured, the repository provides services for:

- **Vehicle positions:** GPS location, bearing, speed, vehicle status, stop sequence, and occupancy fields when supplied.
- **Trip updates:** delays and predicted stop arrival/departure information.
- **Service alerts:** active disruptions, causes, effects, time windows, and affected transit entities.
- **Data quality filtering:** invalid coordinates, implausible GPS jumps, and stale telemetry handling.

The API also includes operational modules for occupancy calculations, route capacity profiles, service-planning what-if scenarios, bus bunching/gap alerts, dispatch recommendations, and daily/capacity-risk reports.

> **Integration status:** a live GTFS-Realtime provider, ticketing/APC feed, or vehicle telemetry source is not connected automatically by deploying the public demo. Configure and validate an approved provider, credentials, polling schedule, and data mapping before describing the application as live operations monitoring.

## AI transit assistant

The optional AI assistant uses OpenRouter when an API key is configured. It can answer transit questions using the application's available forecast and operational context, such as forecast summaries, route comparisons, and model explanations. When the provider is not configured or is unavailable, the application should return a clear configuration/provider message rather than relying on a fabricated answer.

Configure the key only on the server. Never commit an API key to this repository or expose it in frontend JavaScript.

```env
OPENROUTER_API_KEY=your_openrouter_api_key_here
OPENROUTER_MODEL=openrouter/free
```

## API reference

The API is served by the FastAPI application. The following are the main endpoint groups currently represented in the application code. Endpoints marked **protected** require the configured bearer-key authorization; exact permissions depend on the server's role configuration.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Basic service health |
| `GET` | `/api/ready` | Readiness check for database, storage, and forecast initialization |
| `GET` | `/api/status` | Protected application/data/model status |
| `POST` | `/api/data/upload` | Protected daily ridership CSV upload |
| `POST` | `/api/data/ingest` | Protected daily-count JSON ingestion/upsert |
| `POST` | `/api/train` | Protected model training and evaluation |
| `POST` | `/api/forecast` | Protected forecast refresh |
| `GET` | `/api/forecast.csv` | Protected forecast CSV download |
| `GET` | `/api/template.csv` | Download the blank daily-count CSV template |
| `POST` | `/api/gtfs/upload` | Protected GTFS ZIP upload |
| `GET` | `/api/gtfs/feeds`, `/api/gtfs/agencies` | Imported feed and agency listings |
| `GET` | `/api/gtfs/routes`, `/api/gtfs/routes/{route_id}` | GTFS route catalog and details |
| `GET` | `/api/gtfs/stops` | GTFS stop catalog/search |
| `GET` | `/api/realtime/vehicles`, `/api/realtime/trip-updates`, `/api/realtime/alerts` | Realtime fleet, trip, and alert views |
| `GET` | `/api/forecast/realtime/{route_id}`, `/api/forecast/intraday` | Intraday forecast endpoints |
| `GET` | `/api/operations/alerts`, `/api/operations/dispatch-recommendations`, `/api/operations/metrics` | Operations and dispatch decision support |
| `POST` | `/api/operations/disruptions`, `GET /api/operations/disruptions` | Register service disruptions and adjust intraday demand curves |
| `POST` | `/api/data/etm/validate`, `POST /api/data/etm/ingest` | Validate and ingest Electronic Ticketing Machine (ETM) transaction CSVs |
| `GET` | `/api/model/registry` | Model registry: view active/candidate/retired builds and hyperparameters |
| `GET` | `/api/forecast/accuracy` | Forecast accuracy ledger: rolling WAPE and bias vs seasonal-naive |
| `GET` | `/api/public/monthly-data/routes` | Public Smart Route Explorer: search CTA reference routes |
| `GET` | `/api/public/monthly-data/routes/{route_id}/history` | Public Smart Route Explorer: month-by-month observed boardings |
| `GET` | `/api/quality/drift-status`, `POST /api/quality/check-drift` | Data/model drift assessment |
| `GET` | `/api/model/benchmark` | Multi-model benchmark results |
| `POST` | `/api/simulator/evaluate` | Service-planning scenario evaluation |
| `GET` | `/api/reports/daily`, `/api/reports/daily/csv` | Daily passenger reports |
| `GET` | `/api/reports/capacity-risk` | Capacity-risk report |
| `GET` | `/api/ai/status`, `POST /api/ai/chat` | AI assistant configuration and chat |

For the precise request schema and response model, consult the route definitions in `src/app.py` and the Pydantic request models in the same module. Do not assume an endpoint is connected to a live external data provider merely because the route exists.

### Scheduled daily-count ingestion example

An operator-managed scheduler can send validated route/day records to `POST /api/data/ingest`:

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

Send `Content-Type: application/json` and `Authorization: Bearer <operator-key>`. Matching route/date records are upserted so retries do not multiply counts. Missing dates are rejected by default; use a zero-fill policy only when the data owner has confirmed its meaning. Ingestion refreshes forecasts using the available model; it does not automatically replace the model-training and backtesting review process.

## Technology stack

| Layer | Technologies |
|---|---|
| Language | Python 3.10+ |
| API | FastAPI, Pydantic |
| Data processing | pandas, NumPy |
| Machine learning | scikit-learn, joblib |
| Database | SQLite / PostgreSQL, SQLAlchemy, Alembic |
| Frontend | HTML, CSS, JavaScript |
| Transit data | GTFS Schedule, optional GTFS-Realtime / Protocol Buffers |
| AI integration | OpenRouter API (optional) |
| Testing | Python `unittest` and JavaScript/Node test suite |
| Deployment | Render configuration, Docker support, GitHub Actions |
| App clients | Progressive Web App and PyInstaller Windows build |

## Run locally

### 1. Clone the repository

```bash
git clone https://github.com/Vital1506/bus-passenger-demand-forecasting.git
cd bus-passenger-demand-forecasting
```

### 2. Create and activate a virtual environment

**Windows PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

For the Windows desktop launcher, also install:

```bash
python -m pip install -r requirements-desktop.txt
```

### 4. Start the application

```bash
python -m uvicorn src.app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. The local API's health route is `/api/health` and readiness route is `/api/ready`.

### 5. Run the Windows desktop launcher (optional)

```powershell
python desktop_launcher.py
```

The launcher starts a local API and opens the dashboard in a native WebView window. Local application data is stored in the signed-in user's application-data folder by default.

## Train and generate forecasts

### Train from the bundled synthetic sample

```bash
python -m src.train \
  --data data/sample_daily_ridership.csv \
  --horizon 7 \
  --folds 4
```

On Windows PowerShell, run the command on one line or use PowerShell's backtick for line continuation.

### Fetch and train on public MTA historical data

The workflow supports the public MTA Bus Hourly Ridership dataset, which is historical data rather than a live feed. Example for route M15:

```bash
python -m src.fetch_mta_daily --routes M15 --output artifacts/mta_daily_ridership.csv
python -m src.train --data artifacts/mta_daily_ridership.csv --missing-days zero --horizon 7 --folds 4 --holdout-days 28
```

Review the service calendar before interpreting zero-filled dates as days with no service or zero demand.

### Generate a forecast

```bash
python -m src.forecast \
  --data data/sample_daily_ridership.csv \
  --model artifacts/model.joblib \
  --days 7 \
  --output outputs/forecast.csv
```

The forecast horizon must be compatible with the saved model bundle's training horizon. The CLI validates route history and refuses to silently invent missing passenger observations.

## Configuration

Set environment variables in your local environment or hosting provider's secret manager. Keep `.env` files and all secrets out of Git.

| Variable | Purpose |
|---|---|
| `BUS_FORECAST_API_KEY` | Unified bearer key where a single shared key is appropriate |
| `BUS_FORECAST_OPERATOR_KEY` | Operator access key |
| `BUS_FORECAST_ADMIN_KEY` | Administrative access key for protected management actions |
| `DATABASE_URL` | Optional PostgreSQL connection string; SQLite is the local default |
| `BUS_FORECAST_DATA_DIR` | Optional directory for persistent application data and artifacts |
| `OPENROUTER_API_KEY` | Optional server-side key for the AI assistant |
| `OPENROUTER_MODEL` | Optional OpenRouter model identifier; defaults to `openrouter/free` |

Example PostgreSQL configuration:

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
BUS_FORECAST_OPERATOR_KEY=replace-with-a-long-random-secret
BUS_FORECAST_ADMIN_KEY=replace-with-a-different-long-random-secret
```

Database migrations are managed with Alembic:

```bash
alembic upgrade head
```

For legacy CSV/JSON migration, see [MIGRATIONS.md](MIGRATIONS.md). Review the documented backup and dry-run options before migrating valuable data.

## Desktop and mobile app

### Windows desktop

The repository includes a `desktop_launcher.py` entry point and a GitHub Actions workflow that packages a Windows desktop build. To obtain the latest package, open the [Build Windows desktop app workflow](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/build-app.yml), select a successful run, and download the `BusDemandForecast-Windows` artifact when available. Workflow artifacts are retained for a limited period, so download and archive releases you need to keep.

The desktop executable is not code-signed. Windows SmartScreen may display a publisher warning. Download builds only from the repository's official Actions workflow and follow your organization's software policy; do not disable Defender or other security controls to run an unfamiliar executable.

### Android / mobile

The dashboard is responsive and can be installed as a Progressive Web App (PWA): open the approved HTTPS deployment in Chrome on Android and select **Install app** or **Add to Home screen** when offered. This repository does not currently provide a Play Store APK.

## Deployment

### Live public demo

<div align="center">
  <a href="https://bus-passenger-demand-forecasting-xgx2.onrender.com"><img src="https://img.shields.io/badge/LAUNCH%20BUS%20DEMAND%20DASHBOARD-OPEN%20LIVE%20APP-137F83?style=for-the-badge&logo=render&logoColor=white" alt="Launch live bus demand dashboard" /></a>
</div>

**Live URL:** <https://bus-passenger-demand-forecasting-xgx2.onrender.com>

The public demo is intended for exploration and feedback. It uses synthetic sample records and is read-only. A free hosting instance may sleep while idle, so the first visit can take time to start. Its filesystem is not a durable storage layer; demo data, uploads, and artifacts may not survive a restart or redeploy.

### Deploy your own instance

The repository includes a Render Blueprint and Docker support. For an approved deployment, configure secrets in the hosting dashboard, serve the application over HTTPS, and attach persistent storage for the configured data directory. A basic Docker example is:

```bash
docker build -t bus-demand-forecast .
docker run --rm -p 8000:8000 \
  -e BUS_FORECAST_API_KEY='replace-with-a-long-random-secret' \
  -v bus-forecast-data:/var/lib/bus-forecast \
  bus-demand-forecast
```

Before importing operational data, verify the container's configured data directory, database URL, authentication policy, backups, and restore procedure. Persistent storage alone is not a backup strategy.

## Tests and model training

Run the Python test suite:

```bash
python -m unittest discover -s tests
```

The repository also contains frontend/JavaScript tests and GitHub Actions workflows for Python checks, model training, and Windows packaging. Review the [Actions tab](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions) for the latest workflow results rather than relying on a static status number in this document.

The automated checks cover important data-validation and forecasting behaviors, but passing tests cannot prove that every deployment, external feed, dataset, or operating environment is defect-free. Validate the application against the systems and data it will actually use.

## Limitations and responsible use

This is an actively developed prototype and decision-support project. It should not be described as a production-certified transit platform or as a guaranteed accurate forecasting service.

- The public demo uses synthetic data and is not evidence of real-world forecast accuracy.
- MTA experiment results cover one historical route and cannot establish generalization to other agencies or future periods.
- A missing route/date record can mean missing data, no service, or zero demand. Confirm the correct interpretation with a service calendar and data owner.
- A live ticketing, APC/AFC, AVL, or GTFS-Realtime provider must be configured and validated separately; one is not automatically connected by opening the demo.
- Holidays, school terms, service changes, weather, disruptions, and special events are not all included as future-known predictors in the daily forecasting model.
- Error bands are historical-residual approximations, not formal guarantees.
- Before a company pilot, complete data governance, access-control, privacy, security, backup/restore, monitoring, shadow-mode, and operational acceptance reviews.

For a structured pilot checklist, see [COMPANY_PILOT.md](COMPANY_PILOT.md). Do not upload confidential company or passenger data to the public demonstration deployment.

## Project resources

| Resource | Link |
|---|---|
| Live application | [Open the dashboard](https://bus-passenger-demand-forecasting-xgx2.onrender.com) |
| Source repository | [GitHub repository](https://github.com/Vital1506/bus-passenger-demand-forecasting) |
| Model training workflow | [GitHub Actions](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/train-model.yml) |
| Windows build workflow | [GitHub Actions](https://github.com/Vital1506/bus-passenger-demand-forecasting/actions/workflows/build-app.yml) |
| Migration guide | [MIGRATIONS.md](MIGRATIONS.md) |
| Company pilot checklist | [COMPANY_PILOT.md](COMPANY_PILOT.md) |
| MTA historical ridership source | [NY Open Data](https://data.ny.gov/Transportation/MTA-Bus-Hourly-Ridership-2020-2024/kv7t-n8in) |

---

<div align="center">
  <p><strong>Built to make transit demand easier to inspect, explain, and evaluate.</strong></p>
  <p>Bus Passenger Demand Forecasting · <a href="https://github.com/Vital1506">Vital1506</a></p>
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:137F83,55:123B46,100:0B1F2A&height=110&section=footer&animation=twinkling" width="100%" alt="Animated footer banner" />
</div>
