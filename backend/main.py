"""VestaSpotter FastAPI app — detects overhead aircraft and pushes to Vestaboard."""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import board_state, cost_tracker, daily_history, faa_registry, pause_state, potus_schedule, scheduled_profiles, settings_state, sightings_db, watch_list
from .config import settings
from .data_pipeline import to_aircraft_view, to_airport_view
from .enrichment import FlightEnricher
from .formatter import format_board, format_no_traffic_board, render_ascii
from .opensky_auth import OpenSkyAuth
from .scheduler import (
    SpotterState,
    _push_aircraft,
    _push_no_traffic,
    get_detector,
    is_quiet_hours,
    start_daily_snapshot_loop,
    start_followed_flight_loop,
    start_polling_loop,
    start_potus_schedule_refresh_loop,
)
from .tracker import AircraftTracker
from .vestaboard import make_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vestaspotter")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sightings_db.init_db()
    faa_registry.init_db()
    cost_tracker.init_db()
    daily_history.init_db()

    opensky_auth = OpenSkyAuth(
        client_id=settings.opensky_client_id,
        client_secret=settings.opensky_client_secret,
        username=settings.opensky_username,
        password=settings.opensky_password,
    )
    tracker = AircraftTracker(
        latitude=settings.latitude,
        longitude=settings.longitude,
        orientation_deg=settings.orientation_deg,
        radius_nm=settings.radius_nm,
        max_altitude_ft=settings.max_altitude_ft,
        min_altitude_ft=settings.min_altitude_ft,
        field_of_view_deg=settings.field_of_view_deg,
        predict_seconds_ahead=settings.predict_seconds_ahead,
        opensky_auth=opensky_auth,
    )
    enricher = FlightEnricher(api_key=settings.flightaware_api_key)
    board = make_client()
    state = SpotterState()

    # Restore the last-pushed frame so the dashboard preview survives restarts.
    saved = board_state.load()
    if saved:
        state.last_render_matrix = saved.get("matrix")
        state.last_pushed_icao24 = saved.get("last_pushed_icao24")
        state.last_pushed_no_traffic = bool(saved.get("last_pushed_no_traffic"))
        log.info("restored last-pushed frame from disk (saved at %s)", saved.get("saved_at"))

    app.state.tracker = tracker
    app.state.enricher = enricher
    app.state.board = board
    app.state.spotter = state

    poll_task = asyncio.create_task(
        start_polling_loop(state, tracker, enricher, board, interval_seconds=settings.poll_interval)
    )
    snapshot_task = asyncio.create_task(
        start_daily_snapshot_loop(settings.airport_code)
    )
    potus_schedule_task = asyncio.create_task(
        start_potus_schedule_refresh_loop()
    )
    followed_flight_task = asyncio.create_task(
        start_followed_flight_loop(state, enricher, board)
    )
    yield
    for t in (poll_task, snapshot_task, potus_schedule_task, followed_flight_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="VestaSpotter", version="0.1.0", lifespan=lifespan)


