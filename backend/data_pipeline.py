"""Adapter: EnrichedAircraft (rich pydantic) -> AircraftView (board-shaped).

Converts datetimes into "842P" style local-time strings, parses the IATA
flight number into airline + numeric, derives delay minutes from sched/actual,
picks airline tile color, and packages everything for the formatter.
"""
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import sightings_db
from .airline_colors import GREEN, RED, WHITE, YELLOW, color_for
from .formatter import AircraftView, AirportView
from .models import EnrichedAircraft

from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)


def _fmt_time(dt: Optional[datetime]) -> str:
    """Format a UTC datetime as '842P' (no leading zero on hour, lowercase
    meridian as A/P). Returns '----' if dt is None."""
    if dt is None:
        return "----"
    local = dt.astimezone(_LOCAL_TZ)
    hour = local.hour
    minute = local.minute
    meridian = "P" if hour >= 12 else "A"
    h12 = hour % 12 or 12
    return f"{h12}{minute:02d}{meridian}"


def _delay_minutes(scheduled: Optional[datetime], actual: Optional[datetime]) -> int:
    """Return positive=late, negative=early, 0=on time or missing data."""
    if scheduled is None or actual is None:
        return 0
    return int((actual - scheduled).total_seconds() // 60)


def _parse_flight_number(flight_id: Optional[str], fallback_iata: Optional[str]) -> tuple[str, int]:
    """Split 'AA1234' or 'B61955' into ('AA', 1234) / ('B6', 1955).

    IATA airline codes are always 2 chars and may contain a digit in position 2
    (B6, F9, G4, 9E, etc.) — so we can't use isalpha() on the prefix. We require
    position 0 to be a letter, then take chars 0-1 as the carrier and parse the
    rest as the flight number.
    """
    if flight_id and len(flight_id) >= 3 and flight_id[0].isalpha():
        prefix = flight_id[:2].upper()
        rest = flight_id[2:]
        try:
            num = int(rest.lstrip("0") or "0")
            return prefix, num
        except ValueError:
            pass
    return ((fallback_iata or "??").upper()[:2], 0)


def to_aircraft_view(enriched: EnrichedAircraft) -> Optional[AircraftView]:
    """Build an AircraftView from an enriched aircraft.

    Returns None when essential data is missing — the caller skips the push,
    leaving the previous frame on the board. We require:
      - flight identity (flight_number OR callsign), and
      - route (both origin_iata and destination_iata), so we don't render
        '? -- ?' to the board when enrichment is mid-flight.
    """
    if not (enriched.flight_number or enriched.callsign):
        return None
    if not enriched.origin_iata or not enriched.destination_iata:
        return None

    airline_iata, flight_num = _parse_flight_number(
        enriched.flight_number, enriched.airline_iata
    )

    sch_dep = enriched.scheduled_departure
    act_dep = enriched.departure_time or enriched.estimated_departure
    sch_arr = enriched.scheduled_arrival
    est_arr = enriched.estimated_arrival or enriched.arrival_time

    # King-of-the-hill: this tail wears the crown if its sighting count equals
    # the max across the DB. Ties (multiple tails at the same max) all wear it.
    sighting_count = enriched.seen_count or 1
    is_king = bool(enriched.registration) and sighting_count > 1 and sighting_count >= sightings_db.max_sighting_count()

    return AircraftView(
        airline_iata=airline_iata,
        flight_number=flight_num,
        airline_color=color_for(airline_iata),
        origin_iata=(enriched.origin_iata or "?").upper()[:3],
        destination_iata=(enriched.destination_iata or "?").upper()[:3],
        scheduled_departure=_fmt_time(sch_dep),
        actual_departure=_fmt_time(act_dep),
        departure_delay_min=_delay_minutes(sch_dep, act_dep),
        scheduled_arrival=_fmt_time(sch_arr),
        estimated_arrival=_fmt_time(est_arr),
        arrival_delay_min=_delay_minutes(sch_arr, est_arr),
        tail_number=(enriched.registration or "?").upper()[:7],
        year_built=enriched.year_built,
        sighting_count=sighting_count,
        aircraft_name=(enriched.aircraft_name or enriched.aircraft_type or "AIRCRAFT").upper(),
        is_rare=enriched.is_rare,
        rare_reason=enriched.rare_reason,
        livery_name=enriched.livery_name,
        is_king=is_king,
    )


_STATUS_COLOR_MAP = {
    "green": GREEN, "yellow": YELLOW, "red": RED, "orange": YELLOW,
}


def to_airport_view(airport_code: str, status: Optional[dict]) -> AirportView:
    """Build an AirportView for the row-6 footer.

    Personal counts only — both numbers come from our local sightings DB
    (free). Each is the count of flights WE pushed to the board today
    (since local Eastern midnight) that arrived at / departed from the airport.

    Bonus: the ratio of ARR:DEP doubles as a runway-flow indicator. From
    Jake's apartment (~3.1nm N of DCA), north flow (rwy 1) makes departures
    visible and arrivals invisible — vice versa for south flow. So a glance
    at the footer tells you which way the wind is blowing today.

    Tile color is still FA's airport-status `color` from /delays — the only
    paid call on the footer. Cached aggressively (see enrichment.py).
    """
    color = WHITE
    if status:
        color = _STATUS_COLOR_MAP.get((status.get("color") or "").lower(), WHITE)
    arr = sightings_db.count_seen_today(airport_code, "arrival")
    dep = sightings_db.count_seen_today(airport_code, "departure")
    return AirportView(
        iata=airport_code.upper(),
        status_color=color,
        arrivals_today=arr,
        departures_today=dep,
    )
