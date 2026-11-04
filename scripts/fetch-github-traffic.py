"""Snapshot GitHub traffic stats for CliMA repos before they age out.

GitHub keeps only the last 14 days of traffic, so this runs daily and
accumulates it. Reading traffic needs push access to the repo, which the
workflow's GITHUB_TOKEN doesn't have, so in CI ``GH_TOKEN`` comes from the
CLIMA_GH_TOKEN secret.

Data layout (everything under ``data/github-traffic/``)::

    views.csv      # repo, date, count, uniques: one row per repo per day
    clones.csv     # same, for git clones
    referrers.csv  # fetched_on, repo, referrer, count, uniques
    paths.csv      # fetched_on, repo, path, title, count, uniques

Views and clones come back per day, so each run upserts the days it sees:
any run within 14 days of a missed one fills the gap. GitHub returns zero
days too, so a missing row means no data, not zero traffic. Referrers and
popular paths are top-10 totals over the 14 days before ``fetched_on``, with
no daily breakdown, so they're appended as one snapshot per run.

Unique counts are per repo: summing them across repos or days overcounts
people who visit more than one repo or come back.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from clima_repos import CLIMA_REPOS
from period import current_day

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "github-traffic"

TODAY_UTC = current_day()

DAILY_FIELDS = ["repo", "date", "count", "uniques"]
REFERRER_FIELDS = ["fetched_on", "repo", "referrer", "count", "uniques"]
PATH_FIELDS = ["fetched_on", "repo", "path", "title", "count", "uniques"]


def gh_api(path: str):
    """GET a GitHub API path with the gh CLI and parse the JSON."""
    out = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upsert_daily(path: Path, new_rows: list[dict]) -> int:
    """Merge per-day rows into ``path``, newest reading winning.

    Returns how many (repo, day) rows weren't there before.
    """
    by_key = {(r["repo"], r["date"]): r for r in read_csv(path)}
    n_new = sum((r["repo"], r["date"]) not in by_key for r in new_rows)
    for r in new_rows:
        by_key[(r["repo"], r["date"])] = r
    rows = sorted(by_key.values(), key=lambda r: (r["repo"], r["date"]))
    write_csv(path, DAILY_FIELDS, rows)
    return n_new


def replace_snapshot(path: Path, fields: list[str], new_rows: list[dict]):
    """Append this run's snapshot, replacing any earlier one from today."""
    today = TODAY_UTC.isoformat()
    rows = [r for r in read_csv(path) if r["fetched_on"] != today]
    rows.extend(new_rows)
    write_csv(path, fields, rows)


def daily_rows(repo: str, items: list[dict]) -> list[dict]:
    """Per-day rows for complete days, i.e., before the current UTC day."""
    rows = []
    for it in items:
        day = it["timestamp"][:10]
        if day < TODAY_UTC.isoformat():
            rows.append(
                {
                    "repo": f"{repo}.jl",
                    "date": day,
                    "count": it["count"],
                    "uniques": it["uniques"],
                }
            )
    return rows


def main() -> int:
    print(f"Fetching GitHub traffic for {len(CLIMA_REPOS)} repos")
    views, clones, referrers, paths = [], [], [], []
    failed: list[str] = []
    fetched_on = TODAY_UTC.isoformat()
    for repo in CLIMA_REPOS:
        base = f"repos/CliMA/{repo}.jl/traffic"
        try:
            v = gh_api(f"{base}/views?per=day")
            c = gh_api(f"{base}/clones?per=day")
            refs = gh_api(f"{base}/popular/referrers")
            pops = gh_api(f"{base}/popular/paths")
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: traffic {repo}: {tail[0]}", file=sys.stderr)
            failed.append(repo)
            continue
        views.extend(daily_rows(repo, v.get("views", [])))
        clones.extend(daily_rows(repo, c.get("clones", [])))
        for r in refs:
            referrers.append(
                {
                    "fetched_on": fetched_on,
                    "repo": f"{repo}.jl",
                    "referrer": r["referrer"],
                    "count": r["count"],
                    "uniques": r["uniques"],
                }
            )
        for p in pops:
            paths.append(
                {
                    "fetched_on": fetched_on,
                    "repo": f"{repo}.jl",
                    "path": p["path"],
                    "title": p["title"],
                    "count": p["count"],
                    "uniques": p["uniques"],
                }
            )
        print(f"  {repo}: {v.get('uniques', 0)} unique visitors over 14 days")

    # Every repo failing means the token can't read traffic at all, e.g.,
    # it's missing or lacks push access. Fail loudly rather than let the
    # 14-day window quietly roll past.
    if len(failed) == len(CLIMA_REPOS):
        print(
            "ERROR: couldn't read traffic for any repo. Is GH_TOKEN a token "
            "with push access to the CliMA repos?",
            file=sys.stderr,
        )
        return 1

    n_views = upsert_daily(OUT_DIR / "views.csv", views)
    n_clones = upsert_daily(OUT_DIR / "clones.csv", clones)
    replace_snapshot(OUT_DIR / "referrers.csv", REFERRER_FIELDS, referrers)
    replace_snapshot(OUT_DIR / "paths.csv", PATH_FIELDS, paths)
    print(
        f"✓ {n_views} new repo-days of views, {n_clones} of clones, "
        f"{len(referrers)} referrer rows, {len(paths)} path rows"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
