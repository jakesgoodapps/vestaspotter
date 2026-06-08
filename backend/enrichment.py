"""Flight enrichment — FlightAware AeroAPI + custom data (rare types, liveries,
FAA registry year-built lookup). Lifted from PlaneSpotter's enrichment.py with:

  - imports adapted for backend/ package
  - history → sightings_db (renamed but same API)
  - photo/weather sections kept (cheap, cached) but not consumed by the renderer
  - new: year_built fetch via faa_registry
  - new: livery lookup via custom_data
  - new: airport-specific rare check via custom_data
"""
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Optional

from . import cost_tracker, faa_registry
from . import sightings_db as history
from .custom_data import get_aircraft_name, get_airport_rare, get_livery
from .models import EnrichedAircraft, RawAircraft


_MAINLINE_PRIORITY = ["AA", "DL", "UA", "AS", "WN", "B6", "NK", "F9", "HA"]
_MAINLINE_IATA_TO_NAME = {
    "AA": "American Airlines", "DL": "Delta Air Lines", "UA": "United Airlines",
    "AS": "Alaska Airlines", "WN": "Southwest Airlines", "B6": "JetBlue Airways",
    "NK": "Spirit Airlines", "F9": "Frontier Airlines", "HA": "Hawaiian Airlines",
}


def _pick_marketing_codeshare(codeshares_iata: list[str]) -> tuple[Optional[str], Optional[str]]:
    if not codeshares_iata:
        return (None, None)
    for carrier in _MAINLINE_PRIORITY:
        for cs in codeshares_iata:
            if cs.startswith(carrier) and len(cs) > len(carrier):
                return (cs, _MAINLINE_IATA_TO_NAME[carrier])
    return (None, None)


AIRLINE_CODES = {
    "AAL": ("American Airlines", "AA"),
    "DAL": ("Delta Air Lines", "DL"),
    "UAL": ("United Airlines", "UA"),
    "SWA": ("Southwest Airlines", "WN"),
    "JBU": ("JetBlue Airways", "B6"),
    "NKS": ("Spirit Airlines", "NK"),
    "FFT": ("Frontier Airlines", "F9"),
    "ASA": ("Alaska Airlines", "AS"),
    "RPA": ("Republic Airways", "YX"),
    "SKW": ("SkyWest Airlines", "OO"),
    "ENY": ("Envoy Air", "MQ"),
    "PDT": ("Piedmont Airlines", "PT"),
    "JIA": ("PSA Airlines", "OH"),
    "EDV": ("Endeavor Air", "9E"),
    "ASH": ("Mesa Airlines", "YV"),
    "GJS": ("GoJet Airlines", "G7"),
    "QXE": ("Horizon Air", "QX"),
    "UCA": ("CommutAir", "C5"),
    "BAW": ("British Airways", "BA"),
    "DLH": ("Lufthansa", "LH"),
    "AFR": ("Air France", "AF"),
    "ACA": ("Air Canada", "AC"),
}


# Global rare list (widebodies + military regardless of airport)
RARE_TYPES = {
    "A388": "AIRBUS A380", "B744": "BOEING 747-400", "B748": "BOEING 747-8",
    "B742": "BOEING 747-200", "B772": "BOEING 777-200", "B77L": "BOEING 777-200LR",
    "B77W": "BOEING 777-300ER", "B788": "BOEING 787-8", "B789": "BOEING 787-9",
    "B78X": "BOEING 787-10", "A359": "AIRBUS A350-900", "A35K": "AIRBUS A350-1000",
    "MD11": "MCDONNELL DOUGLAS MD-11", "CONC": "CONCORDE",
}


_FLIGHT_TTL = 300
_OWNER_TTL = 604800
_TYPE_TTL = 604800
# Airport status TTL: 4 hours. Color rarely changes (mostly green); refreshing
# this 6×/day at $0.01 = ~$2/month. Compare to the 10-min default which would
# burn ~$15/day on /delays + /flights/counts combined (the $750 bill culprit).
_AIRPORT_STATUS_TTL = 14400


