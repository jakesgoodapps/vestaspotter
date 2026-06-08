from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class RawAircraft(BaseModel):
    icao24: str
    callsign: Optional[str] = None
    origin_country: Optional[str] = None
    latitude: float
    longitude: float
    altitude_m: Optional[float] = None
    velocity_ms: Optional[float] = None
    heading: Optional[float] = None
    vertical_rate: Optional[float] = None
    on_ground: bool = False
    squawk: Optional[str] = None
    category: Optional[int] = None

    @property
    def altitude_ft(self) -> Optional[float]:
        return None if self.altitude_m is None else self.altitude_m * 3.28084

    @property
    def speed_knots(self) -> Optional[float]:
        return None if self.velocity_ms is None else self.velocity_ms * 1.94384

    @property
    def distance_nm(self) -> Optional[float]:
        return getattr(self, "_distance_nm", None)


class EnrichedAircraft(BaseModel):
    icao24: str
    callsign: Optional[str] = None
    latitude: float
    longitude: float
    altitude_ft: Optional[float] = None
    speed_knots: Optional[float] = None
    heading: Optional[float] = None
    vertical_rate: Optional[float] = None
    distance_nm: Optional[float] = None

    flight_number: Optional[str] = None
    airline: Optional[str] = None
    airline_iata: Optional[str] = None
    operated_by: Optional[str] = None
    codeshares_iata: list[str] = []
    origin_airport: Optional[str] = None
    origin_city: Optional[str] = None
    origin_iata: Optional[str] = None
    destination_airport: Optional[str] = None
    destination_city: Optional[str] = None
    destination_iata: Optional[str] = None
    aircraft_type: Optional[str] = None
    aircraft_name: Optional[str] = None
    aircraft_manufacturer: Optional[str] = None
    aircraft_description: Optional[str] = None
    aircraft_engine_count: Optional[int] = None
    registration: Optional[str] = None
    departure_time: Optional[datetime] = None       # actual_off
    arrival_time: Optional[datetime] = None         # actual_on
    estimated_departure: Optional[datetime] = None
    estimated_arrival: Optional[datetime] = None
    scheduled_departure: Optional[datetime] = None
    scheduled_arrival: Optional[datetime] = None
    delay_minutes: Optional[int] = None
    status: Optional[str] = None
    progress_pct: Optional[int] = None
    route: Optional[str] = None
    diverted: bool = False
    cancelled: bool = False

    gate_origin: Optional[str] = None
    gate_destination: Optional[str] = None
    terminal_origin: Optional[str] = None
    terminal_destination: Optional[str] = None

    filed_altitude_ft: Optional[int] = None
    filed_airspeed_kts: Optional[int] = None
    scheduled_duration_seconds: Optional[int] = None
    route_distance_nm: Optional[int] = None

    owner_name: Optional[str] = None
    owner_location: Optional[str] = None

    seen_count: int = 0
    last_seen_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None

    is_rare: bool = False
    rare_reason: Optional[str] = None

    # VestaSpotter additions
    year_built: Optional[int] = None
    livery_name: Optional[str] = None
