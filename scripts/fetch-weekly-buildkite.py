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

Note that the land RMSE metrics behind the O3 OKRs are *not* in these logs.
They are computed in ClimaLand's leaderboard extension and rendered straight
to PNG, so mirroring logs gets throughput and stability for the land runs
but not their error metrics.

Requires ``BUILDKITE_PAT`` in the environment or in the repo's ``.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import dotenv

BASE = "https://api.buildkite.com/v2/organizations/clima/pipelines"
OUT_DIR = "data/buildkite-weekly"

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

# Buildkite's REST API allows 200 requests/minute per user. Keep a global
# floor on the gap between requests so the thread pool can't trip it.
_gate = threading.Lock()
_last_request = [0.0]
MIN_REQUEST_GAP_S = 0.35


def throttle() -> None:
    with _gate:
        wait = MIN_REQUEST_GAP_S - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def get(url: str, token: str) -> dict | list | None:
    """GET and parse JSON, returning None when there is nothing to fetch.

    Buildkite returns an empty body for a job that never ran and a 404 once
    a log has aged out of retention; both mean "no data", not an error.

    The ``Accept`` header matters. Without it the log endpoint content-
    negotiates down to an HTML transcript instead of JSON.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        throttle()
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
            return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return None
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def job_is_wanted(job: dict, include: re.Pattern) -> bool:
    name = job.get("name") or ""
    if job.get("type") != "script" or not name:
        return False
    return bool(include.search(name)) and not JOB_EXCLUDE.search(name)


def mirror_pipeline(
    pipeline: str, token: str, max_builds: int, branch: str
) -> tuple[int, int]:
    """Mirror recent jobs for one pipeline. Returns (fetched, skipped)."""
    include = re.compile(PIPELINES[pipeline], re.I)
    out_dir = os.path.join(OUT_DIR, pipeline)
    os.makedirs(out_dir, exist_ok=True)

    builds = get(
        f"{BASE}/{pipeline}/builds?per_page={max_builds}&branch={branch}",
        token,
    )
    if not builds:
        print(f"{pipeline}: no builds returned", file=sys.stderr)
        return 0, 0

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
        # Write via a temp file so an interrupted run can't leave a
        # truncated record that the next run would skip as "already done"
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f, indent=1)
        os.replace(tmp, path)
        return True

    print(
        f"{pipeline}: {len(builds)} builds, {len(tasks)} new jobs "
        f"({skipped} already mirrored)"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = sum(pool.map(mirror_one, tasks))
    return fetched, skipped


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

    dotenv.load_dotenv()
    token = os.getenv("BUILDKITE_PAT")
    if not token:
        raise RuntimeError(
            "No Buildkite token found; set BUILDKITE_PAT in the environment "
            "or in the repo's .env file"
        )

    total_fetched = 0
    for pipeline in PIPELINES:
        fetched, _ = mirror_pipeline(
            pipeline, token, args.max_builds, args.branch
        )
        total_fetched += fetched
    print(f"Mirrored {total_fetched} new jobs into {OUT_DIR}")


if __name__ == "__main__":
    main()
