"""Track Julia General registry dependents of CliMA packages.

Rather than committing one ``YYYY-MM-DD.json`` snapshot per run (which
duplicates ~all dependent rows day-over-day), this script maintains a single
event-log table ``data/julia-registry/dependents.jsonl`` with one row per
(clima_package, dependent) pair. Each row carries:

- ``first_seen_at`` — first UTC day we observed this dependency
- ``last_seen_at``  — most recent UTC day we observed it

Adding a dependent for the first time stamps ``first_seen_at`` and
``last_seen_at`` to today; seeing it again updates ``last_seen_at`` only.
Removed dependents keep their last_seen_at frozen — the analyze notebook can
filter for "still present" via ``last_seen_at == latest_run_date``.

This lets the analyzer plot quarterly *new* external dependents (grouped on
``first_seen_at``) without re-scanning a multi-MB pile of daily snapshots.

The latest-run date (and total registry package count) is written to
``latest_run.json`` so the notebook can identify currently-active dependents.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import timedelta
from pathlib import Path

from clima_repos import AFFILIATED_ORGS, CLIMA_REPOS, is_package
from period import current_day

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "julia-registry"

TODAY_UTC = current_day()
LAST_DAY = TODAY_UTC - timedelta(days=1)

REGISTRY_URL = "https://github.com/JuliaRegistries/General"

# Every tracked CliMA repo that's a registered package, from the list all
# fetch stages share.
CLIMA_PKGS = {n for n in CLIMA_REPOS if is_package(n)}
# Old registered names whose dependents count toward the current package
ALIASES = {"CLIMAParameters": "ClimaParams"}
# Hard dependencies, and package extensions (optional dependencies)
DEP_FILES = {"Deps.toml": "deps", "WeakDeps.toml": "weakdeps"}


def tracked_uuids(registry_path: Path) -> dict[str, tuple[str, str]]:
    """Map registry UUID to (tracked CliMA package, registered name).

    Matching on UUIDs rather than names avoids counting an unrelated package
    that happens to share a name.
    """
    registry = tomllib.loads((registry_path / "Registry.toml").read_text())
    out = {}
    for uuid, entry in registry["packages"].items():
        name = entry["name"]
        pkg = ALIASES.get(name, name)
        if pkg in CLIMA_PKGS:
            out[uuid] = (pkg, name)
    return out


def parse_registry(registry_path: Path) -> dict:
    """Walk a checked-out General registry, return {clima_pkg -> [dependent
    dicts]} plus the total package count.

    Reads both Deps.toml and WeakDeps.toml, so packages that use a CliMA
    package through an extension count too; ``kind`` says which, preferring
    "deps" when a package has both.
    """
    uuids = tracked_uuids(registry_path)
    dependents: dict[str, list[dict]] = {pkg: [] for pkg in CLIMA_PKGS}
    total_packages = 0

    for pkg_toml in registry_path.glob("*/*/Package.toml"):
        pkg_dir = pkg_toml.parent
        pkg_name = pkg_dir.name
        total_packages += 1
        repo = ""
        for line in pkg_toml.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.startswith("repo"):
                repo = line.split("=", 1)[1].strip().strip('"')
                break
        is_clima = "/CliMA/" in repo
        is_affiliated = any(f"/{org}/" in repo for org in AFFILIATED_ORGS)

        # clima_pkg -> (kind, registered names it's listed under)
        found: dict[str, tuple[str, set[str]]] = {}
        for dep_file, kind in DEP_FILES.items():
            path = pkg_dir / dep_file
            if not path.exists():
                continue
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = line.strip()
                # Lines look like ``Name = "uuid"``; skip ``[1-2]`` headers
                if "=" not in line or line.startswith("["):
                    continue
                uuid = line.split("=", 1)[1].strip().strip('"')
                if uuid not in uuids:
                    continue
                pkg, reg_name = uuids[uuid]
                prev_kind, names = found.get(pkg, (kind, set()))
                kind_now = "deps" if "deps" in (prev_kind, kind) else kind
                found[pkg] = (kind_now, names | {reg_name})

        for pkg, (kind, names) in found.items():
            dependents[pkg].append(
                {
                    "name": pkg_name,
                    "repo": repo,
                    "is_clima": is_clima,
                    "is_affiliated": is_affiliated,
                    "kind": kind,
                    "listed_as": sorted(names),
                }
            )

    return {
        "dependents": dependents,
        "total_packages_in_registry": total_packages,
    }


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True))
            f.write("\n")


def first_seen_via_git(
    clone_path: Path, names: list[str], dependent_pkg: str
) -> str | None:
    """Find the first commit that added any of ``names`` (a CliMA package's
    registered names) to ``dependent_pkg``'s Deps.toml or WeakDeps.toml,
    using ``git log -G``. Returns an ISO date or None if we couldn't find
    one.

    Notes:
    - Requires a clone with blob history. Partial clones (--filter=blob:none
      or blob:limit) silently return empty results because pickaxe needs to
      diff file contents.
    - Don't combine ``--reverse`` with ``-n 1`` — that returns the *newest*
      match (max-count is applied before --reverse). We grab all matching
      commits in oldest-first order and take the first line.
    """
    if not dependent_pkg:
        return None
    pkg_dir = f"{dependent_pkg[0].upper()}/{dependent_pkg}"
    paths = [f"{pkg_dir}/{f}" for f in DEP_FILES]
    # Anchored so ``ClimaCore`` doesn't match adds of ``ClimaCoreSpectra``
    pattern = "^(" + "|".join(names) + ") = "
    try:
        out = subprocess.run(
            [
                "git", "-C", str(clone_path),
                "log", "--reverse", "-E", "-G", pattern,
                "--format=%aI",
                "HEAD", "--", *paths,
            ],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    first = (out.splitlines() or [""])[0].strip()
    return first[:10] if first else None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = LAST_DAY.isoformat()
    latest_run_path = OUT_DIR / "latest_run.json"

    out_path = OUT_DIR / "dependents.jsonl"
    existing = {
        (r["clima_package"], r["dependent_name"]): r
        for r in read_jsonl(out_path)
    }

    # When what's tracked changes (first run, a new package, a new dep file
    # kind, or a new alias), clone with full history so the dependents that
    # are new to us get their real first_seen_at from git instead of all
    # looking new today. Otherwise a shallow clone is enough: we only need
    # the current registry state to diff against the prior snapshot.
    latest = (
        json.loads(latest_run_path.read_text())
        if latest_run_path.exists()
        else {}
    )
    backfill_history = (
        not existing
        or not CLIMA_PKGS <= set(latest.get("tracked_clima_packages", []))
        or latest.get("dep_files") != sorted(DEP_FILES)
        or latest.get("aliases") != ALIASES
    )
    work = Path(tempfile.mkdtemp(prefix="julia-registry-"))
    try:
        clone_path = work / "General"
        if backfill_history:
            # Full clone (no blob filter): pickaxe needs blob diffs to find
            # the first commit that added each CliMA dep, so blob:none would
            # silently return empty results. Costs ~5x more bandwidth but
            # only happens once.
            print(
                f"→ Cloning {REGISTRY_URL} (full history) "
                "to backfill first_seen_at for newly tracked dependents..."
            )
            subprocess.run(
                ["git", "clone", REGISTRY_URL, str(clone_path)],
                check=True,
            )
        else:
            print(f"→ Cloning {REGISTRY_URL} (shallow)...")
            subprocess.run(
                ["git", "clone", "--depth=1", REGISTRY_URL, str(clone_path)],
                check=True,
            )
        print("→ Parsing registry for CliMA-package dependents...")
        summary = parse_registry(clone_path)

        # Diff against the previous run: anything we see this run gets
        # last_seen_at bumped to today; anything new also gets first_seen_at.
        current_pairs: set[tuple[str, str]] = set()
        new_keys: list[tuple[str, str]] = []
        n_still_present = 0
        for pkg, deps in summary["dependents"].items():
            for d in deps:
                key = (pkg, d["name"])
                current_pairs.add(key)
                row = existing.get(key)
                if row is None:
                    existing[key] = {
                        "clima_package": pkg,
                        "dependent_name": d["name"],
                        "dependent_repo": d["repo"],
                        "is_clima": d["is_clima"],
                        "is_affiliated": d["is_affiliated"],
                        "kind": d["kind"],
                        "listed_as": d["listed_as"],
                        "first_seen_at": today,
                        "last_seen_at": today,
                    }
                    new_keys.append(key)
                else:
                    row["last_seen_at"] = today
                    # Repo + is_clima can change if a package transfers owners;
                    # keep the row in sync rather than letting it drift.
                    row["dependent_repo"] = d["repo"]
                    row["is_clima"] = d["is_clima"]
                    row["is_affiliated"] = d["is_affiliated"]
                    row["kind"] = d["kind"]
                    row["listed_as"] = d["listed_as"]
                    n_still_present += 1
        n_new = len(new_keys)

        # Replace ``first_seen_at = today`` placeholders with real historical
        # dates pulled from git history, which only a full clone has. With a
        # shallow clone, a new row is by definition new today.
        if backfill_history and new_keys:
            print(
                f"→ Backfilling first_seen_at from git history "
                f"for {len(new_keys)} dependencies..."
            )
            for i, key in enumerate(new_keys):
                first = first_seen_via_git(
                    clone_path, existing[key]["listed_as"], key[1]
                )
                if first:
                    existing[key]["first_seen_at"] = first
                if (i + 1) % 25 == 0:
                    print(f"  backfilled {i + 1}/{len(new_keys)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    n_removed = len(existing) - n_new - n_still_present
    rows = sorted(
        existing.values(),
        key=lambda r: (
            r["clima_package"],
            r["first_seen_at"],
            r["dependent_name"],
        ),
    )
    write_jsonl(out_path, rows)

    latest_run_path.write_text(
        json.dumps(
            {
                "date": today,
                "total_packages_in_registry": summary[
                    "total_packages_in_registry"
                ],
                "tracked_clima_packages": sorted(CLIMA_PKGS),
                "dep_files": sorted(DEP_FILES),
                "aliases": ALIASES,
            },
            indent=2,
        )
    )

    print(
        f"Total packages in registry: {summary['total_packages_in_registry']}"
    )
    print(
        f"Dependent rows: {len(rows)} total | "
        f"{n_new} new today | {n_still_present} still present | "
        f"{n_removed} previously seen but not in current snapshot"
    )
    by_pkg: dict[str, dict[str, int]] = {
        pkg: {"total": 0, "external": 0} for pkg in CLIMA_PKGS
    }
    for r in rows:
        if (r["clima_package"], r["dependent_name"]) in current_pairs:
            by_pkg[r["clima_package"]]["total"] += 1
            if not r["is_clima"]:
                by_pkg[r["clima_package"]]["external"] += 1
    for pkg in sorted(CLIMA_PKGS):
        c = by_pkg[pkg]
        if c["total"]:
            print(
                f"  {pkg:<22} {c['total']:>4} dependents ({c['external']} external)"
            )
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
