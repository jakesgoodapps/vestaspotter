"""Local, permanent caches that stand in front of FlightAware.

Tier 2 of the enrichment stack (Tier 1 = faa_registry identity, Tier 3 =
FlightAware live). Everything here is SQLite in data/local_cache.db and
survives restarts — unlike the old in-memory caches, which re-bought the
whole fleet from FA after every deploy.

  routes    callsign → origin / destination / marketing flight number /
            codeshares. Airlines keep a flight number on the same city pair
            for a whole schedule season, and ~87% of sightings here are
            flight numbers seen 5+ times a month, so a 7-day TTL answers
            most "where is it going" questions for free. Seeded from the
            sightings table on first run; refreshed from FA on miss/expiry.
            Live per-day data (times, delays, gates, status) is deliberately
            NOT stored — that's what the live FA call is for.

  fa_types  ICAO type → FA /aircraft/types response (static, cached forever).

Counters (in-memory since process start) feed the dashboard's
"FA calls avoided" line so the savings are visible.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

from . import sightings_db

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "local_cache.db"
_lock = Lock()

_counters: Counter = Counter()
_since = datetime.now(timezone.utc)

# Per-day / live fields from an FA flight object that must never be served
# from cache. Everything else (origin, destination, ident_iata, operator,
# codeshares, aircraft_type, registration, filed route…) is stable enough.
LIVE_FIELDS = {
    "scheduled_out", "scheduled_off", "scheduled_on", "scheduled_in",
    "estimated_out", "estimated_off", "estimated_on", "estimated_in",
    "actual_out", "actual_off", "actual_on", "actual_in",
    "departure_delay", "arrival_delay", "status", "progress_percent",
    "gate_origin", "gate_destination", "terminal_origin", "terminal_destination",
    "baggage_claim", "diverted", "cancelled", "fa_flight_id", "position_only",
    "last_position", "blocked", "foresight_predictions_available", "atc_ident",
    "inbound_fa_flight_id", "actual_runway_off", "actual_runway_on",
}


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
                callsign TEXT PRIMARY KEY,
                flight_number TEXT,
                origin_iata TEXT,
                dest_iata TEXT,
                fa_json TEXT NOT NULL,
                source TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fa_types (
                icao_type TEXT PRIMARY KEY,
                fa_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )


def count(key: str, n: int = 1) -> None:
    _counters[key] += n


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _static_part(fa_flight: dict) -> dict:
    return {k: v for k, v in fa_flight.items() if k not in LIVE_FIELDS}


def get_route(callsign: str, max_age_s: int) -> Optional[dict]:
    """Cached static FA-shaped flight dict for this callsign, or None if
    missing / older than max_age_s. Bumps the hit counter on success."""
    key = callsign.upper().strip()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT fa_json, refreshed_at FROM routes WHERE callsign = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["refreshed_at"])
        except Exception:
            return None
        if age.total_seconds() > max_age_s:
            return None
        conn.execute("UPDATE routes SET hits = hits + 1 WHERE callsign = ?", (key,))
    try:
        data = json.loads(row["fa_json"])
    except Exception:
        return None
    if not ((data.get("origin") or {}).get("code_iata") and (data.get("destination") or {}).get("code_iata")):
        return None
    return data


def put_route(callsign: str, fa_flight: dict, source: str = "fa") -> None:
    """Store the static part of an FA flight object (overwrites — a live FA
    answer is always the freshest truth about the route)."""
    key = callsign.upper().strip()
    static = _static_part(fa_flight)
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO routes (callsign, flight_number, origin_iata, dest_iata, fa_json, source, refreshed_at, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT hits FROM routes WHERE callsign = ?), 0)) "
            "ON CONFLICT(callsign) DO UPDATE SET flight_number=excluded.flight_number, origin_iata=excluded.origin_iata, "
            "dest_iata=excluded.dest_iata, fa_json=excluded.fa_json, source=excluded.source, refreshed_at=excluded.refreshed_at",
            (
                key,
                static.get("ident_iata"),
                (static.get("origin") or {}).get("code_iata"),
                (static.get("destination") or {}).get("code_iata"),
                json.dumps(static),
                source,
                datetime.now(timezone.utc).isoformat(),
                key,
            ),
        )


def seed_routes_from_sightings(days: int = 30, min_count: int = 3, min_share: float = 0.8) -> int:
    """Warm the route cache from our own history: any callsign seen ≥min_count
    times in the last N days whose dominant (origin, dest) pair accounts for
    ≥min_share of its sightings. Only inserts callsigns not already cached.
    Seeded rows are stamped with their last sighting time so they age out and
    get verified by FA within the normal TTL. Returns rows inserted."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sightings_db._lock, sightings_db._connect() as sconn:
        rows = sconn.execute(
            "SELECT callsign, flight_number, origin_iata, destination_iata, COUNT(*) AS n, MAX(seen_at) AS last_seen "
            "FROM sightings WHERE seen_at >= ? AND callsign IS NOT NULL AND callsign != '' "
            "AND origin_iata IS NOT NULL AND destination_iata IS NOT NULL "
            "GROUP BY callsign, origin_iata, destination_iata",
            (since,),
        ).fetchall()
    by_cs: dict[str, list] = {}
    for r in rows:
        by_cs.setdefault(r["callsign"].upper().strip(), []).append(r)
    inserted = 0
    with _lock, _connect() as conn:
        for cs, pairs in by_cs.items():
            total = sum(p["n"] for p in pairs)
            best = max(pairs, key=lambda p: p["n"])
            if best["n"] < min_count or best["n"] / total < min_share:
                continue
            if conn.execute("SELECT 1 FROM routes WHERE callsign = ?", (cs,)).fetchone():
                continue
            fa_like = {
                "ident": cs,
                "ident_iata": best["flight_number"],
                "origin": {"code_iata": best["origin_iata"]},
                "destination": {"code_iata": best["destination_iata"]},
            }
            conn.execute(
                "INSERT INTO routes (callsign, flight_number, origin_iata, dest_iata, fa_json, source, refreshed_at, hits) "
                "VALUES (?, ?, ?, ?, ?, 'sightings', ?, 0)",
                (cs, best["flight_number"], best["origin_iata"], best["destination_iata"], json.dumps(fa_like), best["last_seen"]),
            )
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# FA aircraft types (static)
# ---------------------------------------------------------------------------

def get_type(icao_type: str) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT fa_json FROM fa_types WHERE icao_type = ?", (icao_type.upper(),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["fa_json"])
    except Exception:
        return None


def put_type(icao_type: str, data: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fa_types (icao_type, fa_json, fetched_at) VALUES (?, ?, ?)",
            (icao_type.upper(), json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    from . import faa_registry
    with _lock, _connect() as conn:
        routes = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS hits FROM routes").fetchone()
        types = conn.execute("SELECT COUNT(*) AS n FROM fa_types").fetchone()["n"]
    c = dict(_counters)
    avoided = c.get("route_hit", 0) + c.get("identity_hit", 0) + c.get("owner_skipped", 0) + c.get("type_hit", 0)
    return {
        "since": _since.isoformat(),
        "routes_cached": int(routes["n"]),
        "route_hits_all_time": int(routes["hits"]),
        "identities_cached": faa_registry.count(),
        "types_cached": int(types),
        "counters": c,
        "fa_calls_avoided": avoided,
    }
