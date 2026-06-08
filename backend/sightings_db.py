"""SQLite-backed tail-sighting history. Lifted from PlaneSpotter/history.py."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from .models import EnrichedAircraft

from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)


_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "sightings.db"
_lock = Lock()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registration TEXT,
                callsign TEXT,
                flight_number TEXT,
                aircraft_type TEXT,
                origin_iata TEXT,
                destination_iata TEXT,
                altitude_ft INTEGER,
                distance_nm REAL,
                seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_reg ON sightings(registration)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_flight ON sightings(flight_number)")


def record_sighting(ac: EnrichedAircraft) -> None:
    """Log one sighting. Deduped: don't record the same reg within 10 minutes."""
    now = datetime.now(timezone.utc)
    with _lock, _connect() as conn:
        if ac.registration:
            row = conn.execute(
                "SELECT seen_at FROM sightings WHERE registration = ? ORDER BY seen_at DESC LIMIT 1",
                (ac.registration,),
            ).fetchone()
            if row:
                last = datetime.fromisoformat(row["seen_at"])
                if (now - last).total_seconds() < 600:
                    return
        conn.execute(
            """
            INSERT INTO sightings
              (registration, callsign, flight_number, aircraft_type,
               origin_iata, destination_iata, altitude_ft, distance_nm, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ac.registration,
                ac.callsign,
                ac.flight_number,
                ac.aircraft_type,
                ac.origin_iata,
                ac.destination_iata,
                int(ac.altitude_ft) if ac.altitude_ft else None,
                ac.distance_nm,
                now.isoformat(),
            ),
        )


def get_stats(registration: Optional[str]) -> tuple[int, Optional[datetime], Optional[datetime]]:
    """Return (count, first_seen, last_seen) for a tail."""
    if not registration:
        return (0, None, None)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(seen_at) AS first, MAX(seen_at) AS last "
            "FROM sightings WHERE registration = ?",
            (registration,),
        ).fetchone()
    if not row or row["n"] == 0:
        return (0, None, None)
    return (
        row["n"],
        datetime.fromisoformat(row["first"]) if row["first"] else None,
        datetime.fromisoformat(row["last"]) if row["last"] else None,
    )


def _local_midnight_today_utc_iso() -> str:
    """Today's local-Eastern midnight as a UTC ISO string for SQL date filtering."""
    local_now = datetime.now(_LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()


def count_seen_today(airport_code: str, kind: str) -> int:
    """Count distinct flights we've personally pushed to the board today,
    filtered by direction relative to the configured airport.

    kind:
      - 'arrival'   → destination_iata == airport_code (we saw it landing)
      - 'departure' → origin_iata == airport_code (we saw it taking off)

    "Today" = since local-Eastern midnight. Filters out incomplete records
    where the relevant IATA field wasn't populated (e.g., enrichment never
    completed for that flight).
    """
    code = airport_code.upper()
    midnight_iso = _local_midnight_today_utc_iso()
    column = "destination_iata" if kind == "arrival" else "origin_iata"
    with _lock, _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM sightings "
            f"WHERE seen_at >= ? AND UPPER({column}) = ?",
            (midnight_iso, code),
        ).fetchone()
    return int(row["n"]) if row else 0


def max_sighting_count() -> int:
    """Return the highest sighting count across all tails. Used for the
    'king of the hill' crown — any tail whose count equals this is currently
    the most-seen tail (or tied for it)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sightings WHERE registration IS NOT NULL "
            "GROUP BY registration ORDER BY n DESC LIMIT 1"
        ).fetchone()
    return int(row["n"]) if row else 0


# ICAO type codes that are helicopters (Bell, Sikorsky, Eurocopter/Airbus, Robinson, etc.)
# Used by the helicopter activity dashboard to slice sightings by airframe class.
_HELO_TYPES = {
    "B06", "B06T", "B407", "B412", "B427", "B429", "B430", "B505",  # Bell
    "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "EC20T",  # Eurocopter (legacy)
    "H125", "H130", "H135", "H145", "H155", "H160", "H175",            # Airbus Helicopters
    "AS50", "AS55", "AS65", "AS32", "AS65",                            # Airbus (older AS prefix)
    "S76", "S70", "S92", "S64",                                        # Sikorsky civilian
    "VH60", "VH92", "VH3D",                                            # Marine One family (often ADS-B dark, but defined for completeness)
    "UH60", "UH72", "CH47", "CH53", "MH60",                            # Military variants
    "R22", "R44", "R66",                                               # Robinson
    "MD52", "MD60", "MD80H", "MD500", "MD600", "MD900", "MD90", "H500", "MD90",  # MD Helicopters
    "A109", "A119", "A139", "A169", "A189",                            # Leonardo (formerly Agusta)
    "BK17",                                                              # Kawasaki / MBB
}


def is_helicopter_type(t: Optional[str]) -> bool:
    if not t:
        return False
    return t.upper() in _HELO_TYPES


def _helo_type_sql_list() -> str:
    """Build a parameterizable list of helicopter ICAO codes for SQL IN clauses."""
    return ",".join(f"'{t}'" for t in sorted(_HELO_TYPES))


def helicopter_stats() -> dict:
    """One-shot stats for the helicopter dashboard card.

    Returns:
      - total_helicopters (across all time in DB)
      - today_helicopters (since local midnight)
      - top_types: list of {type, name, n}
      - top_tails: list of {tail, n, last_seen}
      - hourly_today: 24-element list of hourly counts (local ET)
    """
    midnight_iso = _local_midnight_today_utc_iso()
    helos = _helo_type_sql_list()
    with _lock, _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM sightings WHERE aircraft_type IN ({helos})"
        ).fetchone()["n"]
        today = conn.execute(
            f"SELECT COUNT(*) AS n FROM sightings "
            f"WHERE seen_at >= ? AND aircraft_type IN ({helos})",
            (midnight_iso,),
        ).fetchone()["n"]
        types = conn.execute(
            f"SELECT aircraft_type, COUNT(*) AS n FROM sightings "
            f"WHERE aircraft_type IN ({helos}) "
            f"GROUP BY aircraft_type ORDER BY n DESC LIMIT 10"
        ).fetchall()
        tails = conn.execute(
            f"SELECT registration, COUNT(*) AS n, MAX(seen_at) AS last_seen, "
            f"MAX(aircraft_type) AS aircraft_type FROM sightings "
            f"WHERE aircraft_type IN ({helos}) AND registration IS NOT NULL AND registration != '' "
            f"GROUP BY registration ORDER BY n DESC LIMIT 10"
        ).fetchall()
        hourly_rows = conn.execute(
            f"SELECT seen_at FROM sightings "
            f"WHERE seen_at >= ? AND aircraft_type IN ({helos})",
            (midnight_iso,),
        ).fetchall()
    hourly = [0] * 24
    for r in hourly_rows:
        try:
            dt = datetime.fromisoformat(r["seen_at"].replace("Z", "+00:00")).astimezone(_LOCAL_TZ)
            hourly[dt.hour] += 1
        except Exception:
            continue
    return {
        "total": int(total),
        "today": int(today),
        "top_types": [dict(r) for r in types],
        "top_tails": [dict(r) for r in tails],
        "hourly_today": hourly,
    }


def recent_sightings(limit: int = 30) -> list[dict]:
    """Most recent N sightings — for the activity feed card."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT registration, callsign, flight_number, aircraft_type, "
            "origin_iata, destination_iata, altitude_ft, distance_nm, seen_at "
            "FROM sightings ORDER BY seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def top_aircraft_types(limit: int = 15) -> list[dict]:
    """Most-seen ICAO aircraft types across all sightings."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT aircraft_type, COUNT(*) AS n FROM sightings "
            "WHERE aircraft_type IS NOT NULL AND aircraft_type != '' "
            "GROUP BY aircraft_type ORDER BY n DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def hourly_distribution_today() -> list[int]:
    """Sighting count per local-Eastern hour today. Returns 24 ints (0-23)."""
    midnight_iso = _local_midnight_today_utc_iso()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT seen_at FROM sightings WHERE seen_at >= ?",
            (midnight_iso,),
        ).fetchall()
    counts = [0] * 24
    for r in rows:
        s = r["seen_at"]
        try:
            dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(_LOCAL_TZ)
            counts[dt_local.hour] += 1
        except Exception:
            continue
    return counts


def recent_flow_signal(airport_code: str, n: int = 3) -> dict:
    """Determine runway flow from the last N sightings that involved the airport.

    Logic (from Jake's apartment N of DCA):
      - Recent sightings have DCA as origin → planes taking off NORTH past window
        → wind from N → rwy 1 ("NORTH flow")
      - Recent sightings have DCA as destination → planes landing from N past
        window → wind from S → rwy 19 ("SOUTH flow")
      - Mixed → wind just shifted, runway switching

    Returns dict with: flow ('north'|'south'|'mixed'|'unknown'), arr, dep, sample_size.
    """
    code = airport_code.upper()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT origin_iata, destination_iata FROM sightings "
            "WHERE UPPER(origin_iata) = ? OR UPPER(destination_iata) = ? "
            "ORDER BY seen_at DESC LIMIT ?",
            (code, code, n),
        ).fetchall()

    sample_size = len(rows)
    if sample_size < 2:
        return {"flow": "unknown", "arr": 0, "dep": 0, "sample_size": sample_size}

    arr = sum(1 for r in rows if (r["destination_iata"] or "").upper() == code)
    dep = sum(1 for r in rows if (r["origin_iata"] or "").upper() == code)

    if dep == sample_size:
        flow = "north"
    elif arr == sample_size:
        flow = "south"
    else:
        flow = "mixed"
    return {"flow": flow, "arr": arr, "dep": dep, "sample_size": sample_size}


def top_seen(limit: int = 20) -> list[dict]:
    """Top N tails by sighting count, for the leaderboard endpoint."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """
            SELECT registration, COUNT(*) AS n, MAX(seen_at) AS last_seen,
                   MAX(aircraft_type) AS aircraft_type
            FROM sightings
            WHERE registration IS NOT NULL
            GROUP BY registration
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