class FlightEnricher:
    AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._flight_cache: dict[str, tuple[dict, float]] = {}
        self._owner_cache: dict[str, tuple[dict, float]] = {}
        self._type_cache: dict[str, tuple[dict, float]] = {}
        self._airport_status_cache: dict[str, tuple[dict, float]] = {}

    async def enrich(self, aircraft: RawAircraft) -> EnrichedAircraft:
        enriched = EnrichedAircraft(
            icao24=aircraft.icao24,
            callsign=aircraft.callsign,
            latitude=aircraft.latitude,
            longitude=aircraft.longitude,
            altitude_ft=aircraft.altitude_ft,
            speed_knots=aircraft.speed_knots,
            heading=aircraft.heading,
            vertical_rate=aircraft.vertical_rate,
            distance_nm=getattr(aircraft, "_distance_nm", None),
        )

        if not aircraft.callsign:
            return enriched
        callsign = aircraft.callsign.strip()
        if not callsign:
            return enriched

        self._apply_callsign_airline(enriched, callsign)

        async with aiohttp.ClientSession() as session:
            fa_flight = await self._get_flight(session, callsign)
            if fa_flight:
                self._apply_flightaware_flight(enriched, fa_flight)

            if enriched.registration:
                owner = await self._get_owner(session, enriched.registration)
                if owner:
                    enriched.owner_name = owner.get("name")
                    enriched.owner_location = owner.get("location")

            if enriched.aircraft_type:
                # Only call the FA type endpoint if we don't have a custom
                # override — overrides are free and cover most common DCA traffic.
                override = get_aircraft_name(enriched.aircraft_type)
                if override:
                    enriched.aircraft_name = override
                else:
                    type_info = await self._get_type(session, enriched.aircraft_type)
                    if type_info:
                        enriched.aircraft_manufacturer = type_info.get("manufacturer")
                        enriched.aircraft_description = type_info.get("description")
                        enriched.aircraft_engine_count = type_info.get("engine_count")
                        mfg = type_info.get("manufacturer") or ""
                        typ = type_info.get("type") or ""
                        if mfg and typ:
                            enriched.aircraft_name = f"{mfg} {typ}".strip()

        # Rare flag: global rare types, then airport-specific rare list.
        if enriched.aircraft_type:
            if enriched.aircraft_type in RARE_TYPES:
                enriched.is_rare = True
                enriched.rare_reason = RARE_TYPES[enriched.aircraft_type]
            else:
                airport_rare = get_airport_rare(enriched.aircraft_type)
                if airport_rare:
                    enriched.is_rare = True
                    enriched.rare_reason = airport_rare

        # Custom livery overrides aircraft-type display in the renderer.
        enriched.livery_name = get_livery(enriched.registration)

        # Manufacture year (cached forever in registry.db once fetched).
        # Lookup is by ICAO 24-bit hex (always present), with registration
        # passed along for human-readable cache entries.
        enriched.year_built = await faa_registry.year_built(enriched.icao24, enriched.registration)

        # Sighting history
        history.record_sighting(enriched)
        count, first, last = history.get_stats(enriched.registration)
        enriched.seen_count = count
        enriched.first_seen_at = first
        enriched.last_seen_at = last

        return enriched

    async def _get_flight(self, session, callsign):
        now = _now()
        cached = self._flight_cache.get(callsign)
        if cached and cached[1] > now:
            return cached[0]
        if not self.api_key:
            return None
        url = f"{self.AEROAPI_BASE}/flights/{callsign}"
        try:
            async with session.get(url, headers={"x-apikey": self.api_key}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                cost_tracker.record("/flights/{ident}")
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            print(f"FA /flights/{callsign} failed: {e}")
            return None
        flights = data.get("flights", [])
        if not flights:
            return None
        in_air = [f for f in flights if f.get("actual_off") and not f.get("actual_on")]
        if in_air:
            chosen = max(in_air, key=lambda f: f.get("actual_off") or "")
        else:
            active = next((f for f in flights if 0 < (f.get("progress_percent") or 0) < 100), None)
            if active:
                chosen = active
            else:
                scheduled = [f for f in flights if f.get("scheduled_off")]
                if scheduled:
                    now_ts = datetime.now(timezone.utc).timestamp()
                    def _td(f):
                        dt = _parse_dt(f.get("scheduled_off"))
                        return abs(dt.timestamp() - now_ts) if dt else float("inf")
                    chosen = min(scheduled, key=_td)
                else:
                    chosen = flights[0]
        self._flight_cache[callsign] = (chosen, now + _FLIGHT_TTL)
        return chosen

    def _apply_flightaware_flight(self, enriched, fa):
        origin = fa.get("origin") or {}
        dest = fa.get("destination") or {}
        enriched.origin_airport = origin.get("code_icao") or origin.get("code")
        enriched.origin_city = origin.get("city")
        enriched.origin_iata = origin.get("code_iata")
        enriched.destination_airport = dest.get("code_icao") or dest.get("code")
        enriched.destination_city = dest.get("city")
        enriched.destination_iata = dest.get("code_iata")

        enriched.aircraft_type = fa.get("aircraft_type")
        enriched.registration = fa.get("registration")

        enriched.scheduled_departure = _parse_dt(fa.get("scheduled_off"))
        enriched.estimated_departure = _parse_dt(fa.get("estimated_off"))
        enriched.departure_time = _parse_dt(fa.get("actual_off"))
        enriched.scheduled_arrival = _parse_dt(fa.get("scheduled_on"))
        enriched.estimated_arrival = _parse_dt(fa.get("estimated_on"))
        enriched.arrival_time = _parse_dt(fa.get("actual_on"))

        if fa.get("departure_delay"):
            enriched.delay_minutes = int(fa["departure_delay"]) // 60
        elif fa.get("arrival_delay"):
            enriched.delay_minutes = int(fa["arrival_delay"]) // 60

        enriched.status = fa.get("status")
        enriched.progress_pct = fa.get("progress_percent")
        enriched.route = fa.get("route")
        enriched.diverted = bool(fa.get("diverted"))
        enriched.cancelled = bool(fa.get("cancelled"))

        op_icao = fa.get("operator")
        operating_airline_name = None
        if op_icao and op_icao in AIRLINE_CODES:
            name, iata = AIRLINE_CODES[op_icao]
            operating_airline_name = name
            enriched.airline = name
            enriched.airline_iata = iata
        elif not enriched.airline and op_icao:
            enriched.airline = op_icao
            operating_airline_name = op_icao
        if fa.get("operator_iata") and not enriched.airline_iata:
            enriched.airline_iata = fa["operator_iata"]
        if fa.get("ident_iata"):
            enriched.flight_number = fa["ident_iata"]

        codeshares_iata = fa.get("codeshares_iata") or []
        enriched.codeshares_iata = codeshares_iata
        mk_num, mk_airline = _pick_marketing_codeshare(codeshares_iata)
        if mk_num and mk_airline:
            if operating_airline_name and operating_airline_name != mk_airline:
                enriched.operated_by = operating_airline_name
            enriched.flight_number = mk_num
            enriched.airline = mk_airline
            enriched.airline_iata = mk_num[:2]

        enriched.gate_origin = fa.get("gate_origin")
        enriched.gate_destination = fa.get("gate_destination")
        enriched.terminal_origin = fa.get("terminal_origin")
        enriched.terminal_destination = fa.get("terminal_destination")
        enriched.filed_altitude_ft = fa.get("filed_altitude")
        enriched.filed_airspeed_kts = fa.get("filed_airspeed")
        enriched.scheduled_duration_seconds = fa.get("filed_ete")
        enriched.route_distance_nm = fa.get("route_distance")

    def _apply_callsign_airline(self, enriched, callsign):
        icao_prefix = callsign[:3] if len(callsign) >= 3 and callsign[:3].isalpha() else None
        if icao_prefix and icao_prefix in AIRLINE_CODES:
            name, iata = AIRLINE_CODES[icao_prefix]
            enriched.airline = name
            enriched.airline_iata = iata
            numeric = callsign[3:].lstrip("0") or callsign[3:]
            enriched.flight_number = f"{iata}{numeric}"

    async def _get_owner(self, session, reg):
        now = _now()
        cached = self._owner_cache.get(reg)
        if cached and cached[1] > now:
            return cached[0]
        if not self.api_key:
            return None
        url = f"{self.AEROAPI_BASE}/aircraft/{reg}/owner"
        try:
            async with session.get(url, headers={"x-apikey": self.api_key}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                cost_tracker.record("/aircraft/{reg}/owner")
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            print(f"FA owner for {reg} failed: {e}")
            return None
        owner = data.get("owner") or {}
        result = {"name": owner.get("name"), "location": owner.get("location")}
        self._owner_cache[reg] = (result, now + _OWNER_TTL)
        return result

    async def _get_type(self, session, icao_type):
        now = _now()
        cached = self._type_cache.get(icao_type)
        if cached and cached[1] > now:
            return cached[0]
        if not self.api_key:
            return None
        url = f"{self.AEROAPI_BASE}/aircraft/types/{icao_type}"
        try:
            async with session.get(url, headers={"x-apikey": self.api_key}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                cost_tracker.record("/aircraft/types/{type}")
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            print(f"FA type {icao_type} failed: {e}")
            return None
        self._type_cache[icao_type] = (data, now + _TYPE_TTL)
        return data

    async def paginate_airport_flights(
        self, airport_code: str, kind: str, start_iso: str,
        end_iso: Optional[str] = None,
        max_pages_per_call: int = 5, max_follow_ups: int = 12,
        inter_call_sleep_s: float = 4.0,
    ):
        """Generator yielding each flight dict from /airports/{code}/flights/{kind}.

        FA personal-tier reality:
          - `max_pages` ≤ 5 per single call (10 returns "quota limit" 429).
          - Each followed `links.next` is a separate billable call.
          - The /flights/{kind} endpoints have a tight per-minute budget on
            personal tier — empirically ~2-3 paginated calls/minute before 429.

        Strategy:
          - Sleep `inter_call_sleep_s` between calls so we don't trip the
            per-minute limit even during a full-day backfill (~10 calls).
          - On 429, sleep 30s once and retry — that's enough for the per-minute
            window to clear.
          - Bounded by `max_follow_ups` so we never loop unbounded.

        Cost profile:
          - Cold backfill (DB empty for today): ~10 calls × 4s = ~40s to fetch
            a full day's worth of flights. Recovers from 429 once if hit.
          - Steady-state incremental (start=latest-1h): 1 call, no waiting.
        """
        if not self.api_key:
            return
        code = airport_code if airport_code.startswith("K") else f"K{airport_code}"
        end_param = f"&end={end_iso}" if end_iso else ""
        next_path = (
            f"/airports/{code}/flights/{kind}"
            f"?start={start_iso}{end_param}&max_pages={max_pages_per_call}"
        )
        calls = 0
        max_calls = 1 + max_follow_ups
        async with aiohttp.ClientSession() as session:
            while next_path and calls < max_calls:
                url = f"{self.AEROAPI_BASE}{next_path}"
                data = None
                for attempt in range(2):  # 1 initial + 1 retry on 429
                    try:
                        async with session.get(
                            url,
                            headers={"x-apikey": self.api_key},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as resp:
                            cost_tracker.record(f"/airports/{{id}}/flights/{kind}")
                            if resp.status == 429:
                                if attempt == 0:
                                    print(f"FA {kind} for {code} hit 429, backing off 30s and retrying once...")
                                    await asyncio.sleep(30)
                                    continue
                                print(f"FA {kind} for {code} 429 after retry, giving up this cycle (have {calls} calls done)")
                                return
                            if resp.status != 200:
                                print(f"FA {kind} for {code} status={resp.status}")
                                return
                            data = await resp.json()
                            break
                    except Exception as e:
                        print(f"FA {kind} paginate error: {e}")
                        return
                if data is None:
                    return
                for f in data.get(kind, []):
                    yield f
                next_path = (data.get("links") or {}).get("next")
                calls += 1
                if next_path and inter_call_sleep_s > 0:
                    await asyncio.sleep(inter_call_sleep_s)

    async def get_airport_status(self, airport_code: str) -> Optional[dict]:
        """Returns {color, category, delay_minutes, reasons} from FA /delays.

        Cost-trimmed: only /delays (~$0.01/call). The previously-paired
        /flights/counts call (~$0.10/call) was removed — its `departed` and
        `enroute` fields aren't used anywhere in VestaSpotter anymore (footer
        uses personal counts from our sightings DB instead).
        """
        now = _now()
        cached = self._airport_status_cache.get(airport_code)
        if cached and cached[1] > now:
            return cached[0]
        if not self.api_key:
            return None
        code = airport_code if airport_code.startswith("K") else f"K{airport_code}"
        status: dict = {"color": None, "category": None, "delay_minutes": None, "reasons": []}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.AEROAPI_BASE}/airports/{code}/delays",
                    headers={"x-apikey": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    cost_tracker.record("/airports/{id}/delays")
                    if resp.status == 200:
                        d = await resp.json()
                        status["color"] = d.get("color")
                        status["category"] = d.get("category")
                        delay_secs = d.get("delay_secs") or 0
                        status["delay_minutes"] = int(delay_secs // 60)
                        status["reasons"] = d.get("reasons") or []
        except Exception as e:
            print(f"FA airport status for {airport_code} failed: {e}")
            return None

        self._airport_status_cache[airport_code] = (status, now + _AIRPORT_STATUS_TTL)
        return status


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
