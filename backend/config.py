from pathlib import Path

from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # ---- Observer location & view (REQUIRED for every install) ----
    # Defaults below are placeholders. Run `python setup.py` or set these
    # explicitly in .env for your apartment + window.
    latitude: float = 38.8521
    longitude: float = -77.0402
    orientation_deg: float = 180.0  # compass bearing your window faces (0=N, 90=E, 180=S, 270=W)
    radius_nm: float = 3.0          # detection radius from observer
    max_altitude_ft: float = 8000
    min_altitude_ft: float = 0       # exclude on-runway / just-touched-down planes
    field_of_view_deg: float = 120.0 # angular width of your window's view

    # Local timezone for all "today" / quiet-hours math. Set to whatever IANA
    # zone matches your observer location.
    local_timezone: str = "America/New_York"

    # OpenSky Network (OAuth2 client creds preferred, legacy basic supported)
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    opensky_username: str = ""
    opensky_password: str = ""

    # FlightAware AeroAPI
    flightaware_api_key: str = ""

    # Vestaboard Read/Write API
    vestaboard_api_key: str = ""
    dry_run: bool = True  # log rendered board instead of pushing

    # Polling cadence
    poll_interval: int = 60  # seconds between OpenSky polls

    # Predictive detection: project each aircraft's trajectory forward by this
    # many seconds. If the projected position falls inside our FOV, push to
    # the board NOW so it's mid-flap by the time the plane is actually visible.
    # 60s is roughly enough lead time for one full Vestaboard flap-settle cycle.
    predict_seconds_ahead: int = 60

    # Push policy. We push when the displayed flight CHANGES (icao24 differs from
    # last push) or when the airport-status block changes. We also do a heartbeat
    # push every `heartbeat_interval` seconds so the board reflects status drift.
    heartbeat_interval: int = 600  # 10 min — covers airport status refresh

    # Airport movements DB refresh — paginates FA flights/arrivals + /departures.
    # Each cycle ≈ 24 pages × 2 = ~48 FA calls at midday for DCA. 30min = ~2300/day.
    # If FA usage gets uncomfortable, dial this up.
    airport_ingest_interval: int = 1800  # 30 min

    # Quiet hours (local Eastern). During this window:
    #   - OpenSky polling skipped (saves ~1500 calls/night)
    #   - FA airport-movements ingest skipped (saves ~80 calls/night)
    #   - Vestaboard pushes skipped (board's own quiet hours stop the flap, but
    #     if we kept pushing the LAST stale push would be what flaps at wakeup)
    # Defaults match DCA's 10pm-7am noise curfew (basically zero commercial
    # traffic in that window). Format: "HH:MM" 24-hour local Eastern.
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"

    # Display
    airport_code: str = "DCA"

    # ---- POTUS detector (opt-in; DC-only) ----
    # When enabled, watches for a Park Police helicopter doing orbital laps
    # around the White House and cross-references factba.se's POTUS schedule.
    # Only meaningful if observer is near Washington DC. Default OFF for OSS.
    enable_potus_detector: bool = False
    potus_orbit_center_lat: float = 38.8977   # White House (default; configurable)
    potus_orbit_center_lon: float = -77.0365
    potus_schedule_feed_url: str = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"

    class Config:
        env_file = (_PROJECT_ROOT / ".env", ".env")
        env_file_encoding = "utf-8"


settings = Settings()
