"""Shared helpers for analyzing CliMA's package dependency graph.

The ``projecttoml`` data from ``fetch-github`` gives, per tracked repo, the
history of ``Project.toml`` commits. Those commits are either *releases*
(a version bump) or *dependency updates* (a compat-bounds or manifest
change, often a CompatHelper commit). Reading the two together lets us see
the release cascade: when one package tags a new version, its dependents
pull it in and re-tag, which can then ripple out to their own dependents.

This module turns that history into two things the other scripts use:

- ``dependency_edges``: a static directed dependency graph, where an edge
  ``A -> B`` means "A depends on B". Derived from commit messages such as
  "use ClimaCore v0.14.46" or by matching a release in ``B`` against a
  dependency-update commit shortly after in ``A``.
- ``cochange_pairs``: per-quarter pairs of repos that touched
  ``Project.toml`` in the same short window, which is where clusters of
  changes show up.

The files are small, so this module keeps everything in plain Python
rather than pulling in a heavy dependency.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTTOML_ROOT = REPO_ROOT / "data" / "github" / "projecttoml"

# Anything before 2018-Q1 predates the CliMA packaging effort, matching the
# other analyses.
MIN_QUARTER = "2018-Q1"

# A dependency update landing within this many days after a release in
# another package counts as a cascade link.
CASCADE_WINDOW_DAYS = 14


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def quarter_of(dt: datetime) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def camel(name: str) -> str:
    """Canonical CliMA package name from a lowercase on-disk repo name."""
    aliases = {
        "climaatmos": "ClimaAtmos",
        "climacore": "ClimaCore",
        "climaparams": "ClimaParams",
        "thermodynamics": "Thermodynamics",
        "cloudmicrophysics": "CloudMicrophysics",
        "climaland": "ClimaLand",
        "climacoupler": "ClimaCoupler",
        "climaanalysis": "ClimaAnalysis",
        "climautilities": "ClimaUtilities",
        "climadiagnostics": "ClimaDiagnostics",
        "climacalibrate": "ClimaCalibrate",
        "climaocean": "ClimaOcean",
        "oceananigans": "Oceananigans",
        "climaseaice": "ClimaSeaIce",
        "ensemblekalmanprocesses": "EnsembleKalmanProcesses",
        "calibrateemulatesample": "CalibrateEmulateSample",
        "surfacefluxes": "SurfaceFluxes",
        "insolation": "Insolation",
        "climatimesteppers": "ClimaTimeSteppers",
        "climacomms": "ClimaComms",
        "rrtmgp": "RRTMGP",
        "climaearth": "ClimaEarth",
    }
    return aliases.get(name.lower(), name)


@dataclass(frozen=True)
class Commit:
    repo: str
    date: datetime
    message: str
    sha: str


# A version bump, e.g. "Bump patch version", "Make minor release (#4228)",
# "tag v0.14.46", "release 1.5.0 and update tomls", "Patch release 0.16.2".
# Deliberately does not match a bare merge or feature commit, so a merge
# that happens to mention "v0.21.0" or a release branch name is not counted.
RELEASE_RE = re.compile(
    r"\bbump (?:the )?(?:patch|minor|major)?\s*version\b"
    r"|\bbump (?:patch|minor|major)\b"
    r"|\bmake (?:a )?(?:patch|minor|major) release\b"
    r"|\bversion for (?:a )?(?:new )?release\b"
    r"|\btag(?:/)? v?\d"
    r"|\btag release\b"
    r"|\brelease v?\d"
    r"|\b(?:patch|minor|major) release\b"
    r"|\bnew release\b"
    r"|\brelease [0-9]",
    re.I,
)

# A dependency update: compat bounds, a manifest, or a CompatHelper commit.
DEP_UPDATE_RE = re.compile(
    r"compat|bump compat|update .*compat|dependencies|project\.toml|"
    r"compathelper|update tomls?|use .* v\d|update to .*v\d",
    re.I,
)

# A *mechanical* dependency update: bounds bookkeeping that a bundling of
# the packages into one repo would absorb into the triggering change
# itself. CompatHelper commits, compat-entry edits, manifest/.toml
# refreshes. The rest -- interface adaptation, a feature that pulled the
# new version in -- is substantive and would still be work after
# co-location, just done in a different repo.
MECHANICAL_RE = re.compile(
    r"compathelper|compat entry|bump compat|widen compat|"
    r"update tomls?|update project\.toml|update manifests?|"
    r"bump .*compat|project\.toml|update dependenc",
    re.I,
)


def is_release(message: str) -> bool:
    return bool(RELEASE_RE.search(message))


def is_dep_update(message: str) -> bool:
    if is_release(message):
        return False
    return bool(DEP_UPDATE_RE.search(message))


def is_mechanical_update(message: str) -> bool:
    """Whether a dependency update is bounds bookkeeping rather than work
    that adapts to the new version."""
    return is_dep_update(message) and bool(MECHANICAL_RE.search(message))


def load_commits(root: Path | None = None) -> list[Commit]:
    """Every tracked Project.toml commit, sorted by date."""
    root = root or PROJECTTOML_ROOT
    commits: list[Commit] = []
    for path in sorted(root.glob("*.jsonl")):
        repo = camel(path.stem)
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            dt = parse_iso(row["commit_date"])
            if quarter_of(dt) < MIN_QUARTER:
                continue
            commits.append(
                Commit(
                    repo=repo,
                    date=dt,
                    message=row.get("message") or "",
                    sha=row.get("sha") or "",
                )
            )
    commits.sort(key=lambda c: c.date)
    return commits


def releases(commits: list[Commit]) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = defaultdict(list)
    for c in commits:
        if is_release(c.message):
            out[c.repo].append(c.date)
    return out


def cascade_links(
    commits: list[Commit], window_days: int = CASCADE_WINDOW_DAYS
) -> dict[tuple[str, str], int]:
    """Count directed links ``source -> target``: a dependency update in
    ``target`` landing within ``window_days`` after a release in ``source``.

    Only the nearest preceding release is counted, so a long gap between a
    release and an unrelated change is not mistaken for a cascade, and a
    single update is not attributed to every release before it.
    """
    rels = releases(commits)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in commits:
        if not is_dep_update(c.message):
            continue
        for src, times in rels.items():
            if src == c.repo:
                continue
            prior = [t for t in times if t <= c.date]
            if not prior:
                continue
            nearest = max(prior)
            if (c.date - nearest) <= timedelta(days=window_days):
                counts[(src, c.repo)] += 1
    return counts


@dataclass(frozen=True)
class CascadeEvent:
    """One repo-day of dependency propagation attributed to a release.

    A release often triggers several updates in a dependent that all land
    the same day and merge as a single PR, so events are grouped by
    (repo, day) rather than counted per commit.
    """

    repo: str
    date: datetime
    source: str
    mechanical: bool
    n_commits: int


def cascade_events(
    commits: list[Commit], window_days: int = CASCADE_WINDOW_DAYS
) -> list[CascadeEvent]:
    """The dependency updates that followed a release, grouped into events.

    Each is attributed to the nearest preceding release in another package
    within the window. A repo-day counts as mechanical only if every commit
    that day is mechanical: one substantive commit means the update was
    real work, not just bounds bookkeeping.
    """
    rels = releases(commits)
    by_day: dict[tuple[str, object], list[Commit]] = defaultdict(list)
    source_of: dict[tuple[str, object], str] = {}
    for c in commits:
        if not is_dep_update(c.message):
            continue
        best_src = None
        best_lag = None
        for src, times in rels.items():
            if src == c.repo:
                continue
            prior = [t for t in times if t <= c.date]
            if not prior:
                continue
            lag = c.date - max(prior)
            if lag <= timedelta(days=window_days) and (
                best_lag is None or lag < best_lag
            ):
                best_lag = lag
                best_src = src
        if best_src is None:
            continue
        key = (c.repo, c.date.date())
        by_day[key].append(c)
        source_of.setdefault(key, best_src)

    events = []
    for key, members in by_day.items():
        members.sort(key=lambda c: c.date)
        events.append(
            CascadeEvent(
                repo=key[0],
                date=members[0].date,
                source=source_of[key],
                mechanical=all(is_mechanical_update(c.message) for c in members),
                n_commits=len(members),
            )
        )
    events.sort(key=lambda e: (e.date, e.repo))
    return events


# Known CliMA package names, used to read a dependency out of a commit
# message like "update ClimaCore to v0.14.46" or "update to the new
# Thermodynamics.jl interface".
PACKAGE_NAMES = tuple(
    sorted(
        {
            "ClimaAtmos",
            "ClimaCore",
            "ClimaLand",
            "ClimaParams",
            "CloudMicrophysics",
            "Thermodynamics",
            "ClimaCoupler",
            "ClimaUtilities",
            "ClimaDiagnostics",
            "ClimaAnalysis",
            "SurfaceFluxes",
            "Insolation",
            "ClimaTimeSteppers",
            "ClimaComms",
            "RRTMGP",
            "ClimaOcean",
            "ClimaSeaIce",
            "EnsembleKalmanProcesses",
            "CalibrateEmulateSample",
            "ClimaCalibrate",
            "RootSolvers",
            "Makie",
        },
        key=len,
        reverse=True,
    )
)

_MENTION_LEAD = re.compile(
    r"\b(update|upgrade|use|bump|compat|pin|widen|add compat|remove compat"
    r"|support for|to the new)\b",
    re.I,
)


def explicit_dependency_edges(
    commits: list[Commit],
) -> dict[tuple[str, str], int]:
    """Directed dependency edges read out of commit messages.

    A commit in repo ``A`` whose message mentions another CliMA package
    ``B`` in a dependency-update context ("update ClimaCore", "use the new
    Thermodynamics interface", "bump compat for ClimaParams") is evidence
    that ``A -> B``. This is more trustworthy than pure timing because the
    message says which package changed, and it is what lets a layered
    layout put the low-level packages underneath the ones that consume
    them.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in commits:
        if not _MENTION_LEAD.search(c.message):
            continue
        if not is_dep_update(c.message):
            continue
        for name in PACKAGE_NAMES:
            if name == c.repo:
                continue
            if re.search(r"\b" + re.escape(name) + r"\b", c.message, re.I):
                counts[(c.repo, name)] += 1
    return counts


def dependency_edges(
    commits: list[Commit],
) -> dict[tuple[str, str], int]:
    """Directed edges ``A -> B`` meaning A depends on B.

    Prefers evidence from commit messages (``explicit_dependency_edges``),
    which names the package that changed. A real dependency can only point
    one way, so where the messages suggest both directions between a pair
    the weaker one is dropped: "update ClimaCore" inside ClimaAtmos is
    strong evidence, while a stray "update ClimaAtmos" inside ClimaCore is
    almost always a mention rather than a dependency. Ties are dropped too,
    since neither direction is convincing.
    """
    edges = explicit_dependency_edges(commits)
    if not edges:
        return cascade_links(commits)
    resolved: dict[tuple[str, str], int] = {}
    for (src, dst), count in edges.items():
        reverse = edges.get((dst, src), 0)
        if count > reverse:
            resolved[(src, dst)] = count
    return resolved


def cochange_pairs(
    commits: list[Commit], window_days: int = CASCADE_WINDOW_DAYS
) -> dict[str, set[tuple[str, str]]]:
    """Per-quarter unordered pairs of repos that touched Project.toml within
    ``window_days`` of each other.

    A pair is filed under the quarter of the earlier commit, so a cascade
    that spills across a quarter boundary stays attached to when it began.
    """
    by_quarter: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for i, a in enumerate(commits):
        for b in commits[i + 1 :]:
            if (b.date - a.date) > timedelta(days=window_days):
                break
            if a.repo == b.repo:
                continue
            pair = tuple(sorted((a.repo, b.repo)))
            by_quarter[quarter_of(a.date)].add(pair)
    return by_quarter


def hhi(counts: list[int]) -> float:
    """Herfindahl-Hirschman index of a set of counts, in [0, 1].

    1.0 means every co-change involved the same pair of repos (one tight
    cluster); values near 0 mean changes are spread across many pairs. This
    is the single number used to say how concentrated the clusters are in a
    given quarter.
    """
    total = sum(counts)
    if total == 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)
