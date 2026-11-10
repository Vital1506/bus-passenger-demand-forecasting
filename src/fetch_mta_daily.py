"""Fetch public MTA bus ridership and aggregate it to daily route counts.

The upstream dataset is hourly and grouped by fare/payment category. This
script sums those categories into the project's service_date/route_id/passengers
contract. Missing route-days are left absent and must be handled explicitly by
the training command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


DATASET_URL = "https://data.ny.gov/Transportation/MTA-Bus-Hourly-Ridership-2020-2024/kv7t-n8in"
API_URL = "https://data.ny.gov/resource/kv7t-n8in.json"
PAGE_SIZE = 50_000
ROUTE_PATTERN = re.compile(r"^[A-Za-z0-9.+_-]{1,24}$")


def fetch_page(params: dict[str, str]) -> list[dict]:
    url = f"{API_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "bus-passenger-demand-forecasting/1.0 (public-data portfolio)"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read().decode("utf-8")
            rows = json.loads(payload)
            if not isinstance(rows, list):
                raise ValueError("MTA API returned an unexpected response shape")
            return rows
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(f"MTA API returned HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}") from exc
            if attempt == 3:
                raise RuntimeError(f"MTA API remained unavailable after retries (HTTP {exc.code})") from exc
            time.sleep(min(2 ** attempt, 8))
        except (URLError, TimeoutError) as exc:
            if attempt == 3:
                reason = getattr(exc, "reason", str(exc))
                raise RuntimeError(f"Could not reach the MTA public-data API: {reason}") from exc
            time.sleep(min(2 ** attempt, 8))
    return []


def fetch_daily_counts(
    start_date: str = "2022-02-01",
    end_date: str = "2024-12-31",
    routes: list[str] | None = None,
) -> pd.DataFrame:
    """Return one summed passenger count per local calendar date and bus route."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start-date must be on or before end-date")
    selected_routes = routes or ["M15"]
    if not selected_routes or any(not ROUTE_PATTERN.fullmatch(route) for route in selected_routes):
        raise ValueError("Routes must be comma-separated MTA route identifiers using letters, digits, '.', '+', '_' or '-'")
    selected_routes = sorted(set(selected_routes))
    route_filter = ",".join("'" + route + "'" for route in selected_routes)
    rows: list[dict] = []
    # Monthly windows keep Socrata queries bounded and avoid request timeouts
    # when the source table scans several years of hourly fare-category rows.
    for month in pd.period_range(start=start, end=end, freq="M"):
        window_start = max(start, month.start_time.normalize())
        window_end = min(end, month.end_time.normalize())
        where = (
            f"transit_timestamp >= '{window_start:%Y-%m-%d}T00:00:00' "
            f"AND transit_timestamp < '{(window_end + pd.Timedelta(days=1)):%Y-%m-%d}T00:00:00' "
            f"AND bus_route in ({route_filter})"
        )
        offset = 0
        while True:
            page = fetch_page({
                "$select": "date_trunc_ymd(transit_timestamp) as service_date,bus_route as route_id,sum(ridership) as passengers",
                "$where": where,
                "$group": "service_date,route_id",
                "$order": "service_date,route_id",
                "$limit": str(PAGE_SIZE),
                "$offset": str(offset),
            })
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += len(page)
    if not rows:
        raise ValueError(f"The MTA dataset returned no rows for routes {', '.join(selected_routes)} in the requested date range")

    data = pd.DataFrame(rows).rename(columns={"service_date": "service_date", "route_id": "route_id", "passengers": "passengers"})
    missing = {"service_date", "route_id", "passengers"} - set(data.columns)
    if missing:
        raise ValueError(f"MTA API response is missing columns: {', '.join(sorted(missing))}")
    data = data[["service_date", "route_id", "passengers"]].copy()
    data["service_date"] = pd.to_datetime(data["service_date"], errors="coerce").dt.normalize()
    data["route_id"] = data["route_id"].astype("string").str.strip()
    data["passengers"] = pd.to_numeric(data["passengers"], errors="coerce")
    if data.isna().any().any() or not data["passengers"].map(pd.notna).all():
        raise ValueError("MTA API returned blank or invalid date, route, or ridership values")
    data["route_id"] = data["route_id"].astype(str)
    if (data["passengers"] < 0).any() or data.duplicated(["service_date", "route_id"]).any():
        raise ValueError("MTA daily aggregation contains negative counts or duplicate route/date rows")
    if not data["route_id"].isin(selected_routes).all():
        raise ValueError("MTA API returned a route outside the requested route selection")
    return data.sort_values(["service_date", "route_id"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2022-02-01", help="Inclusive local service date (default: 2022-02-01)")
    parser.add_argument("--end-date", default="2024-12-31", help="Inclusive local service date (default: 2024-12-31)")
    parser.add_argument("--routes", default="M15", help="Comma-separated MTA routes (default: M15)")
    parser.add_argument("--output", default="artifacts/mta_daily_ridership.csv")
    args = parser.parse_args()
    try:
        routes = [route.strip() for route in args.routes.split(",") if route.strip()]
        data = fetch_daily_counts(args.start_date, args.end_date, routes)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        data.to_csv(output, index=False, date_format="%Y-%m-%d")
        expected_days = (pd.Timestamp(args.end_date) - pd.Timestamp(args.start_date)).days + 1
        coverage = {
            route: {
                "observed_route_days": int(len(group)),
                "date_span_days": expected_days,
                "unreported_route_days": max(0, expected_days - int(len(group))),
            }
            for route, group in data.groupby("route_id")
        }
        source_bytes = output.read_bytes()
        metadata = {
            "dataset": "MTA Bus Hourly Ridership: 2020-2024",
            "source_url": DATASET_URL,
            "api_url": API_URL,
            "aggregation": "Sum hourly ridership across fare/payment categories by local date and bus route",
            "start_date_inclusive": args.start_date,
            "end_date_inclusive": args.end_date,
            "routes": routes,
            "row_count": int(len(data)),
            "coverage": coverage,
            "missing_route_day_policy": "Rows absent from the source remain absent. Training must explicitly choose an interpretation; the demo workflow uses zero for unreported passenger boardings and documents this assumption.",
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "csv_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"Saved {len(data):,} daily route counts to {output}")
        for route, values in coverage.items():
            print(f"Route {route}: {values['observed_route_days']:,}/{expected_days:,} calendar dates returned by source")
        print(f"Source: {DATASET_URL}")
    except (ValueError, RuntimeError, HTTPError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

