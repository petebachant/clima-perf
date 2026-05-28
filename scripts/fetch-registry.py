"""Snapshot the Julia General registry for CliMA-package dependents.

The General registry only changes when packages are added or updated, so we
just keep one snapshot per UTC day. We:

1. Shallow-clone https://github.com/JuliaRegistries/General into a tempdir.
2. Walk every package's ``Deps.toml`` and record which CliMA packages it
   depends on. We also track whether the dependent itself is a CliMA-org
   package (so the analyzer can split external-vs-internal adoption).
3. Write the snapshot to ``data/julia_registry/<YYYY-MM-DD>.json``, keyed
   by the UTC day it represents. No-op if that day's file already exists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "julia-registry"

TODAY_UTC = datetime.now(timezone.utc).date()
LAST_DAY = TODAY_UTC - timedelta(days=1)

REGISTRY_URL = "https://github.com/JuliaRegistries/General"

# Packages we track as "the CliMA stack" in the registry. A registry entry
# whose Deps.toml lists any of these is a dependent.
CLIMA_PKGS = {
    "ClimaAtmos",
    "ClimaCore",
    "ClimaParams",
    "Thermodynamics",
    "CloudMicrophysics",
    "ClimaCoupler",
    "ClimaLand",
    "ClimaOcean",
    "ClimaUtilities",
    "ClimaDiagnostics",
    "ClimaTimeSteppers",
    "ClimaComms",
    "SurfaceFluxes",
    "Insolation",
    "RRTMGP",
    "ClimaAnalysis",
    "ClimaCalibrate",
}


def parse_registry(registry_path: Path) -> dict:
    """Walk a checked-out General registry and return the dependents summary."""
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
            # Deps.toml entries look like ``Name = "uuid"``; section headers
            # like ``[deps]`` use brackets, so skip those.
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


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{LAST_DAY.isoformat()}.json"
    if out_path.exists():
        print(f"Already have registry snapshot for {LAST_DAY} at {out_path}")
        return 0

    work = Path(tempfile.mkdtemp(prefix="julia-registry-"))
    try:
        clone_path = work / "General"
        print(f"→ Cloning {REGISTRY_URL} (shallow)...")
        subprocess.run(
            ["git", "clone", "--depth=1", REGISTRY_URL, str(clone_path)],
            check=True,
        )

        print("→ Parsing registry for CliMA-package dependents...")
        summary = parse_registry(clone_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summary["snapshot_date"] = LAST_DAY.isoformat()
    summary["fetched_at"] = datetime.now(timezone.utc).isoformat()
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))

    print(
        f"Total packages in registry: {summary['total_packages_in_registry']}"
    )
    for pkg in sorted(CLIMA_PKGS):
        deps = summary["dependents"][pkg]
        external = sum(1 for d in deps if not d["is_clima"])
        print(f"  {pkg:<22} {len(deps):>3} dependents ({external} external)")
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
