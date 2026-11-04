"""Incrementally fetch GitHub data for CliMA org repos.

For each repo and data type we:

- Pick up where the previous run left off (resumable, like the buildkite
  notebook does with its ``finished_from`` timestamp).
- Only fetch data whose timestamp is strictly before the start of the
  *current* UTC day, so every batch we commit represents complete UTC days.

The stage runs once per UTC day on a Calkit schedule input, and the
current day is read from the period file Calkit writes (see period.py).

On GitHub Actions ``GH_TOKEN`` is set
from the ``CLIMA_GH_TOKEN`` secret, a personal token with push access to
the CliMA repos, so ``gh`` is already authenticated.

Errors on single repos don't stop the run, but they make it exit non-zero
at the end, so a gap in the data fails the pipeline rather than passing
quietly. Locally the user runs ``gh auth login`` once (the calkit setup
dependency checks this).

Data layout (everything under ``data/github/``)::

    prs/<repo>/<YYYY-MM-DD>.json     # merged PRs that UTC day (omitted if 0)
    prs/<repo>/.last_fetched         # latest UTC day fully fetched
    issues/<repo>/<YYYY-MM-DD>.json  # issues created that UTC day (omitted if 0)
    issues/<repo>/.last_fetched
    repo_stats/<YYYY-MM-DD>.jsonl    # one snapshot row per repo per UTC day
    discussions/<repo>.jsonl         # rewritten each run, by number
    forks/<repo>.jsonl               # append-only, deduped by fork full_name
    releases/<repo>.jsonl            # append-only, deduped by release id
    projecttoml/<repo>.jsonl         # append-only, deduped by commit SHA
    repo_creation_dates.jsonl        # refreshed each run (static-ish)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from clima_repos import CLIMA_REPOS, repo_slug
from period import current_day

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "data" / "github"

# Fetch up to (but not including) start of the current UTC day.
TODAY_UTC = current_day()
LAST_DAY = TODAY_UTC - timedelta(days=1)
TODAY_UTC_START_ISO = f"{TODAY_UTC.isoformat()}T00:00:00Z"

# First-time backfill start when a repo has no prior data.
BACKFILL_START = date(2018, 1, 1)

PR_FIELDS = (
    "number,title,body,author,createdAt,mergedAt,additions,deletions,"
    "changedFiles,labels,closingIssuesReferences"
)
ISSUE_FIELDS = "number,title,author,createdAt,labels"

# Every stream covers every tracked repo, so per-repo engagement compares
# like with like. A repo new to the list backfills from its creation date.
PR_REPOS = ISSUE_REPOS = STATS_REPOS = PROJECTTOML_REPOS = CLIMA_REPOS

# gh search pacing: GitHub allows 30 search requests a minute
SEARCH_INTERVAL_S = 2.1
SEARCH_CAP = 1000

# Problems that lose data. Each stream records its failures here and keeps
# going, so one bad repo doesn't block the rest, but main() exits non-zero
# if there are any, so a broken token or repo fails the pipeline instead of
# quietly leaving a gap.
FAILURES: list[str] = []

# Repos the token can read, with their REST metadata; set in main()
REPO_META: dict[str, dict] = {}


def fail(stream: str, repo: str, err: Exception | str) -> None:
    if isinstance(err, subprocess.CalledProcessError):
        tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
        err = tail[0]
    print(f"  ERROR: {stream} {repo}: {err}", file=sys.stderr)
    FAILURES.append(f"{stream} {repo}: {err}")


# --- gh CLI helpers ---------------------------------------------------------

TRANSIENT = ("HTTP 5", "timeout", "timed out", "connection", "EOF", "abuse",
             "secondary rate limit", "API rate limit exceeded")


def gh(*args: str) -> str:
    """Run gh CLI; return stdout, retrying transient errors.

    Raises CalledProcessError once retries run out or on a permanent error,
    e.g., a 403 or 404.
    """
    for attempt in range(4):
        try:
            return subprocess.run(
                ["gh", *args], check=True, capture_output=True, text=True,
            ).stdout
        except subprocess.CalledProcessError as err:
            msg = err.stderr or ""
            if attempt == 3 or not any(t in msg for t in TRANSIENT):
                raise
            wait = 60 if "rate limit" in msg else 5 * 2**attempt
            print(f"  retrying in {wait}s: {msg.strip()[-120:]}",
                  file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


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

def search_window(
    repo: str, kind: str, qualifier: str, fields: str, start: date, end: date
) -> list[dict]:
    """Every PR or issue matching ``qualifier`` from ``start`` to ``end``.

    gh's search stops at 1,000 results, so a window that hits the cap is
    split in half and each half fetched on its own. Raises
    CalledProcessError if a call fails.
    """
    query = f"{qualifier}:{start.isoformat()}..{end.isoformat()}"
    state = ["--state", "merged"] if kind == "pr" else ["--state", "all"]
    time.sleep(SEARCH_INTERVAL_S)
    items = json.loads(gh(
        kind, "list", "--repo", repo_slug(repo), *state,
        "--search", query, "--limit", str(SEARCH_CAP), "--json", fields,
    ))
    if len(items) < SEARCH_CAP:
        return items
    if start == end:
        raise subprocess.CalledProcessError(
            1, "gh", stderr=f"{query} has {SEARCH_CAP}+ results in one day"
        )
    mid = start + (end - start) // 2
    return search_window(repo, kind, qualifier, fields, start, mid) + (
        search_window(
            repo, kind, qualifier, fields, mid + timedelta(days=1), end
        )
    )


def fetch_search_chunked(
    repo: str,
    kind: str,            # "pr" or "issue"
    qualifier: str,       # "merged" or "created"
    fields: str,
    out_subdir: str,
) -> None:
    """Fetch all events matching the time qualifier and bucket them into
    per-UTC-day files.

    One search per (repo, calendar year), split further only if a year hits
    the result cap, so a backfill is a call or two per repo-year. The
    per-repo ``.last_fetched`` marker only advances past windows fetched in
    full, so a failure is retried on the next run.
    """
    repo_lc = repo.lower()
    out_dir = DATA_ROOT / out_subdir / repo_lc
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".last_fetched"

    last = read_marker(marker)
    if last:
        start_day = last + timedelta(days=1)
    else:
        created = (REPO_META[repo].get("created_at") or "")[:10]
        start_day = max(BACKFILL_START, date.fromisoformat(created)
                        if created else BACKFILL_START)
    if start_day > LAST_DAY:
        return

    ts_field = "mergedAt" if kind == "pr" else "createdAt"
    cur = start_day
    while cur <= LAST_DAY:
        chunk_end = min(date(cur.year, 12, 31), LAST_DAY)
        try:
            items = search_window(repo, kind, qualifier, fields, cur, chunk_end)
        except subprocess.CalledProcessError as err:
            fail(kind, f"{repo} {cur}..{chunk_end}", err)
            return

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
    for repo in readable(PR_REPOS):
        fetch_search_chunked(repo, "pr", "merged", PR_FIELDS, "prs")


def fetch_issues() -> None:
    print("→ Issues (per-day, year-chunked queries)")
    for repo in readable(ISSUE_REPOS):
        fetch_search_chunked(repo, "issue", "created", ISSUE_FIELDS, "issues")


# --- daily snapshot ---------------------------------------------------------

def fetch_repo_stats() -> None:
    """One JSONL file per UTC day with star/watcher/fork/issue counts for
    every repo in STATS_REPOS. Treats the snapshot as "as of end of LAST_DAY".

    Only a complete snapshot is written, since a day's file is never
    revisited: if any repo couldn't be read, the day is left for the next
    run to retry.
    """
    print(f"→ Repo stats snapshot for {LAST_DAY}")
    out_dir = DATA_ROOT / "repo_stats"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{LAST_DAY.isoformat()}.jsonl"
    if out_path.exists():
        print(f"  already have {out_path.name}")
        return
    missing = [r for r in STATS_REPOS if r not in REPO_META]
    if missing:
        fail("stats", ", ".join(missing), "unreadable, so no snapshot today")
        return
    rows = [
        {
            "name": REPO_META[r]["name"],
            "stars": REPO_META[r]["stargazers_count"],
            "watchers": REPO_META[r]["subscribers_count"],
            "forks": REPO_META[r]["forks_count"],
            "open_issues": REPO_META[r]["open_issues_count"],
            "snapshot_date": LAST_DAY.isoformat(),
        }
        for r in STATS_REPOS
    ]
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
    for repo in readable(STATS_REPOS):
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("fork_full_name") for e in existing}
        try:
            pages = gh_api_paginated(f"repos/{repo_slug(repo)}/forks")
        except subprocess.CalledProcessError as err:
            fail("forks", repo, err)
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
    for repo in readable(STATS_REPOS):
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("id") for e in existing}
        try:
            pages = gh_api_paginated(
                f"repos/{repo_slug(repo)}/releases?per_page=100"
            )
        except subprocess.CalledProcessError as err:
            fail("releases", repo, err)
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
    for repo in readable(PROJECTTOML_REPOS):
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
                f"repos/{repo_slug(repo)}/commits?{'&'.join(params)}"
            )
        except subprocess.CalledProcessError as err:
            fail("projecttoml", repo, err)
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
    for repo in readable(STATS_REPOS):
        repo_lc = repo.lower()
        out_path = base_dir / f"{repo_lc}.jsonl"
        existing = read_jsonl(out_path)
        seen = {e.get("login") for e in existing}
        try:
            out = gh(
                "api", "--paginate",
                "-H", "Accept: application/vnd.github.v3.star+json",
                f"repos/{repo_slug(repo)}/stargazers?per_page=100",
            ).strip()
        except subprocess.CalledProcessError as err:
            fail("stargazers", repo, err)
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

    if not fetched:
        # Without the member list, CliMA people would count as external
        fail("members", "CliMA", "neither members endpoint was readable")
    new = fetched - existing
    merged = sorted(existing | fetched)
    out_path.write_text(json.dumps({"members": merged}, indent=2))
    print(
        f"  wrote {len(merged)} members "
        f"({len(new)} new this run, {len(existing)} preserved)"
    )


def fetch_repo_creation_dates() -> None:
    """Single JSONL refreshed each run; the data is effectively static
    once a repo exists but we refresh anyway in case repos are added.
    Rows for repos that couldn't be read this run are kept."""
    print("→ Repo creation dates")
    out_path = DATA_ROOT / "repo_creation_dates.jsonl"
    rows = {r["name"]: r for r in read_jsonl(out_path)}
    for repo in STATS_REPOS:
        meta = REPO_META.get(repo)
        if meta:
            rows[meta["name"]] = {
                "name": meta["name"],
                "created_at": meta["created_at"],
            }
    write_jsonl(out_path, rows.values())
    print(f"  wrote {len(rows)} repos")


