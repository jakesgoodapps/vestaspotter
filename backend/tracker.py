"""Aircraft tracker — polls OpenSky Network and filters to overhead aircraft.

Lifted from PlaneSpotter, imports adapted for backend/ package.
"""
import math
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

from .models import RawAircraft
from .opensky_auth import OpenSkyAuth


class AircraftTracker:
    OPENSKY_BASE = "https://opensky-network.org/api"
    OPENSKY_URL = f"{OPENSKY_BASE}/states/all"

    def __init__(
        self,
        latitude: float,
        longitude: float,
        orientation_deg: float,
        radius_nm: float = 3.0,
        max_altitude_ft: float = 8000,
        min_altitude_ft: float = 0,
        field_of_view_deg: float = 120.0,
        predict_seconds_ahead: int = 0,
        opensky_auth: Optional[OpenSkyAuth] = None,
    ):
        self.lat = latitude
        self.lon = longitude
        self.orientation = orientation_deg
        self.radius_nm = radius_nm
        self.max_altitude_ft = max_altitude_ft
        self.min_altitude_ft = min_altitude_ft
        self.fov = field_of_view_deg
        self.predict_seconds = predict_seconds_ahead
        self.opensky_auth = opensky_auth or OpenSkyAuth()
        self._yesterday_cache: dict[str, tuple[dict, float]] = {}

    async def get_nearby_aircraft(self) -> list[RawAircraft]:
        bbox = self._bounding_box()
        params = {
            "lamin": bbox["lat_min"],
            "lomin": bbox["lon_min"],
            "lamax": bbox["lat_max"],
            "lomax": bbox["lon_max"],
        }

        try:
            async with aiohttp.ClientSession() as session:
                headers = await self.opensky_auth.headers(session)
                async with session.get(
                    self.OPENSKY_URL,
                    params=params,
                    auth=self.opensky_auth.basic,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 429:
                        print("OpenSky rate limited, will retry next cycle")
                        return []
                    if resp.status != 200:
                        print(f"OpenSky returned {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as e:
            print(f"OpenSky request failed: {e}")
            return []

        if not data or not data.get("states"):
            return []

        aircraft = []
        for state in data["states"]:
            if state[5] is None or state[6] is None:
                continue
            if state[8]:
                continue
            ac = RawAircraft(
                icao24=state[0].strip(),
                callsign=state[1].strip() if state[1] else None,
                origin_country=state[2],
                latitude=state[6],
                longitude=state[5],
                altitude_m=state[7] or state[13],
                velocity_ms=state[9],
                heading=state[10],
                vertical_rate=state[11],
                on_ground=state[8],
                squawk=state[14],
                category=state[17] if len(state) > 17 else None,
            )
            aircraft.append(ac)

        return aircraft

    def filter_overhead(self, aircraft: list[RawAircraft]) -> list[RawAircraft]:
        """Return aircraft visible from Jake's window now OR projected to enter
        view within `predict_seconds_ahead`.

        Altitude semantics — important for departing planes:
          - Above max_altitude_ft: ALWAYS reject (high cruisers we'd miss anyway)
          - Below min_altitude_ft: reject IF not predicted-incoming OR not climbing.
            A plane below the floor but actively climbing (vertical_rate > 2 m/s ≈
            400 fpm) AND on a trajectory into our FOV is a JUST-DEPARTED plane —
            exactly what we want to catch with lead time. By the time it reaches
            us it will be well above the floor.

        Each surviving aircraft gets two attached attributes:
            ac._distance_nm   — current distance from observer (nm)
            ac._is_predicted  — True if only projected position is in-view
        """
        overhead = []
        for ac in aircraft:
            alt_ft = ac.altitude_ft
            if alt_ft is not None and alt_ft > self.max_altitude_ft:
                continue

            below_floor = alt_ft is not None and alt_ft < self.min_altitude_ft
            is_climbing = ac.vertical_rate is not None and ac.vertical_rate > 2.0  # m/s

            current_in_window = self._is_in_window(ac.latitude, ac.longitude)
            current_visible = current_in_window and not below_floor

            predicted_visible = False
            if self.predict_seconds > 0 and ac.heading is not None and ac.speed_knots:
                p_lat, p_lon = self._project(
                    ac.latitude, ac.longitude, ac.heading, ac.speed_knots, self.predict_seconds
                )
                projected_in_window = self._is_in_window(p_lat, p_lon)
                if projected_in_window and not current_visible:
                    # Allow below-floor planes IF they're climbing — they'll be
                    # at altitude by the time they reach us. Otherwise apply
                    # the floor (don't predictively-catch a plane that's on
                    # short final or taxiing).
                    predicted_visible = (not below_floor) or is_climbing

            if not (current_visible or predicted_visible):
                continue

            ac._distance_nm = self._haversine_nm(self.lat, self.lon, ac.latitude, ac.longitude)
            ac._is_predicted = predicted_visible and not current_visible
            overhead.append(ac)

        overhead.sort(key=lambda a: (getattr(a, "_is_predicted", False), getattr(a, "_distance_nm", 999)))
        return overhead

    def _is_in_window(self, lat: float, lon: float) -> bool:
        """True if (lat,lon) is within radius AND inside our FOV arc from observer."""
        dist = self._haversine_nm(self.lat, self.lon, lat, lon)
        if dist > self.radius_nm:
            return False
        bearing = self._bearing(self.lat, self.lon, lat, lon)
        return abs(self._angle_diff(bearing, self.orientation)) <= self.fov / 2

    @staticmethod
    def _project(lat: float, lon: float, heading_deg: float, speed_knots: float, seconds_ahead: int) -> tuple[float, float]:
        """Project a great-circle-ish position forward. For our scale (<10nm, <60s)
        flat-earth math is fine — no need for full geodesics."""
        distance_nm = speed_knots * (seconds_ahead / 3600.0)
        hdg = math.radians(heading_deg)
        dlat_nm = distance_nm * math.cos(hdg)
        dlon_nm = distance_nm * math.sin(hdg)
        new_lat = lat + dlat_nm / 60.0
        new_lon = lon + dlon_nm / (60.0 * math.cos(math.radians(lat)))
        return new_lat, new_lon

    def _bounding_box(self) -> dict:
        lat_delta = self.radius_nm / 60.0
        lon_delta = self.radius_nm / (60.0 * math.cos(math.radians(self.lat)))
        return {
            "lat_min": self.lat - lat_delta,
            "lat_max": self.lat + lat_delta,
            "lon_min": self.lon - lon_delta,
            "lon_max": self.lon + lon_delta,
        }

    @staticmethod
    def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R_NM = 3440.065
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        )
        return 2 * R_NM * math.asin(math.sqrt(a))

    @staticmethod
    def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360

    @staticmethod
    def _angle_diff(a: float, b: float) -> float:
        return (a - b + 180) % 360 - 180
