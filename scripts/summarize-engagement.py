"""Summarize the rate of change of external engagement.

Combines the three event streams the analyze notebook writes:

- ``results/external-citations.csv`` -- external citing works per quarter
- ``results/github-engagement.csv`` -- external stars/forks/issues/PRs
- ``results/new-registry-dependents.csv`` -- new external Julia-registry
  dependents per quarter

For each, reports the per-quarter linear trend (fitted over the quarters
with data) and a first-window versus last-window comparison, so the answer
can state a rate without retyping it. Writes
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


def quarter_index(q: str) -> int:
    year, qq = q.split("-Q")
    return int(year) * 4 + int(qq) - 1


def summarize_series(counts: pd.Series) -> dict:
    counts = counts.sort_index()
    if len(counts) < 2:
        return {}
    x = np.arange(len(counts))
    slope = float(np.polyfit(x, counts.values.astype(float), 1)[0])
    window = max(1, len(counts) // 4)
    return {
        "n_events": int(counts.sum()),
        "n_quarters": int(len(counts)),
        "first_quarter": counts.index[0],
        "last_quarter": counts.index[-1],
        "per_quarter_slope": round(slope, 2),
        "per_year_slope": round(slope * 4, 1),
        "first_window_mean": round(float(counts.head(window).mean()), 1),
        "last_window_mean": round(float(counts.tail(window).mean()), 1),
    }


def main() -> None:
    cit = pd.read_csv(ROOT / "results" / "external-citations.csv")
    eng = pd.read_csv(ROOT / "results" / "github-engagement.csv")
    deps = pd.read_csv(ROOT / "results" / "new-registry-dependents.csv")

    def per_quarter(df: pd.DataFrame) -> pd.Series:
        s = df.groupby("quarter").size()
        return s[s.index >= MIN_QUARTER]

    summary = {
        "citations": summarize_series(per_quarter(cit)),
        "github_events": summarize_series(per_quarter(eng)),
        "registry_dependents": summarize_series(per_quarter(deps)),
    }

    # One combined rate the answer can lead with: total external events per
    # year over the two windows that both streams cover.
    all_quarters = sorted(
        set(per_quarter(cit).index)
        | set(per_quarter(eng).index)
        | set(per_quarter(deps).index)
    )
    combined = pd.Series(0, index=all_quarters, dtype=float)
    for s in (per_quarter(cit), per_quarter(eng), per_quarter(deps)):
        combined = combined.add(s.reindex(all_quarters, fill_value=0), fill_value=0)
    summary["combined"] = summarize_series(combined)

    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/engagement-summary.json", "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    for name in ("citations", "github_events", "registry_dependents", "combined"):
        s = summary[name]
        print(
            f"{name:20s} {s['n_events']:5d} events, "
            f"{s['per_year_slope']:+6.1f}/year "
            f"({s['first_window_mean']} -> {s['last_window_mean']}/quarter)"
        )
    print("wrote results/engagement-summary.json")


if __name__ == "__main__":
    main()
