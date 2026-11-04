"""Summarize the rate of change of external engagement.

Combines the event streams analyze-engagement writes:

- ``results/external-citations.csv`` -- external citing works per quarter
- ``results/github-engagement.csv`` -- external stars, forks, issues, merged
  PRs, and discussions, split here into Oceananigans and every other repo,
  since Oceananigans is most of it
- ``results/new-registry-dependents.csv`` -- new external Julia-registry
  dependents per quarter

A single trend over the whole record hides a change of pace, e.g.,
citations that grew for years and then leveled off, so each stream gets
both the long-run trend and the trend over the last two years, plus the
last four quarters against the four before. Only complete quarters count:
the one in progress would read as a dip. Writes
``results/engagement-summary.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path.cwd()

# The floor the engagement figures use; anything earlier is noise from old
# papers predating the packaging effort.
MIN_QUARTER = "2018-Q1"
RECENT_QUARTERS = 8


def last_complete_quarter(eng: pd.DataFrame) -> str:
    """The newest quarter the data covers in full.

    The GitHub events run through the last day fetched, so its quarter is
    complete only if that day ends the quarter.
    """
    through = (
        pd.to_datetime(eng["event_at"], utc=True, format="ISO8601")
        .max()
        .tz_localize(None)
        .normalize()
    )
    quarter = pd.Period(through, freq="Q")
    if pd.Period(through + pd.Timedelta(days=1), freq="Q") == quarter:
        quarter -= 1
    return f"{quarter.year}-Q{quarter.quarter}"


def slope_per_year(counts: pd.Series) -> float:
    if len(counts) < 2:
        return float("nan")
    x = np.arange(len(counts))
    return round(float(np.polyfit(x, counts.values.astype(float), 1)[0]) * 4, 1)


def summarize_series(counts: pd.Series) -> dict:
    counts = counts.sort_index()
    if len(counts) < 2:
        return {}
    last_year = counts.tail(4).mean()
    prior_year = counts.iloc[-8:-4].mean()
    return {
        "n_events": int(counts.sum()),
        "first_quarter": counts.index[0],
        "last_quarter": counts.index[-1],
        "long_run_per_year": slope_per_year(counts),
        "recent_per_year": slope_per_year(counts.tail(RECENT_QUARTERS)),
        "last_year_mean": round(float(last_year), 1),
        "prior_year_mean": round(float(prior_year), 1),
        "change_pct": round(100 * (last_year / prior_year - 1))
        if prior_year
        else None,
    }


def main() -> None:
    cit = pd.read_csv(ROOT / "results" / "external-citations.csv")
    eng = pd.read_csv(ROOT / "results" / "github-engagement.csv")
    deps = pd.read_csv(ROOT / "results" / "new-registry-dependents.csv")
    last_q = last_complete_quarter(eng)

    def per_quarter(df: pd.DataFrame) -> pd.Series:
        """Events per quarter over complete quarters, zeros included."""
        s = df.groupby("quarter").size()
        s = s[(s.index >= MIN_QUARTER) & (s.index <= last_q)]
        if s.empty:
            return s
        periods = pd.period_range(s.index.min(), last_q, freq="Q")
        index = [f"{p.year}-Q{p.quarter}" for p in periods]
        return s.reindex(index, fill_value=0)

    is_ocean = eng["repo"] == "Oceananigans"
    streams = {
        "citations": per_quarter(cit),
        "github_events": per_quarter(eng),
        "github_oceananigans": per_quarter(eng[is_ocean]),
        "github_other_repos": per_quarter(eng[~is_ocean]),
        "registry_dependents": per_quarter(deps),
    }
    summary = {name: summarize_series(s) for name, s in streams.items()}
    summary["last_complete_quarter"] = last_q

    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/engagement-summary.json", "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    for name in streams:
        s = summary[name]
        print(
            f"{name:20s} long run {s['long_run_per_year']:+6.1f}/yr, last "
            f"{RECENT_QUARTERS}q {s['recent_per_year']:+6.1f}/yr, last year "
            f"{s['last_year_mean']}/q vs {s['prior_year_mean']}/q"
        )
    print(f"through {last_q}; wrote results/engagement-summary.json")


if __name__ == "__main__":
    main()
