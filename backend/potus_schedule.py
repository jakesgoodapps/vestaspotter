"""factba.se POTUS public schedule integration.

Fetches the daily JSON feed (~2MB, free, no auth required) once every few hours
and caches it locally. Provides a single lookup function used by the POTUS
detector: "given that something is happening at the White House right now, is
there a scheduled departure or arrival in the next ~90 minutes? if so, what
and where?"

Source: https://media-cdn.factba.se/rss/json/trump/calendar-full.json
This is the same data shown on the rollcall.com factba.se calendar page.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_PATH = _DATA_DIR / "potus_schedule_cache.json"
_META_PATH = _DATA_DIR / "potus_schedule_meta.json"
_lock = Lock()

FEED_URL = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"
CACHE_TTL_SECONDS = 6 * 3600  # re-fetch every 6 hours
LOOKUP_WINDOW_MINUTES = 90    # how far +/- to search around "now"
from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)

# Friendly truncations for common destinations (must fit row 5's 22-char board cell,
# minus the "DEP TO " or "VIA " prefix — practical budget ~15 chars).
DESTINATION_SHORTNAMES = {
    "joint base andrews": "ANDREWS",
    "the capitol": "CAPITOL",
    "the white house": "WH",
    "department of justice": "DOJ",
    "department of state": "STATE DEPT",
    "department of defense": "DOD / PENTAGON",
    "the pentagon": "PENTAGON",
    "camp david": "CAMP DAVID",
    "trump national golf club bedminster": "BEDMINSTER",
    "mar-a-lago": "MAR-A-LAGO",
    "trump international golf club": "TRUMP GOLF",
    "trump tower": "TRUMP TOWER",
    "ronald reagan washington national airport": "DCA",
    "dulles international airport": "IAD",
}

# Full state name → 2-letter abbreviation. Used to compress "City, StateName" →
# "CITY, ST" so factba.se's verbose location strings fit the board.
# (Mirrors city_lookup._STATE_ABBR but kept inline to avoid module coupling.)
_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def _try_city_state_compress(raw_clean: str) -> Optional[str]:
    """Compress a 'City, StateName' string into 'CITY, ST' (or just 'CITY' if
    the full form exceeds 15 chars — the practical budget after 'DEP TO ').

    Examples:
      "Reading, Pennsylvania"      → "READING, PA"        (11 chars, fits)
      "Pittsburgh, Pennsylvania"   → "PITTSBURGH, PA"     (14 chars, fits)
      "Philadelphia, Pennsylvania" → "PHILADELPHIA"       (state dropped, too long)
      "West Palm Beach, Florida"   → "WEST PALM BEACH"    (state dropped)
      "Reading"                    → None                 (no state portion)

    Returns None if the input doesn't match the City, StateName pattern.
    """
    if "," not in raw_clean:
        return None
    city_part, _, state_part = raw_clean.rpartition(",")
    state_key = state_part.strip().lower()
    if state_key not in _STATE_ABBR:
        return None
    city = city_part.strip().upper()
    full = f"{city}, {_STATE_ABBR[state_key]}"
    # Practical budget after "DEP TO " prefix = 22 - 7 = 15
    if len(full) <= 15:
        return full
    return city[:15]


def _short_destination(raw: Optional[str]) -> str:
    if not raw:
        return ""
    raw_clean = raw.strip()
    key = raw_clean.lower()
    if key in DESTINATION_SHORTNAMES:
        return DESTINATION_SHORTNAMES[key]
    # Try prefix match (e.g., "joint base andrews, md" → ANDREWS)
    for k, v in DESTINATION_SHORTNAMES.items():
        if key.startswith(k):
            return v
    # Try generic "City, StateName" → "CITY, ST" compression
    compressed = _try_city_state_compress(raw_clean)
    if compressed:
        return compressed
    return raw_clean.upper()[:18]


def _extract_destination(details: str) -> Optional[str]:
    if not details:
        return None
    # factba.se inconsistently writes "en route X" vs "en route to X" — accept both
    m = re.search(r"en route(?:\s+to)?\s+(.+?)(?:\.|$)", details, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).strip().rstrip(".,")
    return _short_destination(raw)


def _parse_entry_time(date_str: str, time_str: str) -> Optional[datetime]:
    """Combine date + HH:MM:SS time into an aware ET datetime."""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=_LOCAL_TZ)
    except Exception:
        return None


def _read_meta() -> dict:
    if not _META_PATH.exists():
        return {}
    try:
        with _META_PATH.open() as f:
            return json.load(f)
    except Exception:
        return {}


def _write_meta(meta: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _META_PATH.open("w") as f:
        json.dump(meta, f)


def _cache_fresh() -> bool:
    meta = _read_meta()
    fetched = meta.get("fetched_at_unix", 0)
    return _CACHE_PATH.exists() and (time.time() - fetched) < CACHE_TTL_SECONDS


def _load_cached() -> Optional[list]:
    if not _CACHE_PATH.exists():
        return None
    try:
        with _CACHE_PATH.open() as f:
            return json.load(f)
    except Exception:
        return None


async def fetch_and_cache() -> Optional[list]:
    """Download factba.se feed and cache to disk. Returns the data or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(FEED_URL)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("factba.se fetch failed: %s", e)
        return None
    if not isinstance(data, list):
        return None
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        with _CACHE_PATH.open("w") as f:
            json.dump(data, f)
        _write_meta({"fetched_at_unix": time.time(), "entry_count": len(data)})
    logger.info("factba.se cached: %d entries", len(data))
    return data


