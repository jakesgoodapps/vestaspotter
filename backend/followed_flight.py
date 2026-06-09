"""Followed-flight tracker — track a single user-selected flight end-to-end.

The user picks one flight (number + date) via the dashboard. We persist the
selection, poll FlightAware on a phase-appropriate cadence (~hourly when
pre-flight, every 15 min in air, every 2 min on approach), derive a board
phase from the latest data, and the scheduler hands the formatter the right
render template.

When a followed flight is active, normal overhead detection still LOGS to
the sightings DB for stats, but does NOT render. The followed flight gets
exclusive control of the board until 30 min after gate arrival.

Persistence: data/followed_flight.json — single JSON, survives restart.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from .config import settings as _settings

_LOCAL_TZ = ZoneInfo(_settings.local_timezone)

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "followed_flight.json"
_lock = Lock()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Row 5 progress bar: "BOS" (3 cols) + 16 tile cells + "LAX" (3 cols) = 22.
PROGRESS_TILES = 16

# How long after gate arrival before we auto-exit follow mode and resume
# normal overhead rendering.
POST_LANDED_HOLD_SECONDS = 30 * 60

# Boarding window — within this many seconds of scheduled_out, we render
# the BOARDING board (assuming we haven't already pushed back).
BOARDING_WINDOW_SECONDS = 30 * 60

# Within this many seconds of estimated_on we transition AIRBORNE → APPROACH.
APPROACH_WINDOW_SECONDS = 15 * 60

# Label max length on row 6 (1 leading tile + 21 chars of text = 22 cols).
LABEL_MAX_CHARS = 21

# Per-phase polling cadence in seconds.
POLL_CADENCE_SECONDS = {
    "pre_flight":  3600,
    "boarding":     300,
    "taxi_out":     300,
    "airborne":     900,
    "approach":     120,
    "landed":       300,
    "post_landed":  300,
}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    IDLE         = "idle"          # no followed flight
    PRE_FLIGHT   = "pre_flight"    # more than BOARDING_WINDOW from scheduled_out
    BOARDING     = "boarding"      # within boarding window, not yet pushed back
    TAXI_OUT     = "taxi_out"      # actual_out set, actual_off not
    AIRBORNE     = "airborne"      # actual_off set, far from arrival
    APPROACH     = "approach"      # airborne, within APPROACH_WINDOW of estimated_on
    LANDED       = "landed"        # actual_on set, actual_in not (taxiing in)
    POST_LANDED  = "post_landed"   # actual_in set, within hold window
    CANCELLED    = "cancelled"
    DIVERTED     = "diverted"


class WeatherSeverity(str, Enum):
    SMOOTH       = "smooth"        # → blue tile (default)
    LIGHT_TURB   = "light_turb"    # → yellow
    MODERATE     = "moderate"      # → orange
    SEVERE       = "severe"        # → red

WEATHER_TO_TILE_COLOR = {
    WeatherSeverity.SMOOTH:     "blue",
    WeatherSeverity.LIGHT_TURB: "yellow",
    WeatherSeverity.MODERATE:   "orange",
    WeatherSeverity.SEVERE:     "red",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FollowedFlight:
    # User input
    user_ident: str               # what user typed: "AA1" or "AAL1"
    user_date: str                # "YYYY-MM-DD" in origin local tz
    label: str                    # row 6 personalization, e.g. "MOM TO CALI"

    # Resolved from FA on initial verify (fixed for the duration)
    fa_flight_id: str             # FA's unique per-instance id
    operator_iata: str            # "AA", "UA" — for color tile lookup
    flight_number: str            # "1"
    origin_iata: str              # "JFK"
    destination_iata: str         # "LAX"
    aircraft_type: str            # "A321"
    registration: Optional[str]
    route_distance_nm: int

    # Times (ISO 8601 UTC strings as FA returns them — store raw, parse on read)
    scheduled_out: Optional[str]
    scheduled_off: Optional[str]
    scheduled_on:  Optional[str]
    scheduled_in:  Optional[str]
    estimated_out: Optional[str] = None
    estimated_off: Optional[str] = None
    estimated_on:  Optional[str] = None
    estimated_in:  Optional[str] = None
    actual_out:    Optional[str] = None
    actual_off:    Optional[str] = None
    actual_on:     Optional[str] = None
    actual_in:     Optional[str] = None

    # FA flags / status
    status: str = ""              # FA's prose status, e.g. "En Route / On Time"
    cancelled: bool = False
    diverted:  bool = False
    divert_destination_iata: Optional[str] = None

    # Gates / terminals (sometimes null)
    gate_origin: Optional[str] = None
    gate_destination: Optional[str] = None
    terminal_origin: Optional[str] = None
    terminal_destination: Optional[str] = None
    baggage_claim: Optional[str] = None

    # Live position (from /position endpoint, only valid AIRBORNE+)
    progress_percent: int = 0
    last_altitude_100s_ft: Optional[int] = None
    last_groundspeed_kt:   Optional[int] = None
    last_latitude:         Optional[float] = None
    last_longitude:        Optional[float] = None
    last_altitude_change:  Optional[str] = None    # 'C', 'D', '-'
    last_position_time:    Optional[str] = None    # ISO UTC
    last_city:             Optional[str] = None    # from Nominatim

    # Weather history along the route. Index 0 = closest to origin,
    # index PROGRESS_TILES-1 = closest to destination. None = untraversed.
    # Each filled value is one of the WEATHER_TO_TILE_COLOR values.
    progress_tile_colors: list[Optional[str]] = field(default_factory=lambda: [None] * PROGRESS_TILES)

    # Internal bookkeeping
    started_at: str = ""           # when user picked this flight
    last_poll_at: Optional[str] = None
    landed_at: Optional[str] = None   # when we first observed POST_LANDED

    # ----- derived state -----

    def derive_phase(self, now: Optional[datetime] = None) -> Phase:
        """Determine current phase from FA fields + clock.

        Checks are ordered most-specific to least; first match wins.
        """
        if self.cancelled:
            return Phase.CANCELLED
        if self.diverted:
            return Phase.DIVERTED

        now = now or datetime.now(timezone.utc)

        # Post-landed window after gate arrival
        if self.actual_in:
            actual_in_dt = _parse_iso(self.actual_in)
            if actual_in_dt:
                age = (now - actual_in_dt).total_seconds()
                if age <= POST_LANDED_HOLD_SECONDS:
                    return Phase.POST_LANDED
                return Phase.IDLE  # auto-exit

        # Just touched down, taxiing in
        if self.actual_on and not self.actual_in:
            return Phase.LANDED

        # In the air
        if self.actual_off and not self.actual_on:
            # Within approach window of estimated landing?
            target_landing = self.estimated_on or self.scheduled_on
            if target_landing:
                landing_dt = _parse_iso(target_landing)
                if landing_dt and (landing_dt - now).total_seconds() <= APPROACH_WINDOW_SECONDS:
                    return Phase.APPROACH
            # Or descending below 18000 ft (FL180) per altitude trend
            if (self.last_altitude_100s_ft is not None
                and self.last_altitude_100s_ft < 180
                and self.last_altitude_change == "D"):
                return Phase.APPROACH
            return Phase.AIRBORNE

        # Pushed back, taxiing out
        if self.actual_out and not self.actual_off:
            return Phase.TAXI_OUT

        # Pre-departure — boarding vs further out
        target_departure = self.estimated_out or self.scheduled_out
        if target_departure:
            dep_dt = _parse_iso(target_departure)
            if dep_dt:
                until_dep = (dep_dt - now).total_seconds()
                if until_dep <= BOARDING_WINDOW_SECONDS:
                    return Phase.BOARDING
        return Phase.PRE_FLIGHT

    def update_progress_tile(self, severity: WeatherSeverity) -> None:
        """Color the current frontier tile based on current weather.

        The frontier is the tile corresponding to current progress_percent.
        Tiles to the LEFT (already-traversed) are left alone — they preserve
        whatever weather was current when they were the frontier.
        """
        if self.progress_percent <= 0:
            return
        # Map % to tile index 0..PROGRESS_TILES-1
        tile_idx = min(int(self.progress_percent / 100 * PROGRESS_TILES), PROGRESS_TILES - 1)
        self.progress_tile_colors[tile_idx] = WEATHER_TO_TILE_COLOR[severity]

    def time_until_departure_seconds(self, now: Optional[datetime] = None) -> Optional[int]:
        """Seconds until best-guess departure. Negative if past, None if no times."""
        now = now or datetime.now(timezone.utc)
        target = self.estimated_out or self.scheduled_out
        if not target:
            return None
        dt = _parse_iso(target)
        return int((dt - now).total_seconds()) if dt else None

    def time_until_arrival_seconds(self, now: Optional[datetime] = None) -> Optional[int]:
        """Seconds until best-guess gate arrival."""
        now = now or datetime.now(timezone.utc)
        target = self.estimated_in or self.scheduled_in
        if not target:
            return None
        dt = _parse_iso(target)
        return int((dt - now).total_seconds()) if dt else None

    def arrival_delay_minutes(self) -> int:
        """Positive = late, negative = early, 0 = on time. Based on actual or estimated vs scheduled."""
        if not self.scheduled_in:
            return 0
        sched = _parse_iso(self.scheduled_in)
        actual = _parse_iso(self.actual_in) if self.actual_in else _parse_iso(self.estimated_in or "")
        if not sched or not actual:
            return 0
        return int((actual - sched).total_seconds() // 60)


# ---------------------------------------------------------------------------
# Module-level state + persistence
# ---------------------------------------------------------------------------

_current: Optional[FollowedFlight] = None


def is_active() -> bool:
    """True if a followed flight is currently being tracked (not IDLE)."""
    f = get_current()
    if f is None:
        return False
    return f.derive_phase() != Phase.IDLE


def get_current() -> Optional[FollowedFlight]:
    """Get the in-memory current followed flight, loading from disk if needed."""
    global _current
    if _current is None:
        _current = _load_from_disk()
    return _current


def set_current(flight: Optional[FollowedFlight]) -> None:
    """Replace the in-memory followed flight and persist to disk."""
    global _current
    with _lock:
        _current = flight
        if flight is None:
            _clear_disk()
        else:
            _save_to_disk(flight)


def clear() -> None:
    """Stop following whatever flight is active."""
    set_current(None)


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------

def _save_to_disk(flight: FollowedFlight) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(flight), indent=2, default=str))
    tmp.replace(_STATE_PATH)


def _clear_disk() -> None:
    if _STATE_PATH.exists():
        _STATE_PATH.unlink()


def _load_from_disk() -> Optional[FollowedFlight]:
    if not _STATE_PATH.exists():
        return None
    try:
        data = json.loads(_STATE_PATH.read_text())
        # Backfill any new fields added since this file was written
        flight = FollowedFlight(**{k: v for k, v in data.items() if k in FollowedFlight.__dataclass_fields__})
        # Ensure progress_tile_colors is the right length (defensive against version drift)
        if len(flight.progress_tile_colors) != PROGRESS_TILES:
            old = flight.progress_tile_colors
            flight.progress_tile_colors = [None] * PROGRESS_TILES
            for i, v in enumerate(old[:PROGRESS_TILES]):
                flight.progress_tile_colors[i] = v
        return flight
    except Exception as e:
        print(f"followed_flight: failed to load state from {_STATE_PATH}: {e}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_label(raw: str) -> str:
    """Uppercase, strip, truncate to LABEL_MAX_CHARS. Reject empty after sanitize."""
    out = (raw or "").strip().upper()
    return out[:LABEL_MAX_CHARS]


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse FA's ISO 8601 timestamps (always Z-suffixed UTC)."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Flight selection (called by dashboard "Follow a flight" form)
# ---------------------------------------------------------------------------

def pick_instance_by_date(instances: list[dict], user_date: str) -> Optional[dict]:
    """From all instances returned by /flights/{ident}, pick the one whose
    scheduled_out falls on user_date in the origin airport's local timezone.

    user_date is "YYYY-MM-DD". If multiple match, prefers the first one
    (FA returns them in temporal order). If none match exactly, picks the
    closest by date.
    """
    if not instances:
        return None

    target_date = user_date

    def _instance_local_date(f: dict) -> Optional[str]:
        s = f.get("scheduled_out")
        if not s:
            return None
        dt = _parse_iso(s)
        if not dt:
            return None
        origin_tz_name = (f.get("origin") or {}).get("timezone")
        if origin_tz_name:
            try:
                dt = dt.astimezone(ZoneInfo(origin_tz_name))
            except Exception:
                pass
        return dt.date().isoformat()

    # Exact match
    for f in instances:
        if _instance_local_date(f) == target_date:
            return f

    # Fallback: closest by absolute day delta
    target_dt = datetime.fromisoformat(target_date + "T00:00:00")
    def _delta(f: dict) -> int:
        d = _instance_local_date(f)
        if not d:
            return 10**9
        return abs((datetime.fromisoformat(d + "T00:00:00") - target_dt).days)
    return min(instances, key=_delta)


def build_from_fa(instance: dict, user_ident: str, user_date: str, label: str) -> FollowedFlight:
    """Construct a FollowedFlight from an FA /flights/{ident} entry."""
    origin = instance.get("origin") or {}
    dest   = instance.get("destination") or {}
    return FollowedFlight(
        user_ident   = user_ident,
        user_date    = user_date,
        label        = sanitize_label(label),
        fa_flight_id = instance["fa_flight_id"],
        operator_iata    = instance.get("operator_iata") or "",
        flight_number    = instance.get("flight_number") or "",
        origin_iata      = origin.get("code_iata") or "",
        destination_iata = dest.get("code_iata") or "",
        aircraft_type    = instance.get("aircraft_type") or "",
        registration     = instance.get("registration"),
        route_distance_nm = instance.get("route_distance") or 0,
        scheduled_out = instance.get("scheduled_out"),
        scheduled_off = instance.get("scheduled_off"),
        scheduled_on  = instance.get("scheduled_on"),
        scheduled_in  = instance.get("scheduled_in"),
        estimated_out = instance.get("estimated_out"),
        estimated_off = instance.get("estimated_off"),
        estimated_on  = instance.get("estimated_on"),
        estimated_in  = instance.get("estimated_in"),
        actual_out    = instance.get("actual_out"),
        actual_off    = instance.get("actual_off"),
        actual_on     = instance.get("actual_on"),
        actual_in     = instance.get("actual_in"),
        status    = instance.get("status") or "",
        cancelled = bool(instance.get("cancelled")),
        diverted  = bool(instance.get("diverted")),
        gate_origin          = instance.get("gate_origin"),
        gate_destination     = instance.get("gate_destination"),
        terminal_origin      = instance.get("terminal_origin"),
        terminal_destination = instance.get("terminal_destination"),
        baggage_claim        = instance.get("baggage_claim"),
        progress_percent     = int(instance.get("progress_percent") or 0),
        started_at = datetime.now(timezone.utc).isoformat(),
    )


def apply_fa_refresh(flight: FollowedFlight, instance: dict) -> None:
    """Mutate `flight` in place with the latest FA /flights/{ident} response.

    Identity fields (operator, route, registration, fa_flight_id) are NOT
    overwritten — they're locked in at selection time and shouldn't change.
    """
    flight.scheduled_out = instance.get("scheduled_out") or flight.scheduled_out
    flight.scheduled_off = instance.get("scheduled_off") or flight.scheduled_off
    flight.scheduled_on  = instance.get("scheduled_on")  or flight.scheduled_on
    flight.scheduled_in  = instance.get("scheduled_in")  or flight.scheduled_in
    flight.estimated_out = instance.get("estimated_out") or flight.estimated_out
    flight.estimated_off = instance.get("estimated_off") or flight.estimated_off
    flight.estimated_on  = instance.get("estimated_on")  or flight.estimated_on
    flight.estimated_in  = instance.get("estimated_in")  or flight.estimated_in
    flight.actual_out = instance.get("actual_out") or flight.actual_out
    flight.actual_off = instance.get("actual_off") or flight.actual_off
    flight.actual_on  = instance.get("actual_on")  or flight.actual_on
    flight.actual_in  = instance.get("actual_in")  or flight.actual_in
    flight.status     = instance.get("status") or flight.status
    flight.cancelled  = bool(instance.get("cancelled"))
    flight.diverted   = bool(instance.get("diverted"))
    flight.gate_origin          = instance.get("gate_origin")          or flight.gate_origin
    flight.gate_destination     = instance.get("gate_destination")     or flight.gate_destination
    flight.terminal_origin      = instance.get("terminal_origin")      or flight.terminal_origin
    flight.terminal_destination = instance.get("terminal_destination") or flight.terminal_destination
    flight.baggage_claim        = instance.get("baggage_claim")        or flight.baggage_claim
    if instance.get("progress_percent") is not None:
        flight.progress_percent = int(instance["progress_percent"])
    flight.last_poll_at = datetime.now(timezone.utc).isoformat()


def apply_position(flight: FollowedFlight, last_position: dict) -> None:
    """Update live position fields from /flights/{fid}/position.last_position."""
    flight.last_altitude_100s_ft = last_position.get("altitude")
    flight.last_groundspeed_kt   = last_position.get("groundspeed")
    flight.last_latitude         = last_position.get("latitude")
    flight.last_longitude        = last_position.get("longitude")
    flight.last_altitude_change  = last_position.get("altitude_change")
    flight.last_position_time    = last_position.get("timestamp")
