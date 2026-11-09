"""Create a deterministic synthetic dataset for trying the starter workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/sample_daily_ridership.csv")
    parser.add_argument("--days", type=int, default=730)
    args = parser.parse_args()
    if args.days < 60:
        parser.error("--days must be at least 60")
    rng = np.random.default_rng(73)
    end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    dates = pd.date_range(end=end, periods=args.days, freq="D")
    route_base = {"R10": 720, "R22": 460, "R35": 310}
    rows = []
    for route, base in route_base.items():
        for date in dates:
            weekly = 1.0 if date.dayofweek < 5 else 0.55
            annual = 1.0 + 0.12 * np.sin(2 * np.pi * date.dayofyear / 365.25)
            trend = 1.0 + 0.00015 * (date - dates[0]).days
            noise = rng.normal(1.0, 0.07)
            passengers = max(0, round(base * weekly * annual * trend * noise))
            rows.append({"service_date": date.date().isoformat(), "route_id": route, "passengers": passengers})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output, index=False)
    print(f"Wrote {len(rows)} synthetic rows to {output}")


if __name__ == "__main__":
    main()