async def ensure_fresh() -> None:
    """Re-fetch the feed if our cache is older than TTL. Safe to call frequently;
    it'll no-op when the cache is still warm."""
    if _cache_fresh():
        return
    await fetch_and_cache()


_TRANSIT_HUBS = ("joint base andrews",)  # hubs to look past for the real final dest


def _find_final_destination(cal: list, wh_entry_sched: datetime, immediate_dest: Optional[str]) -> Optional[str]:
    """Given the WH→Andrews-style departure, scan forward for the next leg.
    Returns the short-named final destination (e.g., 'MAR-A-LAGO') or None.

    The factba.se entries chain like:
      14:30  departs The White House en route to Joint Base Andrews
      15:05  departs Joint Base Andrews en route to Mar-a-Lago
    """
    if not immediate_dest:
        return None
    # Only chain through known transit hubs (otherwise the immediate dest IS final)
    if not any(h in immediate_dest.lower() or h.replace(" ", "") in immediate_dest.lower().replace(" ", "")
               for h in [s.split(",")[0] for s in _TRANSIT_HUBS]):
        # Check the short-name too
        short_hubs = {DESTINATION_SHORTNAMES[h] for h in _TRANSIT_HUBS if h in DESTINATION_SHORTNAMES}
        if immediate_dest.upper() not in short_hubs:
            return None
    date_str = wh_entry_sched.strftime("%Y-%m-%d")
    for entry in cal:
        if entry.get("date") != date_str:
            continue
        det = (entry.get("details") or "").lower()
        if not any(f"departs {hub}" in det for hub in _TRANSIT_HUBS):
            continue
        sched = _parse_entry_time(entry["date"], entry.get("time", "00:00:00"))
        if not sched:
            continue
        diff_hours = (sched - wh_entry_sched).total_seconds() / 3600
        if 0 < diff_hours < 6:  # within 6 hours after WH departure
            return _extract_destination(entry.get("details", ""))
    return None


def lookup_nearby_movement(now_dt: Optional[datetime] = None) -> Optional[dict]:
    """Find the closest scheduled WH departure or arrival within ±LOOKUP_WINDOW_MINUTES
    of `now_dt` (default = now). Returns dict {kind, destination, final_destination,
    scheduled_at_iso, minutes_until, details} or None if no match."""
    if now_dt is None:
        now_dt = datetime.now(_LOCAL_TZ)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc).astimezone(_LOCAL_TZ)
    else:
        now_dt = now_dt.astimezone(_LOCAL_TZ)

    cal = _load_cached()
    if not cal:
        return None

    today = now_dt.strftime("%Y-%m-%d")
    tomorrow = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    candidates = []
    tbd_today = []   # WH-movement entries for TODAY whose time isn't published yet
    for entry in cal:
        date_val = entry.get("date")
        if date_val not in (today, tomorrow):
            continue
        det = (entry.get("details") or "").lower()
        if "departs the white house" in det:
            kind = "departure"
            dest = _extract_destination(entry.get("details", ""))
        elif "arrives at the white house" in det:
            kind = "arrival"
            dest = None
        else:
            continue
        sched = _parse_entry_time(entry["date"], entry.get("time", "00:00:00"))
        if not sched:
            # TBD movement entry — confirms trip is scheduled today even though
            # the exact time isn't announced yet. Kept as a fallback for when
            # no time-windowed match is found (typical for TBD-only days).
            if date_val == today:
                tbd_today.append({
                    "kind": kind,
                    "destination": dest,
                    "details": entry.get("details", ""),
                })
            continue
        candidates.append({
            "kind": kind,
            "destination": dest,
            "scheduled_at": sched,
            "details": entry.get("details", ""),
        })

    best = None
    best_diff = LOOKUP_WINDOW_MINUTES * 60
    for c in candidates:
        diff = abs((c["scheduled_at"] - now_dt).total_seconds())
        if diff < best_diff:
            best = c
            best_diff = diff

    if best:
        minutes_until = int((best["scheduled_at"] - now_dt).total_seconds() // 60)
        final_dest = None
        if best["kind"] == "departure":
            final_dest = _find_final_destination(cal, best["scheduled_at"], best["destination"])
        return {
            "kind": best["kind"],
            "destination": best["destination"],
            "final_destination": final_dest,
            "scheduled_at_iso": best["scheduled_at"].isoformat(),
            "minutes_until": minutes_until,
            "details": best["details"],
            "is_tbd": False,
        }

    # Fall back to TBD: a movement IS scheduled today, time just isn't published.
    # Prefer departure over arrival (helo typically circles before departures).
    if tbd_today:
        tbd_pick = next((c for c in tbd_today if c["kind"] == "departure"), tbd_today[0])
        return {
            "kind": tbd_pick["kind"],
            "destination": tbd_pick["destination"],
            "final_destination": None,
            "scheduled_at_iso": None,
            "minutes_until": None,
            "details": tbd_pick["details"],
            "is_tbd": True,
        }

    return None


def cache_age_seconds() -> Optional[int]:
    meta = _read_meta()
    fetched = meta.get("fetched_at_unix")
    if not fetched:
        return None
    return int(time.time() - fetched)