def fetch_discussions() -> None:
    """GitHub Discussions per repo (JSONL rewritten each run, by number).

    Oceananigans users ask many of their questions here rather than in
    issues. Only repos with Discussions turned on are fetched; discussions
    started in the current UTC day are left for the next run.
    """
    print("→ Discussions")
    base_dir = DATA_ROOT / "discussions"
    query = """
    query($owner: String!, $name: String!, $endCursor: String) {
      repository(owner: $owner, name: $name) {
        discussions(first: 100, after: $endCursor,
                    orderBy: {field: CREATED_AT, direction: ASC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number title createdAt answerChosenAt
            author { login __typename }
            category { name }
          }
        }
      }
    }"""
    for repo in readable(STATS_REPOS):
        if not REPO_META[repo].get("has_discussions"):
            continue
        owner, name = repo_slug(repo).split("/")
        try:
            out = gh(
                "api", "graphql", "--paginate", "-f", f"query={query}",
                "-F", f"owner={owner}", "-F", f"name={name}",
            )
        except subprocess.CalledProcessError as err:
            fail("discussions", repo, err)
            continue
        rows = []
        decoder, idx, out = json.JSONDecoder(), 0, out.strip()
        while idx < len(out):
            page, idx = decoder.raw_decode(out, idx)
            while idx < len(out) and out[idx].isspace():
                idx += 1
            for d in page["data"]["repository"]["discussions"]["nodes"]:
                if d["createdAt"] >= TODAY_UTC_START_ISO:
                    continue
                author = d.get("author") or {}
                rows.append({
                    "repo": f"{repo}.jl",
                    "number": d["number"],
                    "title": d["title"],
                    "created_at": d["createdAt"],
                    "answer_chosen_at": d.get("answerChosenAt"),
                    "author_login": author.get("login"),
                    "author_type": author.get("__typename"),
                    "category": (d.get("category") or {}).get("name"),
                })
        rows.sort(key=lambda r: r["number"])
        write_jsonl(base_dir / f"{repo.lower()}.jsonl", rows)
        print(f"  discussions {repo}: {len(rows)}")


