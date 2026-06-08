"""
⚠️  DORMANT MODULE — DO NOT RE-ENABLE WITHOUT READING THIS FIRST  ⚠️
================================================================

This module is preserved as a CAUTIONARY REFERENCE, not active code.
Nothing in the running app calls it. The scheduler loop that used to
drive it was removed, and so was the /ingest API endpoint.

Why it was abandoned: this was an attempt to maintain a local cache of
today's airport arrivals + departures by paginating FlightAware's
`/airports/{code}/flights/arrivals` and `/flights/departures` endpoints.
On FA's personal tier those paginated endpoints cost roughly $1-2 PER CALL.

In June 2026 a 30-min ingest cycle (with retries + backfill mode) racked
up ~280 calls in 5 hours and contributed to a ~$750 monthly FlightAware
bill on what should have been a free-tier install. Full incident notes
live in `cost_tracker.py` and the project README.

If you're tempted to re-enable this, FIRST:
  1. Read `cost_tracker.py` and the README "API costs" section
  2. Verify FA's current pricing for paginated endpoints — do not assume
     they're cheap because the docs make them sound simple
  3. Confirm there's no cheaper alternative (FA's free `/flights/counts`
     endpoint covers most "how many flights today?" use cases — that's
     what the dashboard currently uses)

If you genuinely need this functionality, please open an issue first so
we can discuss whether it can be made safe.

================================================================

Original purpose (for historical context — DO NOT TREAT AS ACTIVE SPEC):

FA's `/airports/{code}/flights/counts` only exposes `scheduled_arrivals`
(forward-looking rolling window) and `departed` (today so far). There was
no field for "arrived today so far" — this module computed it by counting
unique flights whose actual_on was set and >= midnight via the paginated
`/flights/arrivals` endpoint.
"""
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "airport_movements.db"
_lock = Lock()

from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS airport_movements (
                fa_flight_id TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,    -- 'arrival' or 'departure'
                airport_code TEXT NOT NULL,    -- 'DCA'
                ident        TEXT,             -- callsign / flight number
                scheduled_at TEXT,             -- ISO UTC
                actual_at    TEXT,             -- ISO UTC, NULL until it happens
                fetched_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mov_count "
            "ON airport_movements(airport_code, kind, actual_at)"
        )


def upsert(
    *,
    fa_flight_id: str,
    kind: str,
    airport_code: str,
    ident: Optional[str],
    scheduled_at: Optional[str],
    actual_at: Optional[str],
) -> None:
    """Insert or update. If a record exists, we KEEP a previously-set actual_at
    (planes don't un-land), but we update everything else."""
    if not fa_flight_id:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO airport_movements
                (fa_flight_id, kind, airport_code, ident, scheduled_at, actual_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fa_flight_id) DO UPDATE SET
                kind = excluded.kind,
                airport_code = excluded.airport_code,
                ident = excluded.ident,
                scheduled_at = excluded.scheduled_at,
                actual_at = COALESCE(excluded.actual_at, airport_movements.actual_at),
                fetched_at = excluded.fetched_at
            """,
            (fa_flight_id, kind, airport_code.upper(), ident, scheduled_at, actual_at, now_iso),
        )


def count_today(airport_code: str, kind: str) -> int:
    """Count movements with actual_at >= local midnight today (UTC-converted)."""
    midnight_utc_iso = local_midnight_today_utc()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM airport_movements "
            "WHERE airport_code = ? AND kind = ? AND actual_at IS NOT NULL AND actual_at >= ?",
            (airport_code.upper(), kind, midnight_utc_iso),
        ).fetchone()
    return int(row["n"]) if row else 0


def local_midnight_today_utc() -> str:
    """Return today's local-midnight as a UTC ISO string for FA `start` param."""
    local_now = datetime.now(_LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def smart_window(airport_code: str, kind: str, overlap_minutes: int = 60) -> tuple[str, Optional[str]]:
    """Choose (start_iso, end_iso) for the next FA ingest call.

    Three regimes:
      1. No data today → (midnight, None): full backfill, fetch everything forward
      2. Data exists but starts AFTER midnight by >30min (gap at start of day)
         → (midnight, oldest_today): backfill the missing morning side. Each
         cycle chips away at the gap; eventually it fills in and we switch
         to regime 3.
      3. No gap, contiguous from midnight → (newest - overlap, None): standard
         incremental fetch forward to catch new flights.
    """
    midnight = local_midnight_today_utc()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT MIN(actual_at) AS oldest, MAX(actual_at) AS newest "
            "FROM airport_movements WHERE airport_code = ? AND kind = ? "
            "AND actual_at IS NOT NULL AND actual_at >= ?",
            (airport_code.upper(), kind, midnight),
        ).fetchone()
    if not row or not row["oldest"]:
        return (midnight, None)

    midnight_dt = datetime.fromisoformat(midnight.replace("Z", "+00:00"))
    oldest_dt = datetime.fromisoformat(row["oldest"].replace("Z", "+00:00"))
    gap_seconds = (oldest_dt - midnight_dt).total_seconds()
    if gap_seconds > 1800:  # >30min gap at start of day → backfill mode
        return (midnight, row["oldest"])

    newest_dt = datetime.fromisoformat(row["newest"].replace("Z", "+00:00"))
    overlap_start = (newest_dt - timedelta(minutes=overlap_minutes)).astimezone(timezone.utc)
    return (overlap_start.isoformat().replace("+00:00", "Z"), None)


async def ingest_today(enricher, airport_code: str) -> tuple[int, int]:
    """Fetch today's arrivals + departures from FA, upsert each. Uses incremental
    start (start=latest_actual_at - 1h) to keep steady-state cost low, and
    relies on paginate_airport_flights's inter-call sleeps to avoid the
    per-minute rate limit during cold-start backfills.

    Returns (n_arrivals_seen, n_departures_seen) for logging.
    """
    n_arr = 0
    arr_start, arr_end = smart_window(airport_code, "arrival")
    async for flight in enricher.paginate_airport_flights(airport_code, "arrivals", arr_start, end_iso=arr_end):
        upsert(
            fa_flight_id=flight.get("fa_flight_id"),
            kind="arrival",
            airport_code=airport_code,
            ident=flight.get("ident"),
            scheduled_at=flight.get("scheduled_on"),
            actual_at=flight.get("actual_on"),
        )
        n_arr += 1

    # Sleep between kinds — gives FA's per-minute window plenty of recovery time
    # before we hit the second endpoint.
    await asyncio.sleep(8)

    n_dep = 0
    dep_start, dep_end = smart_window(airport_code, "departure")
    async for flight in enricher.paginate_airport_flights(airport_code, "departures", dep_start, end_iso=dep_end):
        upsert(
            fa_flight_id=flight.get("fa_flight_id"),
            kind="departure",
            airport_code=airport_code,
            ident=flight.get("ident"),
            scheduled_at=flight.get("scheduled_off"),
            actual_at=flight.get("actual_off"),
        )
        n_dep += 1
    return (n_arr, n_dep)
