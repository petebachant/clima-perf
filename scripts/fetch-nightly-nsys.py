"""Mirror the nightly coarse AMIP's Nsight Systems GPU kernel summaries.

ClimaCoupler's ``climacoupler-coarse-nightly-amip`` pipeline has a step,
"nsys AMIP with prognostic EDMF", that profiles a short GPU run of the
flagship AMIP configuration (``amip_progedmf_1m_land_he16``) and, since
2026-11-16 (ClimaCoupler.jl 5130b9d1), runs
``nsys stats --report cuda_gpu_kern_sum --format csv`` on it, uploading
``report_amip_progedmf_land_cuda_gpu_kern_sum.csv``: one row per GPU kernel
with its share of GPU time, total time, call count, and timing spread over
the profiled window.

Only scheduled builds are mirrored. The same pipeline also runs downstream
tests of upstream PRs and hand-started builds of feature branches, and
their kernels describe that branch, not main. Each CSV is saved verbatim
(~75 KB) before Buildkite's retention window removes it; kernel names are
normalized and ranked downstream in ``analyze-nsys-kernels.py``.

Requires ``BUILDKITE_PAT`` in the environment or in the repo's ``.env``.
"""

from __future__ import annotations

import argparse
import re

from buildkite_api import list_builds, load_token, mirror_artifacts

PIPELINE = "climacoupler-coarse-nightly-amip"
OUT_DIR = "data/buildkite-nightly-nsys"
KERNEL_SUMMARY = re.compile(r"cuda_gpu_kern_sum\.csv$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-builds",
        type=int,
        default=60,
        help="How many recent builds to scan, scheduled or not. The "
        "pipeline also runs many downstream-test builds a day, so this "
        "covers fewer nights than it sounds. Raise it to backfill.",
    )
    args = parser.parse_args()

    token = load_token()
    builds = list_builds(PIPELINE, token, args.max_builds, "main")
    nightly = [b for b in builds if b.get("source") == "schedule"]
    fetched = mirror_artifacts(
        PIPELINE, nightly, token, KERNEL_SUMMARY, OUT_DIR
    )
    print(
        f"{PIPELINE}: {len(nightly)} scheduled of {len(builds)} builds, "
        f"{fetched} new kernel summaries into {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
