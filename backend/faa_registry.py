"""ICAO24 hex → aircraft identity (tail, type, year, owner). Permanent local store.

Source: OpenSky's aircraft metadata endpoint (free; we already have OAuth
creds for them and they have decent US coverage). One record per icao24,
cached forever in registry.db — an aircraft's hex never changes and its
identity almost never does.

This is Tier 1 of the enrichment stack (see enrichment.py): it answers
"what IS this plane" without FlightAware, which used to be the source of
registration + type (via /flights/{ident}) and owner (via /aircraft/{reg}/owner).

Module name kept as `faa_registry` for history; `year_built()` is preserved
as a thin wrapper over `identity()`.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "registry.db"
_lock = Lock()

OPENSKY_METADATA_URL = "https://opensky-network.org/api/metadata/aircraft/icao/{icao24}"
_RETRY_EMPTY_AFTER = timedelta(days=30)   # re-ask OpenSky about unknown hexes monthly

_COLUMNS = ("registration", "typecode", "model", "manufacturer", "owner", "operator", "year_built")


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the table, migrate older installs (which only had registration +
    year_built), and backfill the new identity columns from the raw OpenSky
    JSON we were already storing — zero network calls."""
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
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(registry)").fetchall()}
        for col in ("typecode", "model", "manufacturer", "owner", "operator"):
            if col not in existing:
                conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
        rows = conn.execute(
            "SELECT icao24, raw_json FROM registry WHERE typecode IS NULL AND raw_json IS NOT NULL AND raw_json != '{}'"
        ).fetchall()
        backfilled = 0
        for r in rows:
            try:
                data = json.loads(r["raw_json"]) or {}
            except Exception:
                continue
            fields = _fields_from_metadata(data)
            if not any(fields.values()):
                continue
            conn.execute(
                "UPDATE registry SET typecode=?, model=?, manufacturer=?, owner=?, operator=? WHERE icao24=?",
                (fields["typecode"], fields["model"], fields["manufacturer"], fields["owner"], fields["operator"], r["icao24"]),
            )
            backfilled += 1
        if backfilled:
            logger.info("registry: backfilled identity columns for %d aircraft from cached OpenSky metadata", backfilled)


def _fields_from_metadata(data: dict) -> dict:
    def clean(v):
        v = (v or "").strip() if isinstance(v, str) else v
        return v or None
    return {
        "typecode": clean(data.get("typecode")),
        "model": clean(data.get("model")),
        "manufacturer": clean(data.get("manufacturerName")),
        "owner": clean(data.get("owner")),
        "operator": clean(data.get("operator")),
    }


def _row_to_identity(row) -> dict:
    return {k: row[k] for k in _COLUMNS} | {"icao24": row["icao24"], "checked_at": row["checked_at"]}


def lookup(icao24: Optional[str]) -> Optional[dict]:
    """SQLite-only identity lookup (no network). None if never fetched."""
    if not icao24:
        return None
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM registry WHERE icao24 = ?", (icao24.lower().strip(),)).fetchone()
    return _row_to_identity(row) if row else None


def count() -> int:
    with _lock, _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM registry WHERE registration IS NOT NULL").fetchone()["n"])


def _store(icao24: str, registration: Optional[str], year: Optional[int], raw: dict) -> None:
    f = _fields_from_metadata(raw)
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO registry (icao24, registration, year_built, raw_json, checked_at, "
            "typecode, model, manufacturer, owner, operator) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (icao24, registration, year, json.dumps(raw), datetime.now(timezone.utc).isoformat(),
             f["typecode"], f["model"], f["manufacturer"], f["owner"], f["operator"]),
        )


def _parse_built_year(built_raw) -> Optional[int]:
    """OpenSky's 'built' field is sometimes 'YYYY-MM-DD', sometimes just 'YYYY',
    sometimes empty/null. Extract a year int or return None."""
    if not built_raw:
        return None
    s = str(built_raw).strip()
    if not s:
        return None
    try:
        y = int(s[:4])
        if 1900 <= y <= 2100:
            return y
    except ValueError:
        pass
    return None


def _stale_unknown(ident: dict) -> bool:
    """A row with no registration is an OpenSky miss — worth re-asking monthly
    (their DB grows), but never hammer it."""
    if ident.get("registration"):
        return False
    try:
        checked = datetime.fromisoformat(ident["checked_at"])
    except Exception:
        return True
    return datetime.now(timezone.utc) - checked > _RETRY_EMPTY_AFTER


async def identity(icao24: Optional[str], registration: Optional[str] = None) -> Optional[dict]:
    """Return {registration, typecode, model, manufacturer, owner, operator,
    year_built} for an aircraft, or None if unknown.

    Cache-aside: SQLite hit → return; miss → fetch OpenSky metadata, store
    forever, return. `registration` (if the caller already knows it) is
    stored alongside for readability when OpenSky has nothing.
    """
    if not icao24:
        return None
    key = icao24.lower().strip()
    cached = lookup(key)
    if cached and not _stale_unknown(cached):
        return cached
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OPENSKY_METADATA_URL.format(icao24=key),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    _store(key, registration, None, {})
                    return lookup(key)
                data = await resp.json()
    except Exception as e:
        logger.warning("OpenSky metadata lookup for %s failed: %s", key, e)
        return cached
    year_int = _parse_built_year(data.get("built"))
    reg = (data.get("registration") or "").strip() or registration
    _store(key, reg, year_int, data)
    return lookup(key)


def learn(icao24: Optional[str], registration: Optional[str] = None, typecode: Optional[str] = None) -> None:
    """Fill NULL identity fields from another source (FlightAware's flight
    record). Never overwrites what OpenSky said; creates the row if OpenSky
    had nothing. This is how the ~half of hexes OpenSky lacks a type code
    for converge to fully-known after a single FA call."""
    if not icao24 or not (registration or typecode):
        return
    key = icao24.lower().strip()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT registration, typecode FROM registry WHERE icao24 = ?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO registry (icao24, registration, typecode, raw_json, checked_at) VALUES (?, ?, ?, '{}', ?)",
                (key, registration, typecode, datetime.now(timezone.utc).isoformat()),
            )
            return
        if row["registration"] is None and registration:
            conn.execute("UPDATE registry SET registration = ? WHERE icao24 = ?", (registration, key))
        if row["typecode"] is None and typecode:
            conn.execute("UPDATE registry SET typecode = ? WHERE icao24 = ?", (typecode, key))


async def year_built(icao24: Optional[str], registration: Optional[str] = None) -> Optional[int]:
    """Back-compat wrapper: year manufactured, or None."""
    ident = await identity(icao24, registration)
    return ident.get("year_built") if ident else None
