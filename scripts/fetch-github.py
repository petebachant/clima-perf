"""Incrementally fetch GitHub data for CliMA org repos.

For each repo and data type we:

- Pick up where the previous run left off (resumable, like the buildkite
  notebook does with its ``finished_from`` timestamp).
- Only fetch data whose timestamp is strictly before the start of the
  *current* UTC day, so every batch we commit represents complete UTC days.

This is meant to run nightly on GitHub Actions, where ``GH_TOKEN`` is set
from ``secrets.GITHUB_TOKEN`` and ``gh`` is therefore already authenticated.
Locally the user runs ``gh auth login`` once (the calkit setup dependency
checks this).

Data layout (everything under ``data/github/``)::

    prs/<repo>/<YYYY-MM-DD>.json     # merged PRs that UTC day (omitted if 0)
    prs/<repo>/.last_fetched         # latest UTC day fully fetched
    issues/<repo>/<YYYY-MM-DD>.json  # issues created that UTC day (omitted if 0)
    issues/<repo>/.last_fetched
    repo_stats/<YYYY-MM-DD>.jsonl    # one snapshot row per repo per UTC day
    forks/<repo>.jsonl               # append-only, deduped by fork full_name
    releases/<repo>.jsonl            # append-only, deduped by release id
    projecttoml/<repo>.jsonl         # append-only, deduped by commit SHA
    repo_creation_dates.jsonl        # refreshed each run (static-ish)
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "data" / "github"

# Fetch up to (but not including) start of the current UTC day.
TODAY_UTC = datetime.now(timezone.utc).date()
LAST_DAY = TODAY_UTC - timedelta(days=1)
TODAY_UTC_START_ISO = f"{TODAY_UTC.isoformat()}T00:00:00Z"

# First-time backfill start when a repo has no prior data.
BACKFILL_START = date(2018, 1, 1)

PR_FIELDS = (
    "number,title,body,author,createdAt,mergedAt,additions,deletions,"
    "changedFiles,labels,closingIssuesReferences"
)
ISSUE_FIELDS = "number,title,author,createdAt,labels"

# Repos with detailed PR pulls (the team-relevant set from clima-analysis).
PR_REPOS = [
    "ClimaAtmos", "ClimaCoupler", "ClimaLand", "ClimaCore", "Thermodynamics",
    "CloudMicrophysics", "ClimaParams", "ClimaOcean", "EnsembleKalmanProcesses",
    "CalibrateEmulateSample", "ClimaCalibrate", "ClimaUtilities",
    "ClimaDiagnostics", "ClimaAnalysis", "SurfaceFluxes", "Insolation",
    "RRTMGP", "Oceananigans", "ClimaSeaIce", "ClimaTimeSteppers", "ClimaComms",
    "ClimaEarth",
]

# Broader set used for daily stats + fork/release snapshots.
STATS_REPOS = [
    "ClimaAtmos", "ClimaCore", "ClimaParams", "Thermodynamics", "CloudMicrophysics",
    "ClimaCoupler", "ClimaLand", "ClimaOcean", "SurfaceFluxes", "Insolation",
    "RRTMGP", "ClimaUtilities", "ClimaDiagnostics", "ClimaAnalysis",
    "Oceananigans", "ClimaSeaIce", "SeawaterPolynomials", "CubedSphere",
    "ClimaTimeSteppers", "ClimaComms", "UnrolledUtilities", "MultiBroadcastFusion",
    "LazyBroadcast", "NullBroadcasts", "StructuredPrinting", "OperatorFlux",
    "ClimaInterpolations", "RootSolvers", "ArtifactWrappers",
    "ClimaEarth", "ClimaViz", "RandomFeatures",
    "EnsembleKalmanProcesses", "CalibrateEmulateSample", "ClimaCalibrate",
]

# Issues subset (external-use signal — same set clima-analysis uses).
ISSUE_REPOS = [
    "ClimaAtmos", "ClimaCore", "ClimaParams", "Thermodynamics", "CloudMicrophysics",
    "SurfaceFluxes", "Insolation", "Oceananigans", "ClimaTimeSteppers", "ClimaSeaIce",
]

# Repos whose Project.toml commit history we track for propagation analysis.
PROJECTTOML_REPOS = [
    "ClimaAtmos", "ClimaCore", "ClimaParams", "Thermodynamics",
    "CloudMicrophysics", "ClimaLand",
]


# --- gh CLI helpers ---------------------------------------------------------

def gh(*args: str) -> str:
    """Run gh CLI; return stdout. Raises CalledProcessError on failure."""
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True,
    ).stdout


def gh_api_paginated(path: str) -> list:
    """Run ``gh api --paginate`` against an array endpoint, returning a single
    merged list. ``gh`` concatenates each page's JSON array to stdout; we
    parse them off one by one with a streaming JSON decoder.
    """
    out = gh("api", "--paginate", path).strip()
    if not out:
        return []
    merged: list = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(out):
        obj, end = decoder.raw_decode(out, idx)
        if isinstance(obj, list):
            merged.extend(obj)
        else:
            merged.append(obj)
        idx = end
        while idx < len(out) and out[idx].isspace():
            idx += 1
    return merged


# --- file helpers -----------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def write_jsonl(path: Path, items: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for item in items:
            f.write(json.dumps(item, sort_keys=True))
            f.write("\n")


def read_marker(path: Path) -> date | None:
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text().strip())
    except ValueError:
        return None


def write_marker(path: Path, d: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(d.isoformat() + "\n")


# --- generic per-day search fetcher -----------------------------------------

def fetch_search_chunked(
    repo: str,
    kind: str,            # "pr" or "issue"
    qualifier: str,       # "merged" or "created"
    fields: str,
    out_subdir: str,
) -> None:
    """Fetch all events matching the time qualifier and bucket them into
    per-UTC-day files.

    We issue at most one ``gh`` call per (repo, calendar year) so a 6-12 month
    backfill is one or two calls per repo instead of one per day. Per-day
    files keep storage aligned with the user-visible chunk semantics.
    """
    repo_lc = repo.lower()
    out_dir = DATA_ROOT / out_subdir / repo_lc
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".last_fetched"

    last = read_marker(marker)
    start_day = (last + timedelta(days=1)) if last else BACKFILL_START
    if start_day > LAST_DAY:
        return

    # Iterate by calendar-year chunks. Search API silently truncates above
    # ~1000 results; capping at a year keeps every CliMA repo well under that.
    cur = start_day
    while cur <= LAST_DAY:
        year_end = date(cur.year, 12, 31)
        chunk_end = min(year_end, LAST_DAY)
        query = f"{qualifier}:{cur.isoformat()}..{chunk_end.isoformat()}"

        if kind == "pr":
            cmd = [
                "pr", "list", "--repo", f"CliMA/{repo}.jl",
                "--state", "merged",
                "--search", query,
                "--limit", "1000",
                "--json", fields,
            ]
        else:
            cmd = [
                "issue", "list", "--repo", f"CliMA/{repo}.jl",
                "--state", "all",
                "--search", query,
                "--limit", "1000",
                "--json", fields,
            ]
        try:
            items = json.loads(gh(*cmd))
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: {kind} {repo} {query}: {tail[0]}", file=sys.stderr)
            return
        if len(items) >= 1000:
            print(
                f"  WARN: {kind} {repo} {query} hit 1000-result cap; some "
                "rows may be missing.",
                file=sys.stderr,
            )

        ts_field = "mergedAt" if kind == "pr" else "createdAt"
        by_day: dict[date, list[dict]] = defaultdict(list)
        for it in items:
            ts = it.get(ts_field)
            if not ts:
                continue
            try:
                d = date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if cur <= d <= chunk_end:
                by_day[d].append(it)

        n_events = 0
        for d, evs in by_day.items():
            evs.sort(key=lambda e: e.get(ts_field) or "")
            (out_dir / f"{d.isoformat()}.json").write_text(
                json.dumps(evs, indent=2, sort_keys=True)
            )
            n_events += len(evs)
        write_marker(marker, chunk_end)
        print(f"  {kind} {repo} {cur}..{chunk_end}: {n_events} events")

        cur = chunk_end + timedelta(days=1)


def fetch_prs() -> None:
    print("→ PRs (per-day, year-chunked queries)")
    for repo in PR_REPOS:
        fetch_search_chunked(repo, "pr", "merged", PR_FIELDS, "prs")


def fetch_issues() -> None:
    print("→ Issues (per-day, year-chunked queries)")
    for repo in ISSUE_REPOS:
        fetch_search_chunked(repo, "issue", "created", ISSUE_FIELDS, "issues")


# --- daily snapshot ---------------------------------------------------------

def fetch_repo_stats() -> None:
    """One JSONL file per UTC day with star/watcher/fork/issue counts for
    every repo in STATS_REPOS. Treats the snapshot as "as of end of LAST_DAY"."""
    print(f"→ Repo stats snapshot for {LAST_DAY}")
    out_dir = DATA_ROOT / "repo_stats"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{LAST_DAY.isoformat()}.jsonl"
    if out_path.exists():
        print(f"  already have {out_path.name}")
        return
    rows = []
    for repo in STATS_REPOS:
        try:
            out = gh(
                "api", f"repos/CliMA/{repo}.jl",
                "--jq",
                "{name: .name, stars: .stargazers_count, "
                "watchers: .subscribers_count, forks: .forks_count, "
                "open_issues: .open_issues_count}",
            ).strip()
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: stats {repo}: {tail[0]}", file=sys.stderr)
            continue
        if out:
            row = json.loads(out)
            row["snapshot_date"] = LAST_DAY.isoformat()
            rows.append(row)
    write_jsonl(out_path, rows)
    print(f"  wrote {len(rows)} rows to {out_path.name}")


# --- append-only paginated data ---------------------------------------------

def fetch_forks() -> None:
    """Forks per repo (append-only JSONL, deduped by ``full_name``).

    We skip forks created during the current UTC day so each commit reflects
    only complete days.
    """
    print("→ Forks (append-only)")
    base_dir = DATA_ROOT / "forks"
    for repo in STATS_REPOS:
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("fork_full_name") for e in existing}
        try:
            pages = gh_api_paginated(f"repos/CliMA/{repo}.jl/forks")
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: forks {repo}: {tail[0]}", file=sys.stderr)
            continue
        new = 0
        for f in pages:
            full_name = f.get("full_name")
            if not full_name or full_name in seen:
                continue
            created = f.get("created_at") or ""
            if created and created >= TODAY_UTC_START_ISO:
                continue
            existing.append({
                "repo": f"{repo}.jl",
                "fork_full_name": full_name,
                "fork_owner": (f.get("owner") or {}).get("login"),
                "fork_owner_type": (f.get("owner") or {}).get("type"),
                "fork_created": created,
                "fork_pushed": f.get("pushed_at"),
            })
            seen.add(full_name)
            new += 1
        if new:
            existing.sort(key=lambda e: e.get("fork_created") or "")
            write_jsonl(out_path, existing)
        print(f"  forks {repo}: +{new} (total {len(existing)})")


def fetch_releases() -> None:
    """Releases per repo (append-only JSONL, deduped by release id)."""
    print("→ Releases (append-only)")
    base_dir = DATA_ROOT / "releases"
    for repo in STATS_REPOS:
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("id") for e in existing}
        try:
            pages = gh_api_paginated(
                f"repos/CliMA/{repo}.jl/releases?per_page=100"
            )
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: releases {repo}: {tail[0]}", file=sys.stderr)
            continue
        new = 0
        for r in pages:
            rid = r.get("id")
            if rid is None or rid in seen:
                continue
            pub = r.get("published_at") or ""
            if pub and pub >= TODAY_UTC_START_ISO:
                continue
            existing.append({
                "id": rid,
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "draft": r.get("draft"),
                "prerelease": r.get("prerelease"),
                "created_at": r.get("created_at"),
                "published_at": pub,
            })
            seen.add(rid)
            new += 1
        if new:
            existing.sort(key=lambda e: e.get("published_at") or "")
            write_jsonl(out_path, existing)
        print(f"  releases {repo}: +{new} (total {len(existing)})")


def fetch_projecttoml_commits() -> None:
    """Project.toml commit history per repo (append-only JSONL, deduped by SHA).

    Uses the REST ``since=`` query so we only download commits newer than what
    we already have, which is much cheaper than re-paginating a multi-year
    history every night.
    """
    print("→ Project.toml commits (incremental via ?since=)")
    base_dir = DATA_ROOT / "projecttoml"
    for repo in PROJECTTOML_REPOS:
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("sha") for e in existing}
        latest_ts = max(
            (e.get("commit_date") or "" for e in existing), default="",
        )
        params = ["path=Project.toml", "per_page=100"]
        if latest_ts:
            params.append(f"since={latest_ts}")
        try:
            pages = gh_api_paginated(
                f"repos/CliMA/{repo}.jl/commits?{'&'.join(params)}"
            )
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: projecttoml {repo}: {tail[0]}", file=sys.stderr)
            continue
        new = 0
        for c in pages:
            sha = c.get("sha")
            if not sha or sha in seen:
                continue
            commit = c.get("commit") or {}
            cd = (commit.get("committer") or {}).get("date") or ""
            if cd and cd >= TODAY_UTC_START_ISO:
                continue
            existing.append({
                "sha": sha,
                "commit_date": cd,
                "author_date": (commit.get("author") or {}).get("date"),
                "author_login": (c.get("author") or {}).get("login"),
                "committer_login": (c.get("committer") or {}).get("login"),
                "message": commit.get("message"),
            })
            seen.add(sha)
            new += 1
        if new:
            existing.sort(key=lambda e: e.get("commit_date") or "")
            write_jsonl(out_path, existing)
        print(f"  projecttoml {repo}: +{new} (total {len(existing)})")


# --- static-ish -------------------------------------------------------------

def fetch_stargazers() -> None:
    """Per-repo stargazer events (append-only JSONL, deduped by login).

    GitHub returns starred_at when we send the ``vnd.github.v3.star+json``
    Accept header. We paginate the whole list each run and dedup against
    what we already have; stars whose ``starred_at`` is in the current
    UTC day are excluded so we only ever commit complete days.

    Worth noting: a user who unstars and re-stars will keep their original
    entry (we dedup by login). For trend analysis at quarterly granularity
    this is fine.
    """
    print("→ Stargazers (append-only)")
    base_dir = DATA_ROOT / "stargazers"
    for repo in STATS_REPOS:
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("login") for e in existing}
        try:
            out = gh(
                "api", "--paginate",
                "-H", "Accept: application/vnd.github.v3.star+json",
                f"repos/CliMA/{repo}.jl/stargazers?per_page=100",
            ).strip()
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: stargazers {repo}: {tail[0]}", file=sys.stderr)
            continue
        pages: list = []
        if out:
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(out):
                obj, end = decoder.raw_decode(out, idx)
                if isinstance(obj, list):
                    pages.extend(obj)
                else:
                    pages.append(obj)
                idx = end
                while idx < len(out) and out[idx].isspace():
                    idx += 1
        new = 0
        for s in pages:
            user = s.get("user") or {}
            login = user.get("login")
            if not login or login in seen:
                continue
            starred = s.get("starred_at") or ""
            if starred and starred >= TODAY_UTC_START_ISO:
                continue
            existing.append({
                "repo": f"{repo}.jl",
                "login": login,
                "user_type": user.get("type"),
                "starred_at": starred,
            })
            seen.add(login)
            new += 1
        if new:
            existing.sort(key=lambda e: e.get("starred_at") or "")
            write_jsonl(out_path, existing)
        print(f"  stargazers {repo}: +{new} (total {len(existing)})")


def fetch_clima_members() -> None:
    """Snapshot the CliMA GitHub org's members for the internal-user filter.

    Tries the authenticated ``orgs/CliMA/members`` endpoint first — when the
    caller's token belongs to a CliMA member this returns *all* members
    including private ones. In CI the workflow's ``GITHUB_TOKEN`` isn't a
    CliMA member, so that endpoint 404s and we fall back to
    ``orgs/CliMA/public_members``.

    To avoid forgetting private members we already discovered: we union the
    fetched set with whatever's already in ``clima_members.json``. A past
    local run by a real member can therefore enrich the file for all later
    CI runs.
    """
    print("→ CliMA org members")
    out_path = DATA_ROOT / "clima_members.json"
    existing: set[str] = set()
    if out_path.exists():
        try:
            existing.update(json.loads(out_path.read_text()).get("members", []))
        except (json.JSONDecodeError, OSError):
            pass

    fetched: set[str] = set()
    for endpoint in ("orgs/CliMA/members", "orgs/CliMA/public_members"):
        try:
            out = gh(
                "api", "--paginate",
                f"{endpoint}?per_page=100",
                "--jq", ".[].login",
            ).strip()
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: {endpoint}: {tail[0]}", file=sys.stderr)
            continue
        fetched.update(ln.strip() for ln in out.splitlines() if ln.strip())
        if fetched:
            print(f"  fetched {len(fetched)} from {endpoint}")
            break

    new = fetched - existing
    merged = sorted(existing | fetched)
    out_path.write_text(json.dumps({"members": merged}, indent=2))
    print(
        f"  wrote {len(merged)} members "
        f"({len(new)} new this run, {len(existing)} preserved)"
    )


def fetch_repo_creation_dates() -> None:
    """Single JSONL refreshed each run; the data is effectively static
    once a repo exists but we refresh anyway in case repos are added."""
    print("→ Repo creation dates")
    out_path = DATA_ROOT / "repo_creation_dates.jsonl"
    rows = []
    for repo in STATS_REPOS:
        try:
            out = gh(
                "api", f"repos/CliMA/{repo}.jl",
                "--jq", "{name: .name, created_at: .created_at}",
            ).strip()
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: created_at {repo}: {tail[0]}", file=sys.stderr)
            continue
        if out:
            rows.append(json.loads(out))
    write_jsonl(out_path, rows)
    print(f"  wrote {len(rows)} repos")


def main() -> int:
    print(f"Fetching CliMA GitHub data through UTC day {LAST_DAY}")
    print(f"Backfill start (when no prior data): {BACKFILL_START}")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    # Global short-circuit: if the previous successful run already covered
    # LAST_DAY, there is nothing to do today. Skips ALL per-repo work
    # (forks/releases pagination, repo-stats snapshot, etc.).
    completed_marker = DATA_ROOT / ".last_completed_day"
    if read_marker(completed_marker) == LAST_DAY:
        print(f"Already completed for UTC day {LAST_DAY}; nothing to do.")
        return 0

    fetch_prs()
    fetch_issues()
    fetch_repo_stats()
    fetch_forks()
    fetch_releases()
    fetch_stargazers()
    fetch_projecttoml_commits()
    fetch_repo_creation_dates()
    fetch_clima_members()
    write_marker(completed_marker, LAST_DAY)
    print("✓ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
