"""Trace one concrete release cascade, to show the mechanism the
co-change analysis is measuring.

Pick the release whose downstream pull-ins are the clearest and print
the chain: a release in one package, the dependency updates it triggers
in others within the cascade window, and the releases those packages then
cut. The picked example is resolved from the data rather than hard-coded,
so it stays correct as more history arrives.

Writes ``results/example-release-cascade.json``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
projecttoml_graph = import_module("projecttoml-graph")

ROOT = Path.cwd()
WINDOW_DAYS = projecttoml_graph.CASCADE_WINDOW_DAYS


def main() -> None:
    commits = projecttoml_graph.load_commits()
    rels = projecttoml_graph.releases(commits)

    # Score each release by how many distinct other packages pushed a
    # dependency update within the window and, of those, how many cut a
    # release of their own. Prefer a source whose first line names the
    # release (so the example reads cleanly), then the widest fan-out,
    # then the most recent release.
    #
    # Only consider the last two years: early history has a few very busy
    # quarters whose cascades span the whole stack, but the example is
    # meant to show how the project works now.
    newest = max(c.date for c in commits)
    recent_cutoff = newest - timedelta(days=730)
    best = None
    for c in commits:
        if c.date < recent_cutoff:
            continue
        if not projecttoml_graph.is_release(c.message):
            continue
        first_line = c.message.splitlines()[0]
        names_release = bool(projecttoml_graph.RELEASE_RE.search(first_line))
        followers = {}
        for other in commits:
            if other.repo == c.repo:
                continue
            if not projecttoml_graph.is_dep_update(other.message):
                continue
            if not (timedelta(0) < (other.date - c.date) <= timedelta(days=WINDOW_DAYS)):
                continue
            followers.setdefault(other.repo, []).append(other)
        n_released = sum(
            1
            for repo in followers
            if any(
                0 <= (t - c.date).days <= WINDOW_DAYS
                for t in rels.get(repo, [])
            )
        )
        key = (
            0 if names_release else 1,
            -len(followers),
            -n_released,
            -c.date.timestamp(),
        )
        if best is None or key < best[0]:
            best = (key, c, followers)

    _, source, followers = best

    # For each downstream package, list its updates and whether it then
    # cut its own release inside the window.
    downstream = []
    for repo, updates in sorted(followers.items()):
        releases_after = [
            t
            for t in rels.get(repo, [])
            if 0 <= (t - source.date).days <= WINDOW_DAYS
        ]
        downstream.append(
            {
                "repo": repo,
                "updates": [
                    {
                        "date": u.date.date().isoformat(),
                        "message": u.message.splitlines()[0],
                    }
                    for u in sorted(updates, key=lambda u: u.date)
                ],
                "released_after": bool(releases_after),
            }
        )

    out = {
        "source_repo": source.repo,
        "source_date": source.date.date().isoformat(),
        "source_message": source.message.splitlines()[0],
        "window_days": WINDOW_DAYS,
        "n_downstream_repos": len(downstream),
        "n_downstream_repos_that_released": sum(
            1 for d in downstream if d["released_after"]
        ),
        "downstream": downstream,
    }

    os.makedirs(ROOT / "results", exist_ok=True)
    with open(ROOT / "results/example-release-cascade.json", "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)

    print(
        f"{source.repo} {source.date.date()} '{out['source_message']}' "
        f"-> {out['n_downstream_repos']} downstream repos updated, "
        f"{out['n_downstream_repos_that_released']} released"
    )
    print("wrote results/example-release-cascade.json")


if __name__ == "__main__":
    main()
