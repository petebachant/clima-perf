"""Summarize the flagship AMIP speedup for the question's answer.

Reads ``results/amip.csv`` (written by the analyze notebook) and reports the
headline figures: the speedup at the end of the series relative to the
November 2025 baseline, the peak, and a robust recent level that ignores the
occasional crashed run. Writes ``results/amip-speedup-summary.json``.

The normalization matches the notebook: ``speedup`` is SYPD divided by the
first measured SYPD, so the first point is 1.0 and every later point is a
multiple of the November 2025 baseline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

ROOT = Path.cwd()

# A speedup this far below the running peak is a run that crashed early
# rather than a genuine regression (the benchmark prints a low SYPD when it
# dies partway through), so it is excluded from the recent-level figure.
CRASH_FRACTION = 0.5

# How recent a window to summarize the current level over.
RECENT_DAYS = 90


def main() -> None:
    df = pd.read_csv(ROOT / "results" / "amip.csv")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df[df["speedup"].notna()].sort_values("date").reset_index(drop=True)

    latest = df.iloc[-1]
    peak = df.loc[df["speedup"].idxmax()]

    recent = df[df["date"] >= df["date"].max() - pd.Timedelta(days=RECENT_DAYS)]
    healthy = recent[
        recent["speedup"] >= recent["speedup"].max() * CRASH_FRACTION
    ]

    baseline_date = df["date"].iloc[0]
    summary = {
        "baseline_date": baseline_date.date().isoformat(),
        "baseline_sypd": round(float(df["sypd"].iloc[0]), 4),
        "latest_date": latest["date"].date().isoformat(),
        "latest_speedup": round(float(latest["speedup"]), 2),
        "latest_sypd": round(float(latest["sypd"]), 4),
        "peak_speedup": round(float(peak["speedup"]), 2),
        "peak_date": peak["date"].date().isoformat(),
        "recent_median_speedup": round(float(healthy["speedup"].median()), 2),
        "recent_window_days": RECENT_DAYS,
        "n_runs": int(len(df)),
    }

    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/amip-speedup-summary.json", "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(
        f"{summary['baseline_date']} to {summary['latest_date']}: "
        f"{summary['latest_speedup']}x (recent median "
        f"{summary['recent_median_speedup']}x, peak "
        f"{summary['peak_speedup']}x)"
    )
    print("wrote results/amip-speedup-summary.json")


if __name__ == "__main__":
    main()