_INDEX_HTML = (Path(__file__).resolve().parent / "templates" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the dashboard UI."""
    return _INDEX_HTML


@app.get("/live", response_class=HTMLResponse)
async def guest_view():
    """Same dashboard but stripped of controls (pause buttons, settings).
    Safe to share publicly so friends can see what's flying past Jake right now."""
    return _INDEX_HTML.replace("<body>", '<body class="guest">', 1)


@app.get("/api/recent")
async def api_recent(limit: int = 30):
    return {"sightings": sightings_db.recent_sightings(limit=limit)}


@app.get("/api/types")
async def api_types(limit: int = 15):
    from .custom_data import get_aircraft_name
    rows = sightings_db.top_aircraft_types(limit=limit)
    # Decorate with friendly names where we have an override
    return {
        "types": [
            {
                "type": r["aircraft_type"],
                "n": r["n"],
                "name": get_aircraft_name(r["aircraft_type"]) or r["aircraft_type"],
            }
            for r in rows
        ]
    }


@app.get("/api/heatmap")
async def api_heatmap():
    return {"hourly_counts": sightings_db.hourly_distribution_today()}


@app.get("/api/cost")
async def api_cost():
    return cost_tracker.month_summary()


@app.get("/api/helicopters")
async def api_helicopters():
    from .custom_data import get_aircraft_name
    raw = sightings_db.helicopter_stats()
    raw["top_types"] = [
        {**t, "name": get_aircraft_name(t["aircraft_type"]) or t["aircraft_type"]}
        for t in raw["top_types"]
    ]
    return raw


@app.get("/api/watchlist")
async def api_watchlist_get():
    return {"tails": watch_list.list_all()}


@app.post("/api/watchlist/add")
async def api_watchlist_add(tail: str, note: str = ""):
    try:
        added = watch_list.add(tail, note=note)
        return {"added": added}
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/watchlist/remove")
async def api_watchlist_remove(tail: str):
    return {"removed": watch_list.remove(tail)}


@app.get("/api/history")
async def api_history(limit: int = 14):
    return {"days": daily_history.list_recent(limit=limit)}


@app.get("/api/profiles")
async def api_profiles_get():
    return {
        "rules": scheduled_profiles.list_rules(),
        "active": scheduled_profiles.effective_settings(),
    }


@app.post("/api/profiles/add")
async def api_profiles_add(name: str, start: str, end: str, refresh_rate: str, filter_mode: str):
    try:
        return {"added": scheduled_profiles.add_rule(name, start, end, refresh_rate, filter_mode)}
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/profiles/remove")
async def api_profiles_remove(rule_id: str):
    return {"removed": scheduled_profiles.remove_rule(rule_id)}


@app.get("/api/potus")
async def api_potus():
    if not settings.enable_potus_detector:
        return {"enabled": False}
    status = get_detector().status_dict()
    status["enabled"] = True
    status["next_scheduled_movement"] = potus_schedule.lookup_nearby_movement()
    status["schedule_cache_age_s"] = potus_schedule.cache_age_seconds()
    return status


@app.get("/api/status")
async def api_status():
    """Everything the dashboard needs in one call — no FA calls triggered, just
    internal state + DB reads. Polled every 5s by the frontend."""
    state: SpotterState = app.state.spotter
    last_push_age = int(time.time() - state.last_push_ts) if state.last_push_ts else None

    current = None
    if state.current_overhead:
        o = state.current_overhead
        current = {
            "icao24": o.icao24,
            "callsign": o.callsign,
            "flight_number": o.flight_number,
            "airline_iata": o.airline_iata,
            "origin_iata": o.origin_iata,
            "destination_iata": o.destination_iata,
            "registration": o.registration,
            "aircraft_type": o.aircraft_type,
            "aircraft_name": o.aircraft_name,
            "year_built": o.year_built,
            "seen_count": o.seen_count,
        }

    leaderboard = sightings_db.top_seen(limit=15)

    return {
        "service": "VestaSpotter",
        "dry_run": settings.dry_run,
        "airport_code": settings.airport_code,
        "poll_interval": settings.poll_interval,
        "current_overhead": current,
        "last_pushed_icao24": state.last_pushed_icao24,
        "last_push_age_s": last_push_age,
        "board_matrix": state.last_render_matrix,
        "today": {
            "arrivals": sightings_db.count_seen_today(settings.airport_code, "arrival"),
            "departures": sightings_db.count_seen_today(settings.airport_code, "departure"),
        },
        "recent_flow": sightings_db.recent_flow_signal(settings.airport_code, n=3),
        "leaderboard": leaderboard,
        "pause": pause_state.status(),
        "quiet_hours_active": is_quiet_hours(),
        "user_settings": settings_state.get_settings(),
    }


@app.get("/api/settings")
async def api_get_settings():
    return settings_state.get_settings()


@app.post("/api/settings")
async def api_update_settings(refresh_rate: str | None = None, filter_mode: str | None = None):
    try:
        return settings_state.update_settings(refresh_rate=refresh_rate, filter_mode=filter_mode)
    except ValueError as e:
        return {"error": str(e)}


@app.get("/pause")
@app.post("/pause")
async def pause(hours: float = 2.0):
    """Pause all polls + pushes for N hours (default 2). Bookmark this URL on
    your phone for one-tap quiet mode. Examples:
        GET /pause          → 2 hours
        GET /pause?hours=4  → 4 hours
        GET /pause?hours=0.5 → 30 minutes
    """
    if hours <= 0 or hours > 24:
        return {"error": "hours must be between 0 and 24"}
    resume_at = pause_state.pause_for(hours)
    return {
        "paused": True,
        "for_hours": hours,
        "resume_at": resume_at.isoformat(),
        "message": f"VestaSpotter paused. Will resume at {resume_at.isoformat()}.",
    }


@app.get("/resume")
@app.post("/resume")
async def resume():
    """Clear any active pause and start polling/pushing again immediately."""
    pause_state.resume_now()
    return {"paused": False, "message": "VestaSpotter resumed."}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/preview")
async def preview():
    """Returns the most recently pushed board as both the raw matrix and ASCII."""
    state: SpotterState = app.state.spotter
    if state.last_render_matrix is None:
        return {"matrix": None, "ascii": None, "message": "no push yet"}
    return {
        "matrix": state.last_render_matrix,
        "ascii": render_ascii(state.last_render_matrix),
    }


@app.get("/current")
async def current_aircraft():
    """Returns the current overhead-aircraft enriched record (for debugging)."""
    tracker: AircraftTracker = app.state.tracker
    enricher: FlightEnricher = app.state.enricher
    nearby = await tracker.get_nearby_aircraft()
    overhead = tracker.filter_overhead(nearby)
    if not overhead:
        return {"aircraft": None, "nearby_count": len(nearby)}
    enriched = await enricher.enrich(overhead[0])
    return {"aircraft": enriched.model_dump(mode="json"), "nearby_count": len(nearby)}


@app.get("/nearby")
async def nearby_aircraft():
    tracker: AircraftTracker = app.state.tracker
    nearby = await tracker.get_nearby_aircraft()
    return {"count": len(nearby), "aircraft": [a.model_dump() for a in nearby]}


@app.post("/push")
async def force_push():
    """Force-render the current state and push (bypasses change detection)."""
    state: SpotterState = app.state.spotter
    tracker: AircraftTracker = app.state.tracker
    enricher: FlightEnricher = app.state.enricher
    board = app.state.board

    nearby = await tracker.get_nearby_aircraft()
    overhead = tracker.filter_overhead(nearby)
    if overhead:
        enriched = await enricher.enrich(overhead[0])
        await _push_aircraft(state, enriched, enricher, board)
        return {"pushed": True, "aircraft": enriched.callsign}
    else:
        return {"pushed": False, "aircraft": None, "reason": "no traffic in view, board left untouched"}


@app.get("/sightings")
async def sightings(limit: int = 20):
    """Top tails by sighting count."""
    return {"top": sightings_db.top_seen(limit=limit)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8011, reload=True)
