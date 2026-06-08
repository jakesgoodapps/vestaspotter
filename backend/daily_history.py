"""Daily aviation diary — snapshots end-of-day stats so they don't get reset.

A background task in main.py fires once per day shortly after local midnight,
captures yesterday's totals + top tails/types + hourly distribution, and stores
them in a small SQLite table. Becomes a permanent log over time.

Schema: one row per date, with a rich JSON blob for stats that may grow.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from . import cost_tracker, sightings_db

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "daily_history.db"
_lock = Lock()
from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)


def _connect() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_history ("
            "  date TEXT PRIMARY KEY,"
            "  total_flights INTEGER,"
            "  arrivals INTEGER,"
            "  departures INTEGER,"
            "  unique_tails INTEGER,"
            "  top_tail TEXT,"
            "  top_tail_count INTEGER,"
            "  top_type TEXT,"
            "  top_type_count INTEGER,"
            "  fa_cost REAL,"
            "  details_json TEXT,"
            "  created_at TEXT NOT NULL"
            ")"
        )


def _snapshot_for_date(date_iso: str, airport_code: str) -> dict:
    """Compute the day's totals by querying sightings between [date 00:00, date+1 00:00) local."""
    code = airport_code.upper()
    day_start_local = datetime.fromisoformat(date_iso).replace(tzinfo=_LOCAL_TZ)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc).isoformat()
    day_end_utc = day_end_local.astimezone(timezone.utc).isoformat()

    with sightings_db._lock, sightings_db._connect() as conn:
        arr = conn.execute(
            "SELECT COUNT(*) AS n FROM sightings WHERE seen_at >= ? AND seen_at < ? "
            "AND UPPER(destination_iata) = ?",
            (day_start_utc, day_end_utc, code),
        ).fetchone()["n"]
        dep = conn.execute(
            "SELECT COUNT(*) AS n FROM sightings WHERE seen_at >= ? AND seen_at < ? "
            "AND UPPER(origin_iata) = ?",
            (day_start_utc, day_end_utc, code),
        ).fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM sightings WHERE seen_at >= ? AND seen_at < ?",
            (day_start_utc, day_end_utc),
        ).fetchone()["n"]
        uniq = conn.execute(
            "SELECT COUNT(DISTINCT registration) AS n FROM sightings "
            "WHERE seen_at >= ? AND seen_at < ? AND registration IS NOT NULL AND registration != ''",
            (day_start_utc, day_end_utc),
        ).fetchone()["n"]
        top_tail_row = conn.execute(
            "SELECT registration, COUNT(*) AS n FROM sightings "
            "WHERE seen_at >= ? AND seen_at < ? AND registration IS NOT NULL AND registration != '' "
            "GROUP BY registration ORDER BY n DESC LIMIT 1",
            (day_start_utc, day_end_utc),
        ).fetchone()
        top_type_row = conn.execute(
            "SELECT aircraft_type, COUNT(*) AS n FROM sightings "
            "WHERE seen_at >= ? AND seen_at < ? AND aircraft_type IS NOT NULL AND aircraft_type != '' "
            "GROUP BY aircraft_type ORDER BY n DESC LIMIT 1",
            (day_start_utc, day_end_utc),
        ).fetchone()
        # Hourly distribution
        all_rows = conn.execute(
            "SELECT seen_at FROM sightings WHERE seen_at >= ? AND seen_at < ?",
            (day_start_utc, day_end_utc),
        ).fetchall()
    hourly = [0] * 24
    for r in all_rows:
        try:
            dt_local = datetime.fromisoformat(r["seen_at"].replace("Z", "+00:00")).astimezone(_LOCAL_TZ)
            hourly[dt_local.hour] += 1
        except Exception:
            continue

    # FA cost for that day
    fa_cost = 0.0
    try:
        with cost_tracker._lock, cost_tracker._connect() as cconn:
            cost_rows = cconn.execute(
                "SELECT endpoint, COUNT(*) AS n FROM fa_calls WHERE called_at >= ? AND called_at < ? GROUP BY endpoint",
                (day_start_utc, day_end_utc),
            ).fetchall()
        for r in cost_rows:
            fa_cost += r["n"] * cost_tracker._cost_for(r["endpoint"])
    except Exception:
        pass

    return {
        "date": date_iso,
        "total_flights": int(total),
        "arrivals": int(arr),
        "departures": int(dep),
        "unique_tails": int(uniq),
        "top_tail": top_tail_row["registration"] if top_tail_row else None,
        "top_tail_count": int(top_tail_row["n"]) if top_tail_row else 0,
        "top_type": top_type_row["aircraft_type"] if top_type_row else None,
        "top_type_count": int(top_type_row["n"]) if top_type_row else 0,
        "fa_cost": round(fa_cost, 4),
        "hourly": hourly,
    }


def snapshot_if_missing(date_iso: str, airport_code: str) -> Optional[dict]:
    """Snapshot a given day if we haven't already. Returns the snapshot dict
    if newly written, or None if already present."""
    with _lock, _connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM daily_history WHERE date = ?", (date_iso,)
        ).fetchone()
        if existing:
            return None
    snap = _snapshot_for_date(date_iso, airport_code)
    details_json = json.dumps({"hourly": snap["hourly"]})
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_history "
            "(date, total_flights, arrivals, departures, unique_tails, top_tail, "
            "top_tail_count, top_type, top_type_count, fa_cost, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap["date"], snap["total_flights"], snap["arrivals"], snap["departures"],
                snap["unique_tails"], snap["top_tail"], snap["top_tail_count"],
                snap["top_type"], snap["top_type_count"], snap["fa_cost"],
                details_json, datetime.now(timezone.utc).isoformat(),
            ),
        )
    return snap


def list_recent(limit: int = 14) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_history ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
