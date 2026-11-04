"""Snapshot Julia package-server download stats before they age out.

The Julia package servers publish request logs aggregated per package,
client type, and day, but only for a rolling window of about 31 days, so
this runs daily and accumulates them for CliMA's packages plus a few peers.
Peers give a baseline for Julia-wide trends: SpeedyWeather (the closest
Julia climate-model peer), Trixi (a Julia PDE framework), and DataFrames
(Julia-wide adoption).

Data layout (everything under ``data/julia-downloads/``)::

    packages.csv        # package, uuid, group ("clima" or "peer")
    by-date.csv         # package, date, status, client_type, request_addrs,
                        #   request_count
    by-region-date.csv  # same plus region, the package server's region

``request_addrs`` counts unique IP addresses, the closest thing to a count
of installers; ``request_count`` counts every request. ``client_type`` is
"user", "ci" (a CI environment variable was set), or empty (unknown). Most
"ci" traffic for CliMA packages is CliMA's own CI, so "user" is the human
signal, though CliMA's own people show up there too. Only successful
requests are kept: status 200, and 302, a redirect to storage, which is how
the servers now hand out most packages. Each run upserts the days it sees,
newest reading winning, and skips the current UTC day, which is partial.
"""

from __future__ import annotations

import csv
import gzip
import io
import sys
import tomllib
import urllib.request
from pathlib import Path

from clima_repos import CLIMA_REPOS, is_package
from period import current_day

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "julia-downloads"

TODAY_UTC = current_day()

PEER_PKGS = ["SpeedyWeather", "Trixi", "DataFrames"]

REGISTRY_URL = (
    "https://raw.githubusercontent.com/JuliaRegistries/General/master/"
    "Registry.toml"
)
LOGS_URL = "https://julialang-logs.s3.amazonaws.com/public_outputs/current/"
KEPT_STATUSES = {"200", "302"}

DATE_FIELDS = [
    "package",
    "date",
    "status",
    "client_type",
    "request_addrs",
    "request_count",
]
REGION_FIELDS = DATE_FIELDS[:1] + ["region"] + DATE_FIELDS[1:]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "clima-perf"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def package_uuids() -> dict[str, tuple[str, str]]:
    """Map registry UUID to (package name, group) for tracked packages.

    Repos that aren't registered packages, e.g., ClimaEarth, are skipped.
    """
    registry = tomllib.loads(fetch(REGISTRY_URL).decode())
    by_name = {v["name"]: uuid for uuid, v in registry["packages"].items()}
    wanted = [(n, "clima") for n in CLIMA_REPOS if is_package(n)] + [
        (n, "peer") for n in PEER_PKGS
    ]
    out = {}
    for name, group in wanted:
        if name in by_name:
            out[by_name[name]] = (name, group)
        else:
            print(f"  {name}: not in the General registry, skipping")
    return out


def filtered_rows(name: str, uuids: dict, with_region: bool) -> list[dict]:
    """Rows of one log file for tracked packages, complete days only."""
    raw = gzip.decompress(fetch(LOGS_URL + name))
    today = TODAY_UTC.isoformat()
    rows = []
    for r in csv.DictReader(io.StringIO(raw.decode())):
        pkg = uuids.get(r["package_uuid"])
        if pkg is None or r["status"] not in KEPT_STATUSES:
            continue
        if r["date"] >= today:
            continue
        row = {
            "package": pkg[0],
            "date": r["date"],
            "status": r["status"],
            "client_type": r["client_type"],
            "request_addrs": r["request_addrs"],
            "request_count": r["request_count"],
        }
        if with_region:
            row["region"] = r["region"]
        rows.append(row)
    return rows


def upsert(path: Path, fields: list[str], new_rows: list[dict]) -> int:
    """Merge rows into ``path`` keyed on every non-count field.

    Returns how many keys weren't there before.
    """
    key_fields = [
        f for f in fields if f not in ("request_addrs", "request_count")
    ]

    def key(r):
        return tuple(r[f] for f in key_fields)

    existing = {}
    if path.exists():
        with path.open(newline="") as f:
            existing = {key(r): r for r in csv.DictReader(f)}
    n_new = sum(key(r) not in existing for r in new_rows)
    for r in new_rows:
        existing[key(r)] = r
    rows = sorted(existing.values(), key=key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return n_new


def main() -> int:
    print("Looking up package UUIDs in the General registry")
    uuids = package_uuids()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "packages.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["package", "uuid", "group"])
        for uuid, (pkg, group) in sorted(uuids.items(), key=lambda x: x[1]):
            writer.writerow([pkg, uuid, group])

    print("Fetching package request logs")
    by_date = filtered_rows("package_requests_by_date.csv.gz", uuids, False)
    by_region = filtered_rows(
        "package_requests_by_region_by_date.csv.gz", uuids, True
    )
    if not by_date:
        # The logs always cover the last month, so no rows at all means the
        # format or location changed. Fail before the window rolls past.
        print("ERROR: no rows for any tracked package", file=sys.stderr)
        return 1
    n_date = upsert(OUT_DIR / "by-date.csv", DATE_FIELDS, by_date)
    n_region = upsert(OUT_DIR / "by-region-date.csv", REGION_FIELDS, by_region)
    days = sorted({r["date"] for r in by_date})
    print(
        f"✓ {len(uuids)} packages, logs cover {days[0]}..{days[-1]}: "
        f"{n_date} new by-date rows, {n_region} new by-region rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
