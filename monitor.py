#!/usr/bin/env python3
"""
JAL First Class JFK→HND Award Monitor
Availability data courtesy of seats.aero (https://seats.aero)

Queries the seats.aero Pro partner API for JAL first-class award space on
JFK→HND, filters for A350-operated trips, deduplicates alerts, and notifies
via macOS notification (local) or email (Docker/headless).

Can be run standalone:
    python3 monitor.py

Or imported and called by the scheduler daemon (scheduler.py):
    from monitor import run
    run()
"""

import json
import logging
import logging.handlers
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Bootstrap ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Constants ──────────────────────────────────────────────────────────────────

API_BASE = "https://seats.aero/partnerapi"

ORIGIN      = "JFK"
DESTINATION = "HND"
CABIN       = "first"   # seats.aero cabin string for first class
CARRIER     = "JL"      # Japan Airlines IATA code

# IATA aircraft type codes for Airbus A350 family.
# JAL operates A350-1000 (35K) on JFK-HND; include sibling codes defensively.
A350_CODES = {"359", "351", "35K", "A359", "A351", "A35K"}

# Known A350 service on this route by flight number (fallback when
# the aircraft code field is absent or uses a non-standard value).
A350_FLIGHT_NUMBERS = {"JL44", "JL 44"}

# How many days forward to search (JAL partner award window ≈ 330 days).
SEARCH_DAYS_AHEAD = 330

# Dedup entries expire after this many days so we re-alert if availability
# disappears and returns (e.g. a seat is released again after a cancellation).
DEDUP_TTL_DAYS = 30

# Allow an explicit data directory (useful in Docker where /data is a volume).
_DATA_DIR  = Path(os.getenv("DATA_DIR", "")).expanduser() if os.getenv("DATA_DIR") else BASE_DIR

STATE_FILE = _DATA_DIR / "seen_flights.json"
LOG_FILE   = _DATA_DIR / "logs" / "monitor.log"

# macOS notifications can be suppressed in non-GUI / headless environments.
DISABLE_MACOS_NOTIFY = os.getenv("DISABLE_MACOS_NOTIFICATIONS", "").lower() in ("1", "true", "yes")

# ── Logging ────────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure logging once; safe to call multiple times (no-op after first call)."""
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. by scheduler.py import)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: 5 MB per file, keep 3 backups.
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


_setup_logging()
log = logging.getLogger(__name__)

# ── Dedup state ────────────────────────────────────────────────────────────────

