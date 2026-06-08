"""POTUS movement detector via Park Police helicopter pattern over the White House.

Observed pattern: before a presidential helo movement, a US Park Police
helicopter does 30-40 min of orbital laps over the White House. When it breaks
pattern and flies away, presidential movement is imminent (~10 min).

Opt-in via ENABLE_POTUS_DETECTOR. Useful primarily for spotters within visual
range of the White House (downtown DC).

We watch the same OpenSky polling we already do — no new API spend.

Detection layers (must all pass):
  1. Low altitude (< 2000 ft) — helo, not commercial overflight
  2. Slow groundspeed (< 150 kt) — helo cruise/orbit, not approach traffic
  3. Position cluster centroid within ~0.5nm of White House
  4. Quadrant-transition sequence consistent with circular orbit (lap counting)

State machine:
  IDLE → CIRCLING (a helo started clustering near WH)
       → CONFIRMED (≥2 full laps observed → "movement 30-40m")
       → IMMINENT (heli broke pattern, flying away → "movement ~10m")
       → POST (cooldown after imminent, ~15 min)
       → IDLE

State is in-memory only — restart loses the detection state, which is fine.
Detection re-engages on next observed pattern.
"""
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# Orbit-center coordinates the detector watches helicopters orbit AROUND.
# Defaults to the White House — DC-specific. Configurable for other deployments
# via POTUS_ORBIT_CENTER_LAT/LON env vars (see config.py). Read at import time;
# restart container to change.
from .config import settings as _detector_settings
WH_LAT = _detector_settings.potus_orbit_center_lat
WH_LON = _detector_settings.potus_orbit_center_lon

# Detection thresholds
MAX_HELI_ALT_FT = 2000
MAX_HELI_SPEED_KT = 150
CENTROID_TOLERANCE_NM = 0.6   # how close cluster centroid must be to WH
ORBIT_MIN_RADIUS_NM = 0.15    # too close = probably landed
ORBIT_MAX_RADIUS_NM = 1.5     # too far = not an orbit
MIN_LAPS_FOR_CONFIRMED = 2
POSITION_HISTORY_WINDOW_S = 900  # 15-min rolling window per helo
HELI_GONE_THRESHOLD_S = 240      # haven't seen helo in this long → drop it
PATTERN_BREAK_DISTANCE_NM = 2.5  # helo this far from WH after orbiting → flew away
IMMINENT_DURATION_S = 900        # how long to hold IMMINENT before POST (real POTUS)
IMMINENT_DURATION_DRILL_S = 60   # short hold for drill mode (just enough to show "bye helo")
POST_DURATION_S = 600            # cooldown for real POTUS event
POST_DURATION_DRILL_S = 60       # cooldown for drill mode — release board fast


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def quadrant_of(lat: float, lon: float) -> int:
    """Which quadrant of the White House is (lat, lon) in?
    0=NE, 1=SE, 2=SW, 3=NW (CW order so we can detect orbit direction)."""
    if lat >= WH_LAT and lon >= WH_LON: return 0  # NE
    if lat < WH_LAT and lon >= WH_LON:  return 1  # SE
    if lat < WH_LAT and lon < WH_LON:   return 2  # SW
    return 3                                       # NW


