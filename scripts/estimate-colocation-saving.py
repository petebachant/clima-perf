"""Rough estimate of the development speed-up from co-locating tightly
coupled CliMA packages into shared repos.

The idea being tested: if a release in package ``B`` forces a compat bump
in dependent ``A``, then housing ``A`` and ``B`` in one repo would let the
two land as a single pull request instead of two. This script sizes that
opportunity from the history the other analyses already use.

What it measures, and what it deliberately does not:

- A **cascade event** is one repo-day of dependency propagation: a
  dependency update in ``A`` landing within two weeks of a release in
  another package (grouped by repo and day, since a batch of same-day
  CompatHelper commits merges as one PR).
- Each event is classed **mechanical** (CompatHelper, compat entries,
  manifest or ``Project.toml`` refreshes) or **substantive** (interface
  adaptation, a feature that pulled the new version in). Only the
  mechanical ones are plausibly absorbed by co-location; the substantive
  work still happens, just in the combined repo.
- The output is a **volume** estimate -- events and pull requests per
  year that could be eliminated -- not a wall-clock saving. Dependency
  updates already merge in a median of well under a day, so the time won
  is in CI runs and review cycles, which this dataset cannot size. That
  distinction is stated plainly so the number is not read as more than it
  is.
"""

from __future__ import annotations

import json
import os
import re
import sys
from importlib import import_module
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parent))
projecttoml_graph = import_module("projecttoml-graph")

ROOT = Path.cwd()
DATA = ROOT / "data" / "github"

# A release's dependents have two weeks to adopt it before the link is not
# counted; matches the other analyses.
WINDOW_DAYS = projecttoml_graph.CASCADE_WINDOW_DAYS

# Only the last few years are used for the rate: early history has a
# handful of quarters as the packaging effort spun up, which would skew a
# per-year average.
RATE_START = "2021-Q2"
RATE_END = "2026-Q3"


