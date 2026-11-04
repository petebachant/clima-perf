"""Find GitHub repos that depend on CliMA packages, registered or not.

The Julia registry only lists dependents that are registered packages
themselves, which misses most users: research code, course material, and
experiments like valentinaschueller/clima-playground. This searches GitHub
code for each CliMA package's UUID in ``Project.toml`` files, which lists
direct dependencies only and avoids false matches on the name.

Code search covers default branches only, skips forks, returns at most
1,000 results per query, and allows 10 requests a minute, so this runs
weekly. Results also drift a little between runs as GitHub's index
changes, which the first/last-seen log absorbs.

Data layout (everything under ``data/github-code-dependents/``)::

    hits.jsonl    # clima_package, repo, path, first_seen_at, last_seen_at
    repos.jsonl   # metadata for every repo with a hit, refreshed each run
    owners.jsonl  # public profile fields for each repo owner, refreshed

``first_seen_at`` is when this pipeline first found the hit, not when the
dependency was added; repo ``created_at`` is a lower bound for that. CliMA
org repos are skipped here. CliMA members' own repos are kept and tagged
internal downstream.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from pathlib import Path

from period import current_day

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "github-code-dependents"
PACKAGES = REPO_ROOT / "data" / "julia-downloads" / "packages.csv"

TODAY = current_day().isoformat()

# Code search allows 10 requests a minute
SEARCH_INTERVAL_S = 6.5
MAX_PAGES = 10  # 100 results a page; search stops at 1,000

REPO_FIELDS = [
    "full_name",
    "html_url",
    "description",
    "created_at",
    "pushed_at",
    "stargazers_count",
    "forks_count",
    "archived",
    "fork",
    "language",
    "topics",
]
# Public profile fields that help tell who an owner is; names and emails
# aren't kept.
OWNER_FIELDS = [
    "login",
    "type",
    "company",
    "location",
    "blog",
    "bio",
    "description",
    "created_at",
]


def gh_api(*args: str):
    out = subprocess.run(
        ["gh", "api", *args], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)


def search(query: str) -> list[dict]:
    """All code-search hits for ``query``, paced under the rate limit."""
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        for attempt in range(3):
            time.sleep(SEARCH_INTERVAL_S)
            try:
                res = gh_api(
                    "-X",
                    "GET",
                    "search/code",
                    "-f",
                    f"q={query}",
                    "-f",
                    "per_page=100",
                    "-f",
                    f"page={page}",
                )
                break
            except subprocess.CalledProcessError as err:
                msg = (err.stderr or "").strip()
                if "rate limit" in msg.lower() and attempt < 2:
                    print("  rate limited, waiting a minute", file=sys.stderr)
                    time.sleep(60)
                    continue
                raise
        items.extend(res.get("items", []))
        if len(items) >= res.get("total_count", 0) or not res.get("items"):
            break
    return items


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def main() -> int:
    with PACKAGES.open() as f:
        packages = [r for r in csv.DictReader(f) if r["group"] == "clima"]
    hits_path = OUT_DIR / "hits.jsonl"
    log = {
        (r["clima_package"], r["repo"], r["path"]): r
        for r in read_jsonl(hits_path)
    }

    print(f"Searching Project.toml files for {len(packages)} CliMA packages")
    n_found, failed = 0, []
    for pkg in packages:
        query = f'"{pkg["uuid"]}" filename:Project.toml'
        try:
            items = search(query)
        except subprocess.CalledProcessError as err:
            tail = (err.stderr or "").strip().splitlines()[-1:] or [str(err)]
            print(f"  WARN: {pkg['package']}: {tail[0]}", file=sys.stderr)
            failed.append(pkg["package"])
            continue
        n_pkg = 0
        for it in items:
            repo = it["repository"]["full_name"]
            if repo.split("/")[0].lower() == "clima":
                continue
            key = (pkg["package"], repo, it["path"])
            row = log.setdefault(
                key,
                {
                    "clima_package": pkg["package"],
                    "repo": repo,
                    "path": it["path"],
                    "first_seen_at": TODAY,
                },
            )
            row["last_seen_at"] = TODAY
            n_pkg += 1
        n_found += n_pkg
        print(f"  {pkg['package']}: {n_pkg} files outside CliMA")
    if len(failed) == len(packages):
        print("ERROR: every search failed; check GH_TOKEN", file=sys.stderr)
        return 1
    write_jsonl(hits_path, [log[k] for k in sorted(log)])

    # Metadata for every repo ever seen, so repos that dropped out of the
    # index still have a row
    repos = sorted({r["repo"] for r in log.values()})
    old_repos = {r["full_name"]: r for r in read_jsonl(OUT_DIR / "repos.jsonl")}
    repo_rows, owners = [], set()
    print(f"Fetching metadata for {len(repos)} repos")
    for name in repos:
        try:
            meta = gh_api(f"repos/{name}")
            row = {k: meta.get(k) for k in REPO_FIELDS}
            row["full_name"] = name  # keep the searched name if it moved
            row["owner"] = meta["owner"]["login"]
            row["owner_type"] = meta["owner"]["type"]
            row["available"] = True
        except subprocess.CalledProcessError:
            # Deleted or made private; keep what we knew
            row = old_repos.get(name, {"full_name": name})
            row["owner"] = row.get("owner") or name.split("/")[0]
            row["available"] = False
        repo_rows.append(row)
        owners.add(row["owner"])
    write_jsonl(OUT_DIR / "repos.jsonl", repo_rows)

    old_owners = {r["login"]: r for r in read_jsonl(OUT_DIR / "owners.jsonl")}
    owner_rows = []
    print(f"Fetching profiles for {len(owners)} owners")
    for login in sorted(owners):
        try:
            prof = gh_api(f"users/{login}")
            if prof.get("type") == "Organization":
                prof = {**prof, **gh_api(f"orgs/{login}")}
            owner_rows.append({k: prof.get(k) for k in OWNER_FIELDS})
        except subprocess.CalledProcessError:
            owner_rows.append(old_owners.get(login, {"login": login}))
    write_jsonl(OUT_DIR / "owners.jsonl", owner_rows)

    print(
        f"✓ {n_found} hits this run, {len(log)} (package, repo, file) "
        f"rows logged, {len(repos)} repos, {len(owners)} owners"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
