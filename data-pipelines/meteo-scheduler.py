"""Run meteo-fetch.py every 10 minutes as a blocking loop.

Usage:
    uv run python meteo-scheduler.py

The script fetches current Swiss meteo data from MeteoSwiss (geo.admin),
converts it to GeoZarr format on the shared 100 m LV95 grid, uploads to
local MinIO, and writes a meteo_manifest.json so the Angular frontend can
detect fresh data.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

INTERVAL_SECONDS = int(os.getenv("METEO_FETCH_INTERVAL_SECONDS", "600"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("METEO_FETCH_MAX_FAILURES", "3"))
SCRIPT = Path(__file__).parent / "meteo-fetch.py"


def run_once() -> bool:
    print("=" * 60)
    print(f"Starting meteo fetch at {datetime.now(timezone.utc).isoformat()}…")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--upload"],
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("[ERROR] meteo-fetch.py timed out after 300 seconds", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"[ERROR] meteo-fetch.py exited with code {result.returncode}",
            file=sys.stderr,
        )
        return False
    return True


if __name__ == "__main__":
    print(f"Meteo scheduler started. Interval: {INTERVAL_SECONDS // 60} minutes.")
    print(f"Script: {SCRIPT}")
    print("Press Ctrl+C to stop.")
    consecutive_failures = 0
    try:
        while True:
            success = run_once()
            if success:
                consecutive_failures = 0
                sleep_seconds = INTERVAL_SECONDS
            else:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"[CRITICAL] Reached {consecutive_failures} consecutive failures, exiting.",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)
                sleep_seconds = min(60 * (2 ** consecutive_failures), INTERVAL_SECONDS)
                print(
                    f"[WARNING] Failure count={consecutive_failures}. "
                    f"Retrying in {sleep_seconds} seconds.",
                    file=sys.stderr,
                )

            print(f"Sleeping {sleep_seconds // 60} minutes until next fetch…")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("\nScheduler stopped.")