def readable(repos: list[str]) -> list[str]:
    """The repos the token could read at the start of the run."""
    return [r for r in repos if r in REPO_META]


def check_access() -> bool:
    """Fail fast if the token can't do what the streams need.

    The workflow's own GITHUB_TOKEN can read repos but not list stargazers,
    which is how stars silently stopped updating in October 2026.
    """
    try:
        gh("api", "-H", "Accept: application/vnd.github.v3.star+json",
           "repos/CliMA/ClimaAtmos.jl/stargazers?per_page=1")
        return True
    except subprocess.CalledProcessError as err:
        fail("access", "token", err)
        print(
            "ERROR: GH_TOKEN can't list stargazers on CliMA repos. In CI it "
            "should come from the CLIMA_GH_TOKEN secret.",
            file=sys.stderr,
        )
        return False


def load_repo_meta() -> None:
    """Read every tracked repo's metadata once; unreadable ones fail."""
    print("→ Repo metadata")
    for repo in STATS_REPOS:
        try:
            REPO_META[repo] = json.loads(gh("api", f"repos/{repo_slug(repo)}"))
        except subprocess.CalledProcessError as err:
            fail("repo", repo, err)
    print(f"  read {len(REPO_META)} of {len(STATS_REPOS)} repos")


def main() -> int:
    print(f"Fetching CliMA GitHub data through UTC day {LAST_DAY}")
    print(f"Backfill start (when no prior data): {BACKFILL_START}")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    if not check_access():
        return 1
    load_repo_meta()
    fetch_prs()
    fetch_issues()
    fetch_repo_stats()
    fetch_forks()
    fetch_releases()
    fetch_stargazers()
    fetch_discussions()
    fetch_projecttoml_commits()
    fetch_repo_creation_dates()
    fetch_clima_members()
    if FAILURES:
        print(f"✗ {len(FAILURES)} failures; data from them is missing:",
              file=sys.stderr)
        for f in FAILURES:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("✓ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
