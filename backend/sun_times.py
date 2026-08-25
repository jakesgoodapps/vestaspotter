"""Sunrise / sunset / golden hour for the observer location.

Pure-Python NOAA solar calculator — no dependencies, ~1 min accuracy, which
is plenty for a "golden hour starts at 6:52P" card. Used by idle_cards.py.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def _julian_day(d: date) -> float:
    # Julian day number at 0h UT for a civil date.
    y, m, day = d.year, d.month, d.day
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def _sun_event_utc_minutes(d: date, lat: float, lon: float, rising: bool) -> Optional[float]:
    """Minutes after 0h UTC of sunrise (rising=True) or sunset on date d.
    Returns None for polar day/night."""
    jd = _julian_day(d)
    # Iterate twice: the equation-of-time / declination depend on the time of
    # the event itself, so refine once using the first estimate.
    t_minutes = 720.0
    for _ in range(2):
        jc = (jd + t_minutes / 1440.0 - 2451545.0) / 36525.0
        geom_mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
        geom_mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
        ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
        m_rad = math.radians(geom_mean_anom)
        eq_ctr = (
            math.sin(m_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
            + math.sin(2 * m_rad) * (0.019993 - 0.000101 * jc)
            + math.sin(3 * m_rad) * 0.000289
        )
        true_long = geom_mean_long + eq_ctr
        app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))
        mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
        obliq = mean_obliq + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc))
        declination = math.degrees(
            math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
        )
        y = math.tan(math.radians(obliq / 2)) ** 2
        eq_time = 4 * math.degrees(
            y * math.sin(2 * math.radians(geom_mean_long))
            - 2 * ecc * math.sin(m_rad)
            + 4 * ecc * y * math.sin(m_rad) * math.cos(2 * math.radians(geom_mean_long))
            - 0.5 * y * y * math.sin(4 * math.radians(geom_mean_long))
            - 1.25 * ecc * ecc * math.sin(2 * m_rad)
        )
        # Hour angle for the standard -0.833° sun altitude (refraction + disc).
        cos_ha = (
            math.cos(math.radians(90.833))
            / (math.cos(math.radians(lat)) * math.cos(math.radians(declination)))
            - math.tan(math.radians(lat)) * math.tan(math.radians(declination))
        )
        if cos_ha < -1 or cos_ha > 1:
            return None
        ha = math.degrees(math.acos(cos_ha))
        solar_noon = 720 - 4 * lon - eq_time
        t_minutes = solar_noon - 4 * ha if rising else solar_noon + 4 * ha
    return t_minutes


def sun_times(d: date, lat: float, lon: float, tz: str) -> Optional[dict]:
    """Return local sunrise/sunset/golden-hour datetimes for the date, or None
    in polar conditions. Golden hour = the last hour before sunset (and the
    first hour after sunrise) — the photographer's definition."""
    rise_m = _sun_event_utc_minutes(d, lat, lon, rising=True)
    set_m = _sun_event_utc_minutes(d, lat, lon, rising=False)
    if rise_m is None or set_m is None:
        return None
    zone = ZoneInfo(tz)
    base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    sunrise = (base + timedelta(minutes=rise_m)).astimezone(zone)
    sunset = (base + timedelta(minutes=set_m)).astimezone(zone)
    # Both events computed against the UTC date; near the date line or for
    # western-hemisphere evenings this can land on the wrong local day.
    # Nudge each one to the requested local date.
    if sunrise.date() != d:
        sunrise += timedelta(days=(d - sunrise.date()).days)
    if sunset.date() != d:
        sunset += timedelta(days=(d - sunset.date()).days)
    daylight = sunset - sunrise
    return {
        "sunrise": sunrise,
        "sunset": sunset,
        "golden_am_end": sunrise + timedelta(hours=1),
        "golden_pm_start": sunset - timedelta(hours=1),
        "daylight_minutes": int(daylight.total_seconds() // 60),
    }


def fmt_clock(dt: datetime) -> str:
    """'6:31A' / '7:52P' — the compact style used elsewhere on the board."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}{'A' if dt.hour < 12 else 'P'}"
