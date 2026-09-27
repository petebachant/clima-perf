"""Which clusters of CliMA repos tend to change together?

Combines two views of the ``Project.toml`` commit history fetched by
``fetch-github``:

1. **A dependency graph**, where an edge ``A -> B`` means a dependency
   update in ``A`` landed within two weeks of a release in ``B``. Across
   the tracked packages this resolves into the expected nesting: the
   low-level utility packages (``ClimaCore``, ``ClimaParams``,
   ``Thermodynamics``, ``CloudMicrophysics``) upstream, and ``ClimaAtmos``
   and ``ClimaLand`` downstream, with ``ClimaAtmos`` feeding back into
   ``ClimaLand``.
2. **A co-change correlation matrix**, from each repo's quarterly
   ``Project.toml`` commit count. Pairs that move together across quarters
   are the clusters. Because dependency updates are what these commits
   mostly are, the clusters are what you would expect from a release
   cascading out through the stack rather than from independent work.

The script writes ``results/repo-cochange.csv`` (the per-repo quarterly
commit counts, with a ``changes`` label per release-vs-dependency-update
split) and ``figures/repo-cochange.json``.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import import_module
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# The shared graph helper lives alongside this script and has a hyphen in
# its name, so it is imported by path rather than as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
projecttoml_graph = import_module("projecttoml-graph")

ROOT = Path.cwd()

CASCADE_WINDOW_DAYS = projecttoml_graph.CASCADE_WINDOW_DAYS

# The package graph only becomes meaningful once the packaging effort is
# under way; earlier quarters have a handful of unrelated commits.
GRAPH_START_QUARTER = "2021-Q2"


def load_commits() -> pd.DataFrame:
    commits = projecttoml_graph.load_commits()
    return pd.DataFrame(
        [
            {
                "repo": c.repo,
                "date": c.date,
                "quarter": projecttoml_graph.quarter_of(c.date),
                "message": c.message,
                "release": projecttoml_graph.is_release(c.message),
                "dep_update": projecttoml_graph.is_dep_update(c.message),
            }
            for c in commits
        ]
    )


def main() -> None:
    commits = load_commits()
    commits = commits[
        commits["quarter"] >= GRAPH_START_QUARTER
    ].reset_index(drop=True)

    repos = sorted(commits["repo"].unique())
    quarters = sorted(commits["quarter"].unique())

    # --- Aggregate table: one row per (repo, quarter, kind) -------------
    # "releases" are version bumps; "dep_updates" are compat/manifest
    # changes. Splitting them is what separates a package's own cadence
    # from the cascade it is being dragged along by.
    kind_rows = []
    for (repo, quarter), group in commits.groupby(["repo", "quarter"]):
        kind_rows.append(
            {
                "repo": repo,
                "quarter": quarter,
                "n_releases": int(group["release"].sum()),
                "n_dep_updates": int(group["dep_update"].sum()),
                "n_commits": len(group),
            }
        )
    agg = pd.DataFrame(kind_rows)

    # Dense per-repo quarterly commit counts for the correlation matrix.
    wide = (
        commits.groupby(["quarter", "repo"])
        .size()
        .unstack(fill_value=0)
        .reindex(quarters, fill_value=0)
        .reindex(columns=repos, fill_value=0)
    )

    # Pearson correlation on log1p counts: monotone activity, robust to a
    # single busy quarter dominating the scale.
    corr = np.log1p(wide).corr()

    # Long form of the pairwise correlations, upper triangle only.
    pair_rows = []
    for i, a in enumerate(repos):
        for b in repos[i + 1 :]:
            pair_rows.append(
                {
                    "repo_a": a,
                    "repo_b": b,
                    "corr": float(corr.loc[a, b]),
                }
            )
    pairs = pd.DataFrame(pair_rows).sort_values(
        "corr", ascending=False
    ).reset_index(drop=True)

    # --- Dependency graph edges -----------------------------------------
    edge_counts = projecttoml_graph.dependency_edges(
        projecttoml_graph.load_commits()
    )

    # --- Write the results table ----------------------------------------
    os.makedirs(ROOT / "results", exist_ok=True)
    out = agg.sort_values(["repo", "quarter"]).reset_index(drop=True)
    out.to_csv(ROOT / "results/repo-cochange.csv", index=False)

    # --- Figure ----------------------------------------------------------
    palette = [
        "#1F77B4", "#EDB120", "#D62728", "#2CA02C",
        "#9467BD", "#8C564B", "#17BECF", "#E377C2",
    ]
    color_for = {r: palette[i % len(palette)] for i, r in enumerate(repos)}

    # Cascade timeline: of the dependency updates in each quarter, what
    # fraction landed within the cascade window of a release in another
    # package. If releases drive the coupling, most updates should be
    # cascades, which is what makes "changes together" a release story
    # rather than a claim that the repos otherwise move in lockstep.
    all_commits = projecttoml_graph.load_commits()
    rels = projecttoml_graph.releases(all_commits)
    timeline_rows = []
    for c in all_commits:
        if not projecttoml_graph.is_dep_update(c.message):
            continue
        q = projecttoml_graph.quarter_of(c.date)
        if q < GRAPH_START_QUARTER:
            continue
        cascade = False
        for src, times in rels.items():
            if src == c.repo:
                continue
            prior = [t for t in times if t <= c.date]
            if prior and (c.date - max(prior)).days <= CASCADE_WINDOW_DAYS:
                cascade = True
                break
        timeline_rows.append({"quarter": q, "cascade": cascade})
    timeline = pd.DataFrame(timeline_rows)
    tl = (
        timeline.groupby("quarter")
        .agg(n_updates=("cascade", "size"), n_cascade=("cascade", "sum"))
        .reset_index()
    )
    tl["frac_cascade"] = tl["n_cascade"] / tl["n_updates"]

    # A Sankey of the dependency graph. Dependents on the left, their
    # dependencies on the right, link width by how much evidence the
    # Project.toml history gives for that edge. A package with many thick
    # incoming links is a shared foundation (most of the stack sits on
    # ClimaCore and ClimaParams); one with many outgoing links is a
    # top-level app that composes the rest. This encodes the graph more
    # readably than a node-link plot, which turns into a hairball at this
    # many edges.
    deps_of: dict[str, set[str]] = {r: set() for r in repos}
    n_dependents: dict[str, int] = {r: 0 for r in repos}
    for (src, dst) in edge_counts:
        if src in deps_of and dst in deps_of:
            deps_of[src].add(dst)
            n_dependents[dst] += 1

    # Order left nodes by how many dependencies they pull in (apps at the
    # top), right nodes by how depended-upon they are (foundations at the
    # bottom), so the flow reads as a gradient from app to foundation.
    left_order = sorted(repos, key=lambda r: (-len(deps_of[r]), r))
    right_order = sorted(repos, key=lambda r: (n_dependents[r], r))

    labels = [f"{r}  " for r in left_order] + [
        f"  {r}" for r in right_order
    ]
    left_index = {r: i for i, r in enumerate(left_order)}
    right_index = {r: len(left_order) + i for i, r in enumerate(right_order)}

    link_src, link_dst, link_val, link_color = [], [], [], []
    for (src, dst), count in sorted(
        edge_counts.items(), key=lambda x: (-x[1], x[0])
    ):
        if src not in left_index or dst not in right_index:
            continue
        link_src.append(left_index[src])
        link_dst.append(right_index[dst])
        link_val.append(count)
        # Color each link by its dependent, so one package's fan-out reads
        # as a single hue.
        hex_color = color_for[src]
        r_, g_, b_ = (
            int(hex_color[1:3], 16),
            int(hex_color[3:5], 16),
            int(hex_color[5:7], 16),
        )
        link_color.append(f"rgba({r_},{g_},{b_},0.45)")

    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(
                label=labels,
                color=[color_for[r] for r in left_order]
                + [color_for[r] for r in right_order],
                pad=8,
                thickness=14,
                line=dict(color="white", width=0.5),
                hovertemplate="%{label}<extra></extra>",
            ),
            link=dict(
                source=link_src,
                target=link_dst,
                value=link_val,
                color=link_color,
                hovertemplate=(
                    "%{source.label} depends on %{target.label}"
                    "<br>%{value} release-linked updates<extra></extra>"
                ),
            ),
            textfont=dict(size=10),
        )
    )

    fig.update_layout(
        title="Which CliMA repos change together",
        height=680,
        margin=dict(t=70, b=30, l=30, r=30),
    )

    os.makedirs(ROOT / "figures", exist_ok=True)
    fig.write_json(ROOT / "figures/repo-cochange.json")

    # Values the question's answer cites, written as their own results file
    # so the numbers come from the pipeline rather than being retyped.
    q1 = pairs[pairs["corr"] == pairs["corr"].max()].iloc[0]
    n_updates = int(tl["n_updates"].sum())
    n_cascade = int(tl["n_cascade"].sum())
    upstream_atmos = sorted(deps_of["ClimaAtmos"])
    summary = {
        "strongest_pair": f"{q1['repo_a']} and {q1['repo_b']}",
        "strongest_corr": round(float(q1["corr"]), 2),
        "n_dep_updates": n_updates,
        "n_cascade_updates": n_cascade,
        "frac_cascade": round(n_cascade / n_updates, 2),
        "cascade_window_days": CASCADE_WINDOW_DAYS,
        "upstream_of_climaatmos": upstream_atmos,
        "n_upstream_of_climaatmos": len(upstream_atmos),
    }
    with open(ROOT / "results/repo-cochange-summary.json", "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    # A small machine-readable summary the question's answer can cite.
    frac = float(tl["n_cascade"].sum() / tl["n_updates"].sum())
    print(
        f"strongest co-change: {q1['repo_a']} vs {q1['repo_b']} "
        f"(r = {q1['corr']:.2f})"
    )
    print(
        f"dependency updates following a release within "
        f"{CASCADE_WINDOW_DAYS} days: {tl['n_cascade'].sum()}/"
        f"{tl['n_updates'].sum()} ({frac:.0%})"
    )
    print(
        "upstream dependencies of ClimaAtmos: "
        + ", ".join(sorted(deps_of["ClimaAtmos"]))
    )
    print(f"wrote {len(out)} rows to results/repo-cochange.csv")
    print("wrote figures/repo-cochange.json")


if __name__ == "__main__":
    main()