def load_seen() -> dict[str, str]:
    """Load previously-notified keys, pruning entries older than DEDUP_TTL_DAYS."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw: dict[str, str] = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read %s — starting fresh.", STATE_FILE)
        return {}

    cutoff = datetime.now() - timedelta(days=DEDUP_TTL_DAYS)
    pruned = {
        k: v for k, v in raw.items()
        if datetime.fromisoformat(v) >= cutoff
    }
    if len(pruned) < len(raw):
        log.debug("Pruned %d expired dedup entries.", len(raw) - len(pruned))
    return pruned


def save_seen(seen: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def dedup_key(flight_numbers: str, date: str, source: str) -> str:
    """Stable key for one notifiable event."""
    return f"{flight_numbers.strip()}|{date}|{source}"

# ── API helpers ────────────────────────────────────────────────────────────────

def _make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Partner-Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "MileHighSnatcher/1.0",
    })
    return s


def api_get(session: requests.Session, path: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}/{path}"
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def search_availability(session: requests.Session) -> list[dict]:
    """
    Return all first-class availability objects for JFK→HND on JAL.
    Uses the seats.aero Cached Search endpoint.
    Data courtesy of seats.aero.
    """
    today = datetime.now().date()
    params = {
        "origin_airport":      ORIGIN,
        "destination_airport": DESTINATION,
        "cabins":              CABIN,
        "carriers":            CARRIER,
        "start_date":          (today + timedelta(days=1)).isoformat(),
        "end_date":            (today + timedelta(days=SEARCH_DAYS_AHEAD)).isoformat(),
        "take":                1000,
        "only_direct_flights": "true",
    }

    log.info(
        "Querying seats.aero Cached Search: %s → %s | cabin=%s | carrier=%s",
        ORIGIN, DESTINATION, CABIN, CARRIER,
    )
    payload = api_get(session, "search", params)
    results: list[dict] = payload.get("data", [])
    log.info(
        "  seats.aero returned %d availability object(s)  "
        "(data courtesy of seats.aero — https://seats.aero)",
        len(results),
    )
    return results


def fetch_trips(session: requests.Session, availability_id: str) -> list[dict]:
    """Fetch trip-level (flight-level) details for one availability object."""
    payload = api_get(session, f"trips/{availability_id}")
    return payload.get("data", [])

# ── A350 detection ─────────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return s.strip().upper().replace("-", "").replace(" ", "")


def is_a350_trip(trip: dict) -> bool:
    """
    Return True if this trip is operated by an Airbus A350.

    Detection order:
      1. Known A350 flight number (JL44) in top-level FlightNumbers string.
      2. AircraftCode / AircraftName on any segment matches A350 type codes.
      3. The literal string "A350" appears anywhere in the aircraft name/code.
    """
    # 1. Flight-number shortcut
    raw_fns = trip.get("FlightNumbers", "")
    trip_fns = {_normalise(fn) for fn in raw_fns.split(",")}
    if trip_fns & {_normalise(fn) for fn in A350_FLIGHT_NUMBERS}:
        return True

    # 2 & 3. Segment aircraft codes
    for seg in trip.get("AvailabilitySegments", []):
        code = _normalise(seg.get("AircraftCode") or "")
        name = _normalise(seg.get("AircraftName") or "")
        if code in A350_CODES or name in A350_CODES:
            return True
        if "A350" in code or "A350" in name:
            return True

    return False

# ── Notifications ──────────────────────────────────────────────────────────────

def _build_alert_text(trips: list[dict], avail: dict) -> tuple[str, str, str]:
    """Return (title, short_message, long_body)."""
    date    = avail.get("Date", "?")
    source  = avail.get("Source", "?").upper()
    seats   = avail.get("FRemainingSeats", "?")
    miles   = avail.get("FMileageCost", "?")

    flight_nums = ", ".join(
        dict.fromkeys(          # preserve order, deduplicate
            t.get("FlightNumbers", "?").strip() for t in trips
        )
    )

    title   = "JAL First Class Available!"
    short   = (
        f"{ORIGIN}\u2192{DESTINATION}  {date}  {flight_nums}  "
        f"{seats} seat(s) \u00b7 {miles} miles \u00b7 via {source}"
    )
    body    = (
        f"{short}\n\n"
        f"Search: https://seats.aero/search/{ORIGIN}-{DESTINATION}\n\n"
        "---\n"
        "Availability data provided by seats.aero (https://seats.aero).\n"
        "Book directly through your chosen mileage program."
    )
    return title, short, body


def notify_macos(title: str, message: str) -> None:
    if DISABLE_MACOS_NOTIFY:
        log.debug("macOS notifications disabled — skipping.")
        return
    safe_title   = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}" '
        f'sound name "Ping"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
        log.info("macOS notification sent.")
    except FileNotFoundError:
        log.warning("osascript not found — is this macOS?")
    except subprocess.CalledProcessError as exc:
        log.warning("osascript error: %s", exc.stderr.decode().strip())
    except Exception as exc:
        log.warning("macOS notification failed: %s", exc)


def notify_pushover(title: str, message: str) -> None:
    token    = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not token or not user_key:
        return  # not configured — silently skip

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    token,
                "user":     user_key,
                "title":    title,
                "message":  message,
                "priority": 1,      # high priority — bypasses quiet hours
                "sound":    "cashregister",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Pushover notification sent.")
    except Exception as exc:
        log.error("Pushover notification failed: %s", exc)


def notify_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return  # email not configured — silently skip

    port     = int(os.getenv("SMTP_PORT", "587"))
    user     = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    to_addr  = os.getenv("ALERT_EMAIL", user).strip()

    if not to_addr:
        log.warning("SMTP_HOST is set but ALERT_EMAIL (or SMTP_USER) is empty — skipping email.")
        return

    msg             = MIMEText(body, "plain", "utf-8")
    msg["Subject"]  = subject
    msg["From"]     = user or "monitor@milessnatcher.local"
    msg["To"]       = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        log.info("Email alert sent to %s.", to_addr)
    except Exception as exc:
        log.error("Email notification failed: %s", exc)


def send_alert(trips: list[dict], avail: dict) -> None:
    title, short, body = _build_alert_text(trips, avail)
    log.info("ALERT >>>  %s", short)
    notify_macos(title, short)
    notify_pushover(title, short)
    notify_email(f"[MileHighSnatcher] {title}", body)

# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    """
    Single monitoring pass. Safe to call repeatedly (e.g. from scheduler.py).
    Logs errors and returns rather than calling sys.exit() so the scheduler
    daemon can survive transient failures.
    """
    api_key = os.environ.get("SEATS_AERO_API_KEY", "").strip()
    if not api_key:
        log.error("SEATS_AERO_API_KEY is not set. Check your .env file or container environment.")
        return

    log.info("=" * 64)
    log.info("MileHighSnatcher — JAL F %s→%s monitor starting", ORIGIN, DESTINATION)
    log.info("Availability data courtesy of seats.aero (https://seats.aero)")
    log.info("=" * 64)

    session   = _make_session(api_key)
    seen      = load_seen()
    new_seen  = dict(seen)
    alerts    = 0
    checked   = 0

    # ── Search ────────────────────────────────────────────────────────────────
    try:
        availability_list = search_availability(session)
    except requests.HTTPError as exc:
        log.error("seats.aero API HTTP error: %s", exc)
        return
    except requests.RequestException as exc:
        log.error("Network error querying seats.aero: %s", exc)
        return

    # ── Evaluate each availability object ────────────────────────────────────
    for avail in availability_list:
        if not avail.get("FAvailable"):
            continue

        avail_id = avail["ID"]
        date     = avail.get("Date", "")
        source   = avail.get("Source", "")
        seats    = avail.get("FRemainingSeats", "?")
        miles    = avail.get("FMileageCost", "?")

        log.info(
            "FAvailable on %s via %-14s — %s seat(s) @ %s miles  [id=%s]",
            date, source, seats, miles, avail_id,
        )
        checked += 1

        # Fetch trip-level details to determine aircraft type.
        try:
            trips = fetch_trips(session, avail_id)
        except requests.HTTPError as exc:
            log.warning("  Could not fetch trips for %s: %s", avail_id, exc)
            continue
        except Exception as exc:
            log.warning("  Unexpected error fetching trips for %s: %s", avail_id, exc)
            continue

        if not trips:
            log.debug("  No trips returned for %s — skipping.", avail_id)
            continue

        # Filter for A350-operated first-class trips.
        a350_trips = [
            t for t in trips
            if t.get("Cabin", "").lower() == "first" and is_a350_trip(t)
        ]

        if not a350_trips:
            log.info("  → No A350 first-class trips found; skipping.")
            continue

        log.info(
            "  → A350 first-class trips: %s",
            [t.get("FlightNumbers", "?") for t in a350_trips],
        )

        # Dedup: build one notification per unique (flight_numbers, date, source).
        new_trips: list[dict] = []
        for trip in a350_trips:
            fn  = trip.get("FlightNumbers", avail_id).strip()
            key = dedup_key(fn, date, source)
            if key in seen:
                log.info(
                    "  → Already notified: %s on %s via %s — skipping.",
                    fn, date, source,
                )
            else:
                new_trips.append(trip)
                new_seen[key] = datetime.now().isoformat()

        if not new_trips:
            continue

        send_alert(new_trips, avail)
        alerts += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    save_seen(new_seen)
    log.info(
        "Run complete — checked %d F-available object(s), sent %d new alert(s).",
        checked, alerts,
    )
    log.info("Data courtesy of seats.aero — https://seats.aero")
    log.info("=" * 64)


if __name__ == "__main__":
    api_key = os.environ.get("SEATS_AERO_API_KEY", "").strip()
    if not api_key:
        sys.exit("ERROR: SEATS_AERO_API_KEY is not set. Check your .env file.")
    run()
