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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "julia-registry"

TODAY_UTC = datetime.now(timezone.utc).date()
LAST_DAY = TODAY_UTC - timedelta(days=1)

REGISTRY_URL = "https://github.com/JuliaRegistries/General"

# Every CliMA-org Julia package we care about tracking dependents for. This
# is the same broad set fetch-github uses for stats/forks snapshots — keep
# it in sync. Oceananigans + satellites are especially important: they're
# the externally-adopted core of CliMA's ecosystem.
CLIMA_PKGS = {
    "ClimaAtmos",
    "ClimaCore",
    "ClimaParams",
    "Thermodynamics",
    "CloudMicrophysics",
    "ClimaCoupler",
    "ClimaLand",
    "ClimaOcean",
    "SurfaceFluxes",
    "Insolation",
    "RRTMGP",
    "ClimaUtilities",
    "ClimaDiagnostics",
    "ClimaAnalysis",
    "Oceananigans",
    "ClimaSeaIce",
    "SeawaterPolynomials",
    "CubedSphere",
    "ClimaTimeSteppers",
    "ClimaComms",
    "UnrolledUtilities",
    "MultiBroadcastFusion",
    "LazyBroadcast",
    "NullBroadcasts",
    "StructuredPrinting",
    "OperatorFlux",
    "ClimaInterpolations",
    "RootSolvers",
    "ArtifactWrappers",
    "ClimaEarth",
    "ClimaViz",
    "RandomFeatures",
    "EnsembleKalmanProcesses",
    "CalibrateEmulateSample",
    "ClimaCalibrate",
}


def parse_registry(registry_path: Path) -> dict:
    """Walk a checked-out General registry, return {clima_pkg -> [dependent
    dicts]} plus the total package count."""
    dependents: dict[str, list[dict]] = {pkg: [] for pkg in CLIMA_PKGS}
    total_packages = 0

    for deps_file in registry_path.rglob("Deps.toml"):
        pkg_dir = deps_file.parent
        pkg_name = pkg_dir.name
        pkg_toml = pkg_dir / "Package.toml"
        if not pkg_toml.exists():
            continue

        repo = ""
        for line in pkg_toml.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.startswith("repo"):
                repo = line.split("=", 1)[1].strip().strip('"')
                break

        is_clima = "CliMA/" in repo or "/CliMA/" in repo
        total_packages += 1

        content = deps_file.read_text(encoding="utf-8", errors="replace")
        deps_in_file: set[str] = set()
        for line in content.splitlines():
            line = line.strip()
            # Deps.toml lines look like ``Name = "uuid"``; ``[deps]`` headers
            # use brackets, so skip those.
            if "=" in line and not line.startswith("["):
                dep = line.split("=", 1)[0].strip().strip('"')
                deps_in_file.add(dep)

        for clima_pkg in CLIMA_PKGS:
            if clima_pkg in deps_in_file:
                dependents[clima_pkg].append(
                    {
                        "name": pkg_name,
                        "repo": repo,
                        "is_clima": is_clima,
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


def first_seen_via_git(clone_path: Path, clima_pkg: str, dependent_pkg: str) -> str | None:
    """Find the first commit that added ``clima_pkg`` to ``dependent_pkg``'s
    Deps.toml, using ``git log -S`` (pickaxe). Returns an ISO date or None
    if we couldn't find one.

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
    deps_path = f"{dependent_pkg[0].upper()}/{dependent_pkg}/Deps.toml"
    # Use ``-G`` with an anchored regex so we match the literal Deps.toml
    # line (``ClimaCore = "uuid"``) instead of any string containing the
    # name — otherwise searching for ``ClimaCore`` would false-match on
    # adds of ``ClimaCoreSpectra`` or similar package-name prefixes.
    pattern = rf"^{clima_pkg} = "
    try:
        out = subprocess.run(
            [
                "git", "-C", str(clone_path),
                "log", "--reverse", "-G", pattern,
                "--format=%aI",
                "HEAD", "--", deps_path,
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
    if latest_run_path.exists():
        try:
            if json.loads(latest_run_path.read_text()).get("date") == today:
                print(
                    f"Registry already snapshotted for {today}; nothing to do."
                )
                return 0
        except json.JSONDecodeError:
            pass

    out_path = OUT_DIR / "dependents.jsonl"
    existing = {
        (r["clima_package"], r["dependent_name"]): r
        for r in read_jsonl(out_path)
    }

    # First run (or empty dependents.jsonl): clone with full history so we
    # can pickaxe Deps.toml for each (clima_pkg, dependent) pair and stamp a
    # real first_seen_at. Otherwise a shallow clone is enough — we only need
    # the current registry state to diff against the prior snapshot.
    backfill_history = not existing
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
                "for one-time first_seen_at backfill..."
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
                    n_still_present += 1
        n_new = len(new_keys)

        # Replace ``first_seen_at = today`` placeholders with real historical
        # dates pulled from git history. Only meaningful on the first run
        # (when we did a non-shallow clone); subsequent runs' shallow clone
        # can't see the history a new dependency was first added in, but
        # those new rows are by definition added "today" anyway.
        if backfill_history and new_keys:
            print(
                f"→ Backfilling first_seen_at from git history "
                f"for {len(new_keys)} dependencies..."
            )
            for i, key in enumerate(new_keys):
                first = first_seen_via_git(clone_path, key[0], key[1])
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
