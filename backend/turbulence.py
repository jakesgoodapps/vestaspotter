"""Turbulence severity lookup via AWC SIGMET feed.

Used by the followed-flight tracker to color the row-5 progress-bar
frontier tile + render the row-4 inline weather indicator.

Free public API at aviationweather.gov — no key required, no rate limit
documented, but we cache the SIGMET feed at the module level for 5 minutes
since the bulletins themselves only update at roughly that cadence.

Mapping to followed_flight.WeatherSeverity:
  - No SIGMET match              → SMOOTH       (blue tile)
  - TURB sev 1-2                 → LIGHT_TURB   (yellow tile)
  - TURB sev 3-4                 → MODERATE     (orange tile)
  - TURB sev 5+ OR CONVECTIVE    → SEVERE       (red tile)

CONVECTIVE SIGMETs (thunderstorms) are always treated as SEVERE — thunderstorms
trigger heavy turbulence + are pilot-mandatory-avoid, so flagging them visually
is the right call even though the SIGMET hazard label isn't strictly "TURB".
"""
from __future__ import annotations

import time
from typing import Optional

import aiohttp

from .followed_flight import WeatherSeverity

_SIGMET_URL = "https://aviationweather.gov/api/data/airsigmet?format=json&type=sigmet"
_CACHE_TTL_S = 300  # SIGMETs update ~hourly; 5-min cache is plenty
_sigmets_cache: list[dict] = []
_cache_at: float = 0


async def _refresh_sigmets() -> None:
    """Fetch fresh SIGMET list from AWC. Updates module-level cache."""
    global _sigmets_cache, _cache_at
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_SIGMET_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    print(f"AWC SIGMET feed {resp.status}")
                    return
                _sigmets_cache = await resp.json()
                _cache_at = time.time()
    except Exception as e:
        print(f"AWC SIGMET fetch failed: {e}")


def _point_in_polygon(lat: float, lon: float, coords: list[dict]) -> bool:
    """Ray casting algorithm. coords = [{'lat': ..., 'lon': ...}, ...].

    Does NOT handle anti-meridian (±180°) wrap — rare edge case for our use.
    Polygons in SIGMETs are typically tight regional shapes that don't cross.
    """
    if not coords or len(coords) < 3:
        return False
    n = len(coords)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = coords[i]["lon"], coords[i]["lat"]
        xj, yj = coords[j]["lon"], coords[j]["lat"]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _altitude_overlaps(alt_100s_ft: Optional[int], sigmet: dict) -> bool:
    """True if the aircraft's altitude is inside the SIGMET's altitude band.

    Unit note: FA's /position returns altitude in HUNDREDS of feet (350 = FL350
    = 35,000ft). AWC SIGMET altitudeLow1/altitudeHi1 are in FEET. We convert.

    If we have no altitude data, return True (better to flag than miss). If the
    SIGMET has no upper bound, we use 60,000ft as the absurd ceiling (well above
    civil airline cruise).
    """
    if alt_100s_ft is None:
        return True
    alt_ft = alt_100s_ft * 100
    low_ft = sigmet.get("altitudeLow1") or 0
    high_ft = sigmet.get("altitudeHi1") or 60000
    return low_ft <= alt_ft <= high_ft


def _severity_from_sigmet(sigmet: dict) -> WeatherSeverity:
    """Map a SIGMET hazard + numeric severity → our WeatherSeverity enum."""
    hazard = (sigmet.get("hazard") or "").upper()
    sev_num = sigmet.get("severity")
    try:
        sev_num = int(sev_num) if sev_num is not None else 0
    except (ValueError, TypeError):
        sev_num = 0

    if hazard == "CONVECTIVE":
        return WeatherSeverity.SEVERE

    if hazard == "TURB":
        if sev_num >= 5:
            return WeatherSeverity.SEVERE
        if sev_num >= 3:
            return WeatherSeverity.MODERATE
        return WeatherSeverity.LIGHT_TURB

    # Other hazards (ICE, IFR, MTW) — not directly turbulence, ignore for now
    return WeatherSeverity.SMOOTH


_SEVERITY_RANK = {
    WeatherSeverity.SMOOTH:     0,
    WeatherSeverity.LIGHT_TURB: 1,
    WeatherSeverity.MODERATE:   2,
    WeatherSeverity.SEVERE:     3,
}


async def get_turbulence_at(
    lat: float, lon: float, alt_100s_ft: Optional[int] = None
) -> WeatherSeverity:
    """Worst severity for any active SIGMET containing the position + altitude.

    Returns SMOOTH if no matching SIGMETs (the common case).
    """
    if time.time() - _cache_at > _CACHE_TTL_S:
        await _refresh_sigmets()

    worst = WeatherSeverity.SMOOTH
    for sigmet in _sigmets_cache:
        if not _altitude_overlaps(alt_100s_ft, sigmet):
            continue
        coords = sigmet.get("coords") or []
        if not _point_in_polygon(lat, lon, coords):
            continue
        sev = _severity_from_sigmet(sigmet)
        if _SEVERITY_RANK[sev] > _SEVERITY_RANK[worst]:
            worst = sev
    return worst