@dataclass
class HeliTrack:
    """Rolling position history + lap progress for one helicopter."""
    icao24: str
    callsign: Optional[str]
    positions: deque = field(default_factory=lambda: deque(maxlen=200))  # (lat, lon, alt_ft, ts)
    quadrant_sequence: list = field(default_factory=list)  # ordered visited quadrants
    last_seen_ts: float = 0.0
    laps_observed: int = 0
    first_lap_ts: Optional[float] = None
    last_quadrant: Optional[int] = None

    def add(self, lat: float, lon: float, alt_ft: float, ts: float) -> None:
        self.positions.append((lat, lon, alt_ft, ts))
        self.last_seen_ts = ts
        # Trim positions older than window
        cutoff = ts - POSITION_HISTORY_WINDOW_S
        while self.positions and self.positions[0][3] < cutoff:
            self.positions.popleft()
        # Update quadrant sequence
        q = quadrant_of(lat, lon)
        if q != self.last_quadrant:
            self.quadrant_sequence.append(q)
            self.last_quadrant = q
            # Trim sequence to last 16 transitions (4 laps worth) to keep memory bounded
            if len(self.quadrant_sequence) > 16:
                self.quadrant_sequence = self.quadrant_sequence[-16:]

    def centroid_distance_to_wh(self) -> float:
        if not self.positions:
            return float("inf")
        avg_lat = sum(p[0] for p in self.positions) / len(self.positions)
        avg_lon = sum(p[1] for p in self.positions) / len(self.positions)
        return haversine_nm(avg_lat, avg_lon, WH_LAT, WH_LON)

    def distance_to_wh(self) -> float:
        """Distance of MOST RECENT position to WH."""
        if not self.positions:
            return float("inf")
        lat, lon, _, _ = self.positions[-1]
        return haversine_nm(lat, lon, WH_LAT, WH_LON)

    def update_lap_count(self) -> None:
        """A 'lap' = visiting 4 distinct adjacent quadrants in CW or CCW order.
        We count the most recent complete sequence (e.g., 0,1,2,3 or 3,2,1,0)."""
        seq = self.quadrant_sequence
        laps = 0
        # CW: 0→1→2→3→0  or any rotation
        # CCW: 0→3→2→1→0
        for i in range(len(seq) - 3):
            window = seq[i:i+4]
            cw_ok = all((window[j+1] - window[j]) % 4 == 1 for j in range(3))
            ccw_ok = all((window[j] - window[j+1]) % 4 == 1 for j in range(3))
            if cw_ok or ccw_ok:
                laps += 1
        # Each consecutive 4-window counts as one lap; overlapping windows would over-count
        # so divide by overlap factor. For now just take a conservative count:
        self.laps_observed = max(self.laps_observed, laps)
        if self.laps_observed >= 1 and self.first_lap_ts is None:
            self.first_lap_ts = self.last_seen_ts


@dataclass
class DetectorState:
    state: str = "IDLE"   # IDLE | CIRCLING | CONFIRMED | IMMINENT | POST
    active_icao24: Optional[str] = None
    active_callsign: Optional[str] = None
    laps_at_confirm: int = 0
    confirmed_at: Optional[float] = None
    imminent_at: Optional[float] = None
    post_until: Optional[float] = None
    last_change_at: float = 0.0
    last_check_at: float = 0.0
    # Drill mode = "we got the orbit pattern but factba.se shows no POTUS trip
    # in the next 90min." Almost certainly a routine USPP patrol or training.
    # When True: shorter IMMINENT + POST so the board releases back to normal
    # flight tracking ~2min after the helo leaves, instead of holding 25min.
    is_drill_suspected: bool = False


