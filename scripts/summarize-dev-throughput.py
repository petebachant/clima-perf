"""Summarize how software development throughput has changed over time.

Reads ``results/dev-throughput.csv`` (written by the analyze-dev-speed
notebook) and reports, for the ecosystem-wide "All repos" aggregate, the
per-quarter trend and a first-versus-last comparison for merged PRs,
time-to-merge, and releases. Writes
``results/dev-throughput-summary.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path.cwd()

MIN_QUARTER = "2018-Q1"


def trend(series: pd.Series) -> dict:
    """Linear slope and endpoint means for one metric over time."""
    s = series.dropna()
    if len(s) < 2:
        return {}
    x = np.arange(len(s))
    slope = float(np.polyfit(x, s.values.astype(float), 1)[0])
    window = 4  # one year
    return {
        "per_quarter_slope": round(slope, 3),
        "per_year_slope": round(slope * 4, 2),
        "first_year_mean": round(float(s.head(window).mean()), 2),
        "last_year_mean": round(float(s.tail(window).mean()), 2),
    }


def main() -> None:
    df = pd.read_csv(ROOT / "results" / "dev-throughput.csv")
    agg = (
        df[df["repo"] == "All repos"]
        .sort_values("quarter")
        .reset_index(drop=True)
    )
    agg = agg[agg["quarter"] >= MIN_QUARTER]

    summary = {
        "first_quarter": agg["quarter"].iloc[0],
        "last_quarter": agg["quarter"].iloc[-1],
        "n_repos_tracked": int(df["repo"].nunique() - 1),  # minus "All repos"
        "merged_prs": trend(agg["n_prs"]),
        "median_ttm_days": trend(agg["median_ttm_days"]),
        "p90_ttm_days": trend(agg["p90_ttm_days"]),
        "releases": trend(agg["n_releases"]),
    }
    # Convenience ratios for the answer's prose.
    summary["pr_throughput_multiple"] = round(
        summary["merged_prs"]["last_year_mean"]
        / summary["merged_prs"]["first_year_mean"],
        1,
    )
    summary["release_rate_multiple"] = round(
        summary["releases"]["last_year_mean"]
        / summary["releases"]["first_year_mean"],
        1,
    )

    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/dev-throughput-summary.json", "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    print(
        f"{summary['first_quarter']} to {summary['last_quarter']}, "
        f"{summary['n_repos_tracked']} repos"
    )
    print(
        f"  merged PRs/quarter: "
        f"{summary['merged_prs']['first_year_mean']} -> "
        f"{summary['merged_prs']['last_year_mean']} "
        f"({summary['pr_throughput_multiple']}x, "
        f"{summary['merged_prs']['per_year_slope']:+.0f}/year)"
    )
    print(
        f"  median TTM (days): "
        f"{summary['median_ttm_days']['first_year_mean']} -> "
        f"{summary['median_ttm_days']['last_year_mean']}"
    )
    print(
        f"  releases/quarter: "
        f"{summary['releases']['first_year_mean']} -> "
        f"{summary['releases']['last_year_mean']} "
        f"({summary['release_rate_multiple']}x)"
    )
    print("wrote results/dev-throughput-summary.json")


if __name__ == "__main__":
    main()
