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
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import dotenv

BASE = "https://api.buildkite.com/v2/organizations/clima/pipelines"
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

# A build in one of these states won't gain more artifacts unless a job in
# it is retried, which flips the build back to running.
TERMINAL_BUILD_STATES = {"passed", "failed", "canceled", "skipped", "not_run"}

# Re-scan a settled build's artifacts for this long after it finishes, to
# pick up jobs retried after the build first settled.
LEADERBOARD_RESCAN_DAYS = 14

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect)


def download_artifact(url: str, token: str) -> bytes | None:
    """Download an artifact's bytes, or None if it has aged out.

    The download endpoint answers with a redirect to a signed S3 URL. It has
    to be followed by hand: urllib would forward the Buildkite token, and S3
    rejects a request that carries a second form of authorization.
    """
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    for attempt in range(3):
        throttle()
        try:
            try:
                with _no_redirect.open(req, timeout=180) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise
                location = exc.headers["Location"]
            with urllib.request.urlopen(location, timeout=180) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                return None
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def list_artifacts(pipeline: str, build_number: int, token: str) -> list:
    """All of a build's artifacts, following pagination."""
    artifacts = []
    page = 1
    while True:
        batch = get(
            f"{BASE}/{pipeline}/builds/{build_number}/artifacts"
            f"?per_page=100&page={page}",
            token,
        )
        artifacts.extend(batch or [])
        if not batch or len(batch) < 100:
            return artifacts
        page += 1


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


def write_json(path: str, record: dict) -> None:
    # Via a temp file, so an interrupted run can't leave a truncated file
    # that the next run would skip as already done
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=1)
    os.replace(tmp, path)


def _parse_time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def mirror_leaderboards(pipeline: str, builds: list, token: str) -> int:
    """Mirror the leaderboard RMSE CSVs of one pipeline's settled builds.

    Each build gets ``<build>.json``, an index of its leaderboard artifacts
    with the job each came from (an empty list for a build that has none),
    and each CSV is saved verbatim as ``<build>-<job id>/<file name>``. The
    full artifact path is in the index; it isn't kept on disk because the
    coupler's starts with ``output/``, which the repo's .gitignore excludes.
    The index is what marks a build as done. Jobs are recorded from
    the build itself rather than the log mirror, since the job that writes a
    leaderboard (e.g., ClimaLand's ILAMB step) isn't always one whose log is
    mirrored.

    Returns the number of CSVs downloaded.
    """
    out_dir = os.path.join(LEADERBOARD_DIR, pipeline)
    os.makedirs(out_dir, exist_ok=True)
    now = time.time()
    fetched = 0
    for build in builds:
        if build.get("state") not in TERMINAL_BUILD_STATES:
            continue
        index_path = os.path.join(out_dir, f"{build['number']}.json")
        finished = build.get("finished_at")
        age_days = (
            (now - _parse_time(finished)) / 86400 if finished else math.inf
        )
        if os.path.exists(index_path) and age_days > LEADERBOARD_RESCAN_DAYS:
            continue
        jobs = {job["id"]: job for job in build["jobs"] if job.get("id")}
        records = []
        used = set()
        for artifact in list_artifacts(pipeline, build["number"], token):
            if not LEADERBOARD_ARTIFACT.search(artifact.get("path") or ""):
                continue
            if artifact.get("state") != "finished":
                continue
            job = jobs.get(artifact["job_id"], {})
            local = os.path.join(
                f"{build['number']}-{artifact['job_id']}",
                os.path.basename(artifact["path"]),
            )
            if local in used:
                # Same file name from two directories of one job; keep both
                head, tail = os.path.split(local)
                local = os.path.join(head, f"{artifact['id']}-{tail}")
            used.add(local)
            dest = os.path.join(out_dir, local)
            if not os.path.exists(dest):
                content = download_artifact(artifact["download_url"], token)
                if content is None:
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".tmp", "wb") as f:
                    f.write(content)
                os.replace(dest + ".tmp", dest)
                fetched += 1
            records.append(
                {
                    "artifact_id": artifact.get("id"),
                    "artifact_path": artifact["path"],
                    "local_path": local,
                    "job_id": artifact["job_id"],
                    "job_name": job.get("name"),
                    "job_state": job.get("state"),
                    "job_started_at": job.get("started_at"),
                    "job_finished_at": job.get("finished_at"),
                }
            )
        write_json(
            index_path,
            {
                "pipeline": pipeline,
                "build_number": build.get("number"),
                "build_id": build.get("id"),
                "build_state": build.get("state"),
                "build_created_at": build.get("created_at"),
                "build_finished_at": finished,
                "commit": build.get("commit"),
                "branch": build.get("branch"),
                "artifacts": sorted(records, key=lambda r: r["local_path"]),
            },
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

    dotenv.load_dotenv()
    token = os.getenv("BUILDKITE_PAT")
    if not token:
        raise RuntimeError(
            "No Buildkite token found; set BUILDKITE_PAT in the environment "
            "or in the repo's .env file"
        )

    total_logs = 0
    total_leaderboards = 0
    for pipeline in PIPELINES:
        builds = get(
            f"{BASE}/{pipeline}/builds"
            f"?per_page={args.max_builds}&branch={args.branch}",
            token,
        )
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