class PotusDetector:
    def __init__(self):
        self.tracks: dict[str, HeliTrack] = {}
        self.state = DetectorState()

    def _ingest(self, aircraft_list, now: float) -> None:
        """Add candidates to tracks + drop stale ones."""
        # Drop stale helis we haven't seen in HELI_GONE_THRESHOLD_S
        stale = [k for k, t in self.tracks.items() if now - t.last_seen_ts > HELI_GONE_THRESHOLD_S]
        for k in stale:
            del self.tracks[k]

        for ac in aircraft_list:
            # Skip if missing core fields
            if ac.altitude_ft is None or ac.speed_knots is None:
                continue
            # Layer 1+2: alt + speed gate (helo, not commercial)
            if ac.altitude_ft > MAX_HELI_ALT_FT or ac.altitude_ft < 0:
                continue
            if ac.speed_knots > MAX_HELI_SPEED_KT:
                continue
            # Layer 3: be near WH. We use TWO thresholds:
            #   - NEW tracks only created within 2nm of WH (avoid random helo noise)
            #   - EXISTING tracks keep updating out to 5nm (so we observe fly-away
            #     pattern-breaks after orbit, which is the whole point of IMMINENT)
            d_wh = haversine_nm(ac.latitude, ac.longitude, WH_LAT, WH_LON)
            if d_wh > 5.0:
                continue
            track = self.tracks.get(ac.icao24)
            if track is None:
                if d_wh > 2.0:
                    continue  # too far to start a new track
                track = HeliTrack(icao24=ac.icao24, callsign=ac.callsign)
                self.tracks[ac.icao24] = track
            track.add(ac.latitude, ac.longitude, ac.altitude_ft, now)
            track.update_lap_count()

    def update(self, aircraft_list, now: Optional[float] = None) -> DetectorState:
        """Process a tick of OpenSky data and return the current detector state."""
        if now is None:
            now = time.time()
        self.state.last_check_at = now
        self._ingest(aircraft_list, now)

        # Find the best active candidate: a track whose centroid is near WH,
        # with the most laps. If none, we may transition out of CIRCLING.
        candidate = None
        for t in self.tracks.values():
            if t.centroid_distance_to_wh() > CENTROID_TOLERANCE_NM:
                continue
            if not candidate or t.laps_observed > candidate.laps_observed:
                candidate = t

        prev = self.state.state

        if self.state.state == "POST":
            if self.state.post_until and now >= self.state.post_until:
                # Reset drill flag on return to idle so the next pattern is fresh
                self.state.is_drill_suspected = False
                self._transition("IDLE", now)
            return self.state

        if self.state.state == "IMMINENT":
            hold = IMMINENT_DURATION_DRILL_S if self.state.is_drill_suspected else IMMINENT_DURATION_S
            post_dur = POST_DURATION_DRILL_S if self.state.is_drill_suspected else POST_DURATION_S
            if self.state.imminent_at and now - self.state.imminent_at >= hold:
                self.state.post_until = now + post_dur
                self._transition("POST", now)
            return self.state

        if self.state.state == "CONFIRMED":
            # Check for pattern break: the active heli flew >2.5nm from WH
            t = self.tracks.get(self.state.active_icao24) if self.state.active_icao24 else None
            if t and t.distance_to_wh() > PATTERN_BREAK_DISTANCE_NM:
                self.state.imminent_at = now
                self._transition("IMMINENT", now)
            elif candidate and candidate.laps_observed > self.state.laps_at_confirm:
                self.state.laps_at_confirm = candidate.laps_observed
            return self.state

        if self.state.state == "CIRCLING":
            if not candidate:
                # Lost the pattern — drop back to IDLE
                self._transition("IDLE", now)
            elif candidate.laps_observed >= MIN_LAPS_FOR_CONFIRMED:
                self.state.active_icao24 = candidate.icao24
                self.state.active_callsign = candidate.callsign
                self.state.laps_at_confirm = candidate.laps_observed
                self.state.confirmed_at = now
                self._transition("CONFIRMED", now)
            else:
                # Still circling, update active
                self.state.active_icao24 = candidate.icao24
                self.state.active_callsign = candidate.callsign
            return self.state

        # IDLE
        if candidate and candidate.laps_observed >= 1:
            self.state.active_icao24 = candidate.icao24
            self.state.active_callsign = candidate.callsign
            self._transition("CIRCLING", now)
        elif candidate:
            # We have a centroid-matching helo but no lap yet — still IDLE, just track
            pass
        return self.state

    def _transition(self, new_state: str, now: float) -> None:
        self.state.state = new_state
        self.state.last_change_at = now

    def status_dict(self) -> dict:
        s = self.state
        out = {
            "state": s.state,
            "active_icao24": s.active_icao24,
            "active_callsign": s.active_callsign,
            "laps_observed": 0,
            "last_change_at": s.last_change_at,
            "tracking_helo_count": len(self.tracks),
        }
        if s.active_icao24 and s.active_icao24 in self.tracks:
            t = self.tracks[s.active_icao24]
            out["laps_observed"] = t.laps_observed
            out["centroid_distance_nm"] = round(t.centroid_distance_to_wh(), 2)
            out["current_distance_nm"] = round(t.distance_to_wh(), 2)
        if s.confirmed_at:
            out["confirmed_age_s"] = int(s.last_check_at - s.confirmed_at)
        if s.imminent_at:
            out["imminent_age_s"] = int(s.last_check_at - s.imminent_at)
        return out
