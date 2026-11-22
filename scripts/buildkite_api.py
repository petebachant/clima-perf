"""Shared helpers for mirroring CliMA Buildkite builds and artifacts.

Used by ``fetch-weekly-buildkite.py`` and ``fetch-nightly-nsys.py``.
Requires a Buildkite token, which the callers read from ``BUILDKITE_PAT``.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import dotenv

BASE = "https://api.buildkite.com/v2/organizations/clima/pipelines"

# A build in one of these states won't gain more artifacts unless a job in
# it is retried, which flips the build back to running.
TERMINAL_BUILD_STATES = {"passed", "failed", "canceled", "skipped", "not_run"}

# Re-scan a settled build's artifacts for this long after it finishes, to
# pick up jobs retried after the build first settled.
ARTIFACT_RESCAN_DAYS = 14

# Buildkite's REST API allows 200 requests/minute per user. Keep a global
# floor on the gap between requests so a thread pool can't trip it.
_gate = threading.Lock()
_last_request = [0.0]
MIN_REQUEST_GAP_S = 0.35


def load_token() -> str:
    dotenv.load_dotenv()
    token = os.getenv("BUILDKITE_PAT")
    if not token:
        raise RuntimeError(
            "No Buildkite token found; set BUILDKITE_PAT in the environment "
            "or in the repo's .env file"
        )
    return token


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


def list_builds(
    pipeline: str, token: str, max_builds: int, branch: str
) -> list:
    """A pipeline's most recent builds on a branch, newest first."""
    return (
        get(
            f"{BASE}/{pipeline}/builds?per_page={max_builds}&branch={branch}",
            token,
        )
        or []
    )


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


def write_json(path: str, record: dict) -> None:
    # Via a temp file, so an interrupted run can't leave a truncated file
    # that the next run would skip as already done
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=1)
    os.replace(tmp, path)


def _parse_time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def mirror_artifacts(
    pipeline: str,
    builds: list,
    token: str,
    pattern: re.Pattern,
    out_root: str,
) -> int:
    """Mirror the artifacts matching ``pattern`` from settled builds.

    Each build gets ``<out_root>/<pipeline>/<build>.json``, an index of its
    matching artifacts with the job each came from (an empty list for a
    build that has none), and each artifact is saved verbatim as
    ``<build>-<job id>/<file name>``. The full artifact path is in the
    index; it isn't kept on disk because CliMA's usually start with
    ``output/``, which the repo's .gitignore excludes. The index is what
    marks a build as done. Jobs are recorded from the build itself, since
    the job that writes an artifact isn't always one whose log is mirrored.

    Returns the number of files downloaded.
    """
    out_dir = os.path.join(out_root, pipeline)
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
        if os.path.exists(index_path) and age_days > ARTIFACT_RESCAN_DAYS:
            continue
        jobs = {job["id"]: job for job in build["jobs"] if job.get("id")}
        records = []
        used = set()
        for artifact in list_artifacts(pipeline, build["number"], token):
            if not pattern.search(artifact.get("path") or ""):
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
    return fetched
