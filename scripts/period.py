"""Read the period a scheduled stage is running for.

Stages with a ``kind: schedule`` input in calkit.yaml rerun once per period,
and Calkit writes the current period to a file under .calkit/local before
each run, e.g., ``2026-11-04`` for a daily UTC schedule. Reading it, rather
than the clock, keeps a run's cutoff in step with the period Calkit thinks it
is, including when ``CALKIT_NOW`` is set to redo a past day.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PERIODS_DIR = REPO_ROOT / ".calkit" / "local" / "current-periods"


def current_day() -> date:
    """The UTC day this run is for, from a daily UTC schedule input.

    Falls back to today in UTC when the script runs outside the pipeline
    and no period file has been written yet.
    """
    path = PERIODS_DIR / "1-day-utc.txt"
    if path.exists():
        return date.fromisoformat(path.read_text().strip())
    return datetime.now(timezone.utc).date()
