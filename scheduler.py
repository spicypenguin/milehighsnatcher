#!/usr/bin/env python3
"""
MileHighSnatcher scheduler daemon.
Runs monitor.run() on a configurable schedule (default: 07:00 and 19:00).
Times are interpreted in the container's TZ (set TZ env var to adjust, e.g. TZ=America/New_York).

Designed as the Docker ENTRYPOINT for Portainer/Synology deployment.
"""

import logging
import os
import signal
import sys
import time

import schedule

import monitor

log = logging.getLogger("scheduler")


def job() -> None:
    try:
        monitor.run()
    except Exception:
        log.exception("Unhandled exception in monitor.run() — will retry at next scheduled time.")


def parse_run_times() -> list[str]:
    """Read MONITOR_RUN_TIMES env var, defaulting to 07:00 and 19:00."""
    raw = os.getenv("MONITOR_RUN_TIMES", "07:00,19:00")
    times = [t.strip() for t in raw.split(",") if t.strip()]
    if not times:
        times = ["07:00", "19:00"]
    return times


def main() -> None:
    run_times = parse_run_times()
    tz = os.getenv("TZ", "UTC")
    log.info("Scheduler starting. TZ=%s. Scheduled run times: %s", tz, ", ".join(run_times))

    for t in run_times:
        schedule.every().day.at(t).do(job)

    # Graceful shutdown on SIGTERM (Docker stop) and SIGINT (Ctrl-C).
    def _shutdown(signum, _frame):
        log.info("Signal %d received — shutting down cleanly.", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run once immediately on startup so you get fresh data after a deploy/restart.
    log.info("Running initial check on startup...")
    job()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
