"""Reverse-geocode a lat/lon → short "CITY ST" string for the board.

Uses OpenStreetMap's Nominatim API (free). Nominatim's politeness policy:
  - 1 request per second max
  - Real User-Agent identifying the app
  - Cache aggressively

We round lat/lon to 0.1° (≈ 11km) for cache keys, so multiple position
updates near the same grid cell share a cached lookup. Cache persists
across container restarts in data/city_lookup_cache.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Optional

import aiohttp

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_CACHE_PATH = _DATA_DIR / "city_lookup_cache.json"
_lock = Lock()
_cache: dict[str, str] = {}
_cache_loaded = False
_last_request_at = 0.0

# Politeness — Nominatim asks for ≤1 req/sec
_MIN_INTERVAL_S = 1.1

# Identifier required by Nominatim's ToS
_USER_AGENT = "VestaSpotter/0.1 (+https://github.com/jakesgoodapps/vestaspotter)"

# US state code lookup so we can render "WICHITA KS" instead of "WICHITA KANSAS"
_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}


def _cache_key(lat: float, lon: float) -> str:
    """Round to 0.1° grid (~11km) for cache reuse on nearby positions."""
    return f"{round(lat, 1):.1f},{round(lon, 1):.1f}"


def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    if _CACHE_PATH.exists():
        try:
            _cache = json.loads(_CACHE_PATH.read_text())
        except Exception:
            _cache = {}
    _cache_loaded = True


def _save_cache() -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cache, indent=2, sort_keys=True))
    tmp.replace(_CACHE_PATH)


def _format_display(addr: dict, country_code: Optional[str]) -> Optional[str]:
    """Build the board-display string from a Nominatim address dict.

    Priorities for the place name (first non-empty wins):
      city > town > village > hamlet > county > state > country
    Trailing context:
      US → 2-letter state ("WICHITA KS")
      non-US → ISO country code, uppercased ("LONDON UK")
    """
    place = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or addr.get("county")
        or addr.get("state")
        or addr.get("country")
    )
    if not place:
        return None

    place = place.upper()

    if country_code == "us":
        state = addr.get("state")
        suffix = _STATE_ABBR.get(state, "")
        return f"{place} {suffix}".strip()

    # International — use ISO country code if available
    if country_code:
        return f"{place} {country_code.upper()}"
    return place


async def lookup_city(lat: float, lon: float) -> Optional[str]:
    """Reverse-geocode lat/lon → "CITY STATE" or "CITY CC". Cached.

    Returns None for oceanic positions where Nominatim has no result.
    Caller can render that as "OVER THE OCEAN" or similar.
    """
    global _last_request_at

    _load_cache()
    key = _cache_key(lat, lon)
    if key in _cache:
        return _cache[key] or None

    # Politeness delay
    elapsed = time.time() - _last_request_at
    if elapsed < _MIN_INTERVAL_S:
        await asyncio.sleep(_MIN_INTERVAL_S - elapsed)

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": f"{lat:.4f}",
        "lon": f"{lon:.4f}",
        "format": "json",
        "zoom": "10",  # city level
        "addressdetails": "1",
    }
    headers = {"User-Agent": _USER_AGENT}

    display: Optional[str] = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                _last_request_at = time.time()
                if resp.status != 200:
                    print(f"nominatim {resp.status} for {lat},{lon}")
                else:
                    data = await resp.json()
                    addr = data.get("address") or {}
                    display = _format_display(addr, addr.get("country_code"))
    except Exception as e:
        print(f"nominatim lookup failed for {lat},{lon}: {e}")

    # Cache even None results to avoid repeated lookups for the same oceanic cell
    with _lock:
        _cache[key] = display or ""
        _save_cache()

    return display
