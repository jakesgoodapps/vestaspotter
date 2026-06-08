"""ICAO24 hex → manufacture year lookup.

Uses OpenSky's aircraft metadata endpoint (we already have OAuth creds for them
and they have decent US coverage; hexdb.io was 0% on US commercial tails).
The `built` field is populated for maybe 50-70% of records — we cache the
result (year or None) forever so we don't re-fetch.

Module name kept as `faa_registry` since the public function `year_built()`
is what callers care about; source can swap behind it.
"""
import json
import logging
import os
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "registry.db"
_lock = Lock()

OPENSKY_METADATA_URL = "https://opensky-network.org/api/metadata/aircraft/icao/{icao24}"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registry (
                icao24 TEXT PRIMARY KEY,
                registration TEXT,
                year_built INTEGER,
                raw_json TEXT,
                checked_at TEXT NOT NULL
            )
            """
        )


def _get_cached(icao24: str) -> tuple[bool, Optional[int]]:
    """Returns (was_cached, year_or_none). Differentiates "never looked up"
    from "looked up and got null"."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT year_built FROM registry WHERE icao24 = ?", (icao24,)
        ).fetchone()
    if row is None:
        return (False, None)
    return (True, row["year_built"])


def _store(icao24: str, registration: Optional[str], year: Optional[int], raw: dict) -> None:
    from datetime import datetime, timezone
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO registry (icao24, registration, year_built, raw_json, checked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (icao24, registration, year, json.dumps(raw), datetime.now(timezone.utc).isoformat()),
        )


def _parse_built_year(built_raw) -> Optional[int]:
    """OpenSky's 'built' field is sometimes 'YYYY-MM-DD', sometimes just 'YYYY',
    sometimes empty/null. Extract a year int or return None."""
    if not built_raw:
        return None
    s = str(built_raw).strip()
    if not s:
        return None
    # Take first 4 chars if they look like a year
    candidate = s[:4]
    try:
        y = int(candidate)
        if 1900 <= y <= 2100:
            return y
    except ValueError:
        pass
    return None


async def year_built(icao24: Optional[str], registration: Optional[str] = None) -> Optional[int]:
    """Return year manufactured for an aircraft, or None if unknown.

    Looks up by ICAO 24-bit hex (always present from OpenSky state vectors).
    Registration is stored alongside for human-readable DB browsing only.
    Cache-aside: SQLite hit → return; miss → fetch OpenSky metadata, store, return.
    """
    if not icao24:
        return None
    key = icao24.lower().strip()
    cached, value = _get_cached(key)
    if cached:
        return value
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OPENSKY_METADATA_URL.format(icao24=key),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    _store(key, registration, None, {})
                    return None
                data = await resp.json()
    except Exception as e:
        logger.warning("OpenSky metadata lookup for %s failed: %s", key, e)
        return None
    year_int = _parse_built_year(data.get("built"))
    _store(key, registration, year_int, data)
    return year_int
