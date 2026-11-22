"""Mirror the weekly CliMA benchmark jobs from the Buildkite API.

Coupler pipelines, which report into the CliMA Slack ``#coupler-report``
channel:

- ``climacoupler-amip`` -- the target AMIP configuration (Mon 8pm PST)
- ``climacoupler-longruns`` -- the validation suite (Sun 12am PST)
- ``climacoupler-cpu-gpu-benchmarks`` -- the SYPD comparison table (Sun 12am)

Land pipeline:

- ``climaland-long-runs`` -- runs twice weekly, once as a ~2 year run and
  once as a 19/20 year run that also builds the ILAMB leaderboard

Buildkite ages job logs and artifacts out, so this mirrors them into the
project while they still exist. Everything derived from a log (SYPD,
walltime per step, how far a run got before dying) is parsed downstream in
``notebooks/analyze-weekly.ipynb`` -- the log format has already changed
once, and keeping the raw text means a parser fix can be applied to history
rather than only to jobs run after the fix.

Leaderboard RMSEs are not in the coupler logs, so each build's
``*leaderboard_rmse.csv`` artifacts are mirrored too. ClimaCoupler writes
``output/<run>/artifacts/leaderboard_rmse.csv`` (CliMA's RMSE per variable and
season against observations, next to the CMIP6 models' median and best) and
ClimaLand writes ``<run dir>/<data source>_leaderboard_rmse.csv`` (RMSE and
bias against ERA5, ILAMB, etc.). Both started on 2026-11-09
(ClimaLand.jl#1950, ClimaCoupler.jl#2206), so older builds have none.
Artifacts age out of Buildkite sooner than logs, so these are mirrored as
soon as a build settles.

Requires ``BUILDKITE_PAT`` in the environment or in the repo's ``.env``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from buildkite_api import (
    BASE,
    get,
    list_builds,
    load_token,
    mirror_artifacts,
    write_json,
)

OUT_DIR = "data/buildkite-weekly"
LEADERBOARD_DIR = "data/buildkite-weekly-leaderboards"
LEADERBOARD_ARTIFACT = re.compile(r"leaderboard_rmse\.csv$")

# Which jobs to mirror, per pipeline. The coupler and land pipelines name
# their simulation steps completely differently, so the include pattern is
# per-pipeline rather than one union that would be hard to keep tight.
_COUPLER_JOBS = r"AMIP|CMIP|Slabplanet|Aquaplanet|ClimaAtmos"

PIPELINES = {
    "climacoupler-amip": _COUPLER_JOBS,
    "climacoupler-longruns": _COUPLER_JOBS,
    "climacoupler-cpu-gpu-benchmarks": _COUPLER_JOBS,
    # Runs twice weekly: a ~2 year run plus a 19/20 year run that also
    # builds the ILAMB leaderboard.
    "climaland-long-runs": r"Snowy Land|Soil|Bucket",
}

# Simulation jobs only. Init, reporting, profiling (nsys), Slack uploads,
# ILAMB leaderboard setup and the rsync-to-Azure steps carry no run metrics.
JOB_EXCLUDE = re.compile(
    r"nsys|slack|compare|ilamb|azure|:pipeline:|\binit\b", re.I
)

# A job whose build is still in flight can gain more log later, so only
# mirror jobs whose own state has settled.
TERMINAL_JOB_STATES = {
    "passed",
    "failed",
    "canceled",
    "broken",
    "timed_out",
    "waiting_failed",
    "blocked_failed",
    "skipped",
}


def job_is_wanted(job: dict, include: re.Pattern) -> bool:
    name = job.get("name") or ""
    if job.get("type") != "script" or not name:
        return False
    return bool(include.search(name)) and not JOB_EXCLUDE.search(name)


def mirror_logs(pipeline: str, builds: list, token: str) -> int:
    """Mirror the logs of one pipeline's settled jobs. Returns # fetched."""
    include = re.compile(PIPELINES[pipeline], re.I)
    out_dir = os.path.join(OUT_DIR, pipeline)
    os.makedirs(out_dir, exist_ok=True)

    tasks = []
    skipped = 0
    for build in builds:
        for job in build["jobs"]:
            if not job_is_wanted(job, include):
                continue
            if job.get("state") not in TERMINAL_JOB_STATES:
                continue
            path = os.path.join(out_dir, f"{build['number']}-{job['id']}.json")
            if os.path.exists(path):
                # Already mirrored, and a settled job's log never changes
                skipped += 1
                continue
            tasks.append((build, job, path))

    def mirror_one(task) -> bool:
        build, job, path = task
        log = get(
            f"{BASE}/{pipeline}/builds/{build['number']}/jobs/{job['id']}/log",
            token,
        )
        content = (log or {}).get("content", "")
        if not content.strip():
            # Log has aged out or the job produced none; nothing to mirror
            return False
        record = {
            "pipeline": pipeline,
            "build_number": build.get("number"),
            "build_id": build.get("id"),
            "build_state": build.get("state"),
            "build_created_at": build.get("created_at"),
            "build_finished_at": build.get("finished_at"),
            "commit": build.get("commit"),
            "branch": build.get("branch"),
            "job_id": job.get("id"),
            "job_name": job.get("name"),
            "job_state": job.get("state"),
            "job_command": job.get("command"),
            "job_created_at": job.get("created_at"),
            "job_started_at": job.get("started_at"),
            "job_finished_at": job.get("finished_at"),
            "raw_log_txt": content,
        }
        write_json(path, record)
        return True

    print(
        f"{pipeline}: {len(builds)} builds, {len(tasks)} new jobs "
        f"({skipped} already mirrored)"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = sum(pool.map(mirror_one, tasks))
    return fetched


def mirror_leaderboards(pipeline: str, builds: list, token: str) -> int:
    """Mirror the leaderboard RMSE CSVs of one pipeline's settled builds.

    See ``buildkite_api.mirror_artifacts`` for the layout. Returns the
    number of CSVs downloaded.
    """
    fetched = mirror_artifacts(
        pipeline, builds, token, LEADERBOARD_ARTIFACT, LEADERBOARD_DIR
    )
    print(f"{pipeline}: {fetched} new leaderboard CSVs")
    return fetched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-builds",
        type=int,
        default=30,
        help="How many recent builds per pipeline to scan. Jobs already "
        "mirrored are skipped, so this only needs to exceed the number of "
        "builds since the last run. Raise it to backfill.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Only mirror builds from this branch.",
    )
    args = parser.parse_args()

    token = load_token()

    total_logs = 0
    total_leaderboards = 0
    for pipeline in PIPELINES:
        builds = list_builds(pipeline, token, args.max_builds, args.branch)
        if not builds:
            print(f"{pipeline}: no builds returned", file=sys.stderr)
            continue
        total_logs += mirror_logs(pipeline, builds, token)
        total_leaderboards += mirror_leaderboards(pipeline, builds, token)
    print(f"Mirrored {total_logs} new jobs into {OUT_DIR}")
    print(
        f"Mirrored {total_leaderboards} new leaderboard CSVs into "
        f"{LEADERBOARD_DIR}"
    )


if __name__ == "__main__":
    main()