def pull_request_share() -> dict:
    """Cross-check the commit-based estimate against merged PR titles.

    A merged PR whose title is only about propagation (CompatHelper, a
    dependency bump, a compat update) is counted. This over-counts slightly
    (it misses propagation folded into larger PRs) and under-counts
    slightly (a title can match without the PR being pure propagation), so
    it is a sanity band around the commit-based figure, not the headline.
    """
    compat_helper = re.compile(r"compathelper", re.I)
    dep_ish = re.compile(
        r"compat|update dependenc|bump compat|dependabot|update .* to v?\d",
        re.I,
    )
    release_ish = re.compile(
        r"bump (the )?(patch|minor|major)|version for|tag v?\d"
        r"|make .*release|new release|bump .*version",
        re.I,
    )
    prs_root = DATA / "prs"
    rows = []
    if not prs_root.exists():
        return {"n_merged": 0, "n_propagation": 0}
    for f in prs_root.glob("*/*.json"):
        repo = f.parent.name
        for pr in json.loads(f.read_text()):
            if not pr.get("mergedAt"):
                continue
            title = pr.get("title") or ""
            propagation = bool(compat_helper.search(title)) or (
                bool(dep_ish.search(title))
                and not release_ish.search(title)
            )
            rows.append(
                {
                    "repo": projecttoml_graph.camel(repo),
                    "merged_at": projecttoml_graph.parse_iso(pr["mergedAt"]),
                    "propagation": propagation,
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return {"n_merged": 0, "n_propagation": 0}
    df["quarter"] = df["merged_at"].map(projecttoml_graph.quarter_of)
    in_rate = df[(df["quarter"] >= RATE_START) & (df["quarter"] <= RATE_END)]
    return {
        "n_merged": int(len(in_rate)),
        "n_propagation": int(in_rate["propagation"].sum()),
    }


def release_ceremony(commits, events) -> dict:
    """Classify every release by how much of it is dependency bookkeeping.

    The drudgery complaint is about the per-release ritual -- increment,
    merge, tag via Registrator, then propagate downstream -- not PR count.
    Two grounded signals are reported:

    - a release is **reactive** when an upstream-triggered dependency
      update landed in the same repo within the window before it, so the
      release exists to keep the version graph consistent rather than to
      ship a feature;
    - the **git-tagged** flag marks releases that also show up as a GitHub
      release, which is where the Registrator tag lands.

    A third signal -- a release day with no substantive *Project.toml*
    commit -- is intentionally not treated as evidence of a pure-ceremony
    release, since real code changes may not touch Project.toml at all.
    """
    rels = projecttoml_graph.releases(commits)
    rel_re = projecttoml_graph.RELEASE_RE

    events_by_repo: dict[str, list] = {}
    for e in events:
        events_by_repo.setdefault(e.repo, []).append(e.date)

    per_repo: dict[str, dict[str, int]] = {}
    total = 0
    reactive = 0
    with_substantive_dep_work = 0
    for repo, times in rels.items():
        repo_counts = per_repo.setdefault(
            repo, {"total": 0, "reactive": 0}
        )
        for t in times:
            total += 1
            repo_counts["total"] += 1
            is_reactive = any(
                0 < (t - ed).days <= WINDOW_DAYS
                for ed in events_by_repo.get(repo, [])
            )
            if is_reactive:
                reactive += 1
                repo_counts["reactive"] += 1
            else:
                with_substantive_dep_work += 1

    return {
        "total": total,
        "reactive": reactive,
        "frac_reactive": round(reactive / total, 2) if total else 0.0,
        "non_reactive": with_substantive_dep_work,
        "per_repo": per_repo,
    }


def external_dependents() -> dict[str, int]:
    """Count non-CliMA registry dependents per package.

    Most packages have no external users, so the release/propagate
    machinery is serving internal callers only -- relevant to whether the
    ceremony is worth automating or removing.
    """
    path = ROOT / "data" / "julia-registry" / "dependents.jsonl"
    ext: dict[str, int] = {}
    if not path.exists():
        return ext
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not row.get("is_clima"):
            pkg = row["clima_package"]
            ext[pkg] = ext.get(pkg, 0) + 1
    return ext



def main() -> None:
    commits = projecttoml_graph.load_commits()
    events = projecttoml_graph.cascade_events(commits, WINDOW_DAYS)

    # --- Per-year rate over the recent window ---------------------------
    in_rate = [
        e
        for e in events
        if RATE_START <= projecttoml_graph.quarter_of(e.date) <= RATE_END
    ]
    quarters = {
        projecttoml_graph.quarter_of(e.date) for e in in_rate
    }
    n_quarters = len(quarters)
    years = n_quarters / 4.0

    n_events = len(in_rate)
    n_mechanical = sum(1 for e in in_rate if e.mechanical)
    n_substantive = n_events - n_mechanical

    events_per_year = n_events / years
    eliminable_per_year = n_mechanical / years

    # --- Per-repo table, which is where co-location would pay off -------
    by_repo: dict[str, dict[str, int]] = {}
    for e in in_rate:
        row = by_repo.setdefault(
            e.repo, {"n_events": 0, "n_mechanical": 0}
        )
        row["n_events"] += 1
        if e.mechanical:
            row["n_mechanical"] += 1
    repo_table = pd.DataFrame(
        [
            {
                "repo": repo,
                "n_events": v["n_events"],
                "n_mechanical": v["n_mechanical"],
                "events_per_year": round(v["n_events"] / years, 1),
                "mechanical_per_year": round(v["n_mechanical"] / years, 1),
            }
            for repo, v in by_repo.items()
        ]
    ).sort_values("n_mechanical", ascending=False).reset_index(drop=True)

    # --- PR-title cross-check -------------------------------------------
    pr = pull_request_share()
    pr_per_year = pr["n_propagation"] / years if years else 0.0

    # --- Release ceremony, the drudgery the devs describe ---------------
    ceremony = release_ceremony(commits, events)
    releases_per_year = ceremony["total"] / years

    # --- External users, to weigh the ceremony against its reach --------
    ext = external_dependents()
    repos_with_external = sum(1 for r in ceremony["per_repo"] if ext.get(r))

    # --- Machine-readable result ----------------------------------------
    result = {
        "window_days": WINDOW_DAYS,
        "rate_start": RATE_START,
        "rate_end": RATE_END,
        "years_covered": round(years, 1),
        "n_cascade_events": n_events,
        "n_mechanical": n_mechanical,
        "n_substantive": n_substantive,
        "frac_substantive": round(n_substantive / n_events, 2),
        "frac_mechanical": round(n_mechanical / n_events, 2),
        "events_per_year": round(events_per_year, 1),
        "eliminable_events_per_year": round(eliminable_per_year, 1),
        "pr_propagation_per_year": round(pr_per_year, 1),
        "n_merged_prs_in_window": pr["n_merged"],
        "n_propagation_prs_in_window": pr["n_propagation"],
        "busiest_repos": ", ".join(repo_table.head(6)["repo"].tolist()),
        "n_releases": ceremony["total"],
        "releases_per_year": round(releases_per_year, 1),
        "n_reactive_releases": ceremony["reactive"],
        "frac_reactive_releases": ceremony["frac_reactive"],
        "packages_with_external_users": repos_with_external,
        "packages_releasing": len(ceremony["per_repo"]),
    }
    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/colocation-saving.json", "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    # Per-repo table now carries releases and external users alongside the
    # cascade events, so the co-location candidates can be judged on the
    # ceremony they impose versus how many outside users they serve.
    repo_table["n_releases"] = repo_table["repo"].map(
        lambda r: ceremony["per_repo"].get(r, {}).get("total", 0)
    )
    repo_table["n_reactive_releases"] = repo_table["repo"].map(
        lambda r: ceremony["per_repo"].get(r, {}).get("reactive", 0)
    )
    repo_table["external_dependents"] = repo_table["repo"].map(
        lambda r: ext.get(r, 0)
    )
    repo_table.to_csv(ROOT / "results/colocation-by-repo.csv", index=False)

    # --- Figure ----------------------------------------------------------
    # The decision is about ceremony versus reach, so the figure leads with
    # releases: bars are the full release count, with the reactive
    # (upstream-driven) share overlaid, and the number of external users
    # annotated so a busy-but-unused package is obvious at a glance.
    repo_plot = repo_table.sort_values("n_releases", ascending=False).copy()
    repo_plot = repo_plot[repo_plot["n_releases"] > 0]

    def _reactive_frac(row) -> float:
        if not row["n_releases"]:
            return 0.0
        return row["n_reactive_releases"] / row["n_releases"]

    repo_plot["reactive_frac"] = repo_plot.apply(_reactive_frac, axis=1)

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            y=repo_plot["repo"],
            x=repo_plot["n_releases"],
            name="All releases",
            orientation="h",
            marker_color="#d9d9d9",
            hovertemplate="%{y}<br>%{x} releases<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            y=repo_plot["repo"],
            x=repo_plot["n_reactive_releases"],
            name="Reactive (upstream-driven)",
            orientation="h",
            marker_color="#1F77B4",
            customdata=repo_plot[
                ["external_dependents", "reactive_frac"]
            ].values,
            hovertemplate=(
                "%{y}<br>%{x} reactive releases"
                "<br>%{customdata[0]} external dependents"
                "<br>%{customdata[1]:.0%} of releases<extra></extra>"
            ),
        )
    )
    # Annotate external users to the right of each bar: the whole point is
    # that several high-release packages serve no one outside CliMA.
    for _, row in repo_plot.iterrows():
        fig.add_annotation(
            x=row["n_releases"],
            y=row["repo"],
            text=f"  {int(row['external_dependents'])} ext.",
            showarrow=False,
            xanchor="left",
            font=dict(size=10, color="#666"),
        )
    fig.update_layout(
        barmode="overlay",
        title="Release burden by repo, with upstream-driven share",
        xaxis_title="Releases since first tagged (external dependents at right)",
        height=720,
        margin=dict(t=70, b=40, l=160, r=120),
        legend=dict(orientation="h", y=-0.1),
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(range=[0, repo_plot["n_releases"].max() * 1.25])
    os.makedirs(ROOT / "figures", exist_ok=True)
    fig.write_json(ROOT / "figures/colocation-saving.json")

    print(
        f"{n_events} cascade events over {years:.1f} years "
        f"({events_per_year:.0f}/year)"
    )
    print(
        f"  mechanical: {n_mechanical} ({n_mechanical/n_events:.0%}), "
        f"{eliminable_per_year:.0f}/year eliminable"
    )
    print(
        f"  PR-title cross-check: {pr['n_propagation']} of "
        f"{pr['n_merged']} merged PRs ({pr_per_year:.0f}/year)"
    )
    print(
        f"releases: {ceremony['total']} "
        f"({releases_per_year:.0f}/year), reactive: "
        f"{ceremony['reactive']} ({ceremony['frac_reactive']:.0%})"
    )
    print(
        f"  packages with any external dependents: "
        f"{repos_with_external} of {len(ceremony['per_repo'])}"
    )
    print(f"wrote results/colocation-saving.json")
    print(f"wrote results/colocation-by-repo.csv")
    print(f"wrote figures/colocation-saving.json")


if __name__ == "__main__":
    main()
