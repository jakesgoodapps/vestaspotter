"""Background polling loop — poll OpenSky → enrich → render → push to Vestaboard.

Push policy (different from PlaneSpotter which had a hard 5-min throttle):

  - Push immediately when the overhead aircraft CHANGES (different icao24 from
    last push). This is the whole point — every new flap event matters.
  - Push immediately when going from "traffic" to "no traffic" or vice versa.
  - Heartbeat: also push every `heartbeat_interval` seconds so the airport
    status block stays fresh even if the same plane has been on screen.
  - Vestaboard cloud API has a ~15s soft rate limit, which is way more than
    enough for plane-pass cadence (planes don't overlap that fast).
"""
import asyncio
import time
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from . import airport_movements, board_state, daily_history, pause_state, potus_detector, potus_schedule, scheduled_profiles, settings_state, watch_list
from .config import settings
from .data_pipeline import to_aircraft_view, to_airport_view
from .enrichment import FlightEnricher
from .formatter import format_board, format_no_traffic_board, format_potus_confirmed_board, format_potus_imminent_board, render_ascii
from .models import EnrichedAircraft
from .tracker import AircraftTracker
from .vestaboard import VestaboardClient

# Single detector instance shared across the polling loop.
_detector = potus_detector.PotusDetector()


def get_detector():
    return _detector

_LOCAL_TZ = ZoneInfo(settings.local_timezone)


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def is_quiet_hours() -> bool:
    """True if current local time is within the configured quiet window.
    Window can wrap midnight (e.g., 23:00 → 07:00)."""
    now = datetime.now(_LOCAL_TZ).time()
    start = _parse_hhmm(settings.quiet_hours_start)
    end = _parse_hhmm(settings.quiet_hours_end)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class SpotterState:
    """Tiny mutable container for last-pushed state + current overhead."""
    current_overhead: Optional[EnrichedAircraft] = None
    last_pushed_icao24: Optional[str] = None
    last_pushed_no_traffic: bool = False
    last_push_ts: float = 0.0
    last_render_matrix: Optional[list[list[int]]] = None


async def start_polling_loop(
    state: SpotterState,
    tracker: AircraftTracker,
    enricher: FlightEnricher,
    board: VestaboardClient,
    interval_seconds: int = 60,
) -> None:
    print(
        f"VestaSpotter started — polling every {interval_seconds}s, "
        f"heartbeat every {settings.heartbeat_interval}s, "
        f"DRY_RUN={settings.dry_run}"
    )
    print(
        f"Watching: {settings.latitude}, {settings.longitude} "
        f"(facing {settings.orientation_deg}°, {settings.radius_nm}nm, "
        f"<{settings.max_altitude_ft}ft)"
    )

    was_quiet = False
    was_paused = False
    while True:
        try:
            if pause_state.is_paused():
                if not was_paused:
                    s = pause_state.status()
                    print(f"PAUSED until {s.get('resume_at')} — skipping all polls/pushes")
                    was_paused = True
            elif is_quiet_hours():
                if was_paused:
                    print("pause cleared (now in quiet hours)")
                    was_paused = False
                if not was_quiet:
                    print(f"entering quiet hours ({settings.quiet_hours_start}-{settings.quiet_hours_end} ET) — skipping polls until window ends")
                    was_quiet = True
            else:
                if was_paused or was_quiet:
                    label = "pause" if was_paused else "quiet hours"
                    print(f"{label} ended — resuming polls")
                    was_paused = was_quiet = False
                    # Clear last-pushed state so the first detection after wake-up
                    # is treated as new and pushes a fresh frame to the board.
                    state.last_pushed_icao24 = None
                    state.last_pushed_no_traffic = False
                await _tick(state, tracker, enricher, board)
        except Exception as e:
            print(f"polling tick failed: {e}")
        await asyncio.sleep(interval_seconds)


async def _tick(
    state: SpotterState,
    tracker: AircraftTracker,
    enricher: FlightEnricher,
    board: VestaboardClient,
) -> None:
    nearby = await tracker.get_nearby_aircraft()
    overhead = tracker.filter_overhead(nearby)

    # POTUS detector is opt-in (DC-only). When disabled it's a no-op so we
    # don't even pay the cost of running pattern matching on every tick.
    if settings.enable_potus_detector:
        det_state = _detector.update(nearby)
    else:
        det_state = None
    if det_state and det_state.state in ("CONFIRMED", "IMMINENT"):
        # Interrupt normal flight push — show POTUS board.
        # Only push when state changes, or every ~5 min to keep it fresh.
        marker = f"potus_{det_state.state}_{int(det_state.last_change_at)}"
        already_showing = getattr(state, "_potus_marker", None) == marker
        stale = (time.time() - state.last_push_ts) > 300 if state.last_push_ts else True
        if not already_showing or stale:
            await _push_potus_board(state, det_state, board)
            state._potus_marker = marker
        return

    # Clear POTUS marker if we left those states
    if hasattr(state, "_potus_marker"):
        delattr(state, "_potus_marker")

    if overhead:
        primary = overhead[0]
        is_predicted = getattr(primary, "_is_predicted", False)
        enriched = await enricher.enrich(primary)
        state.current_overhead = enriched

        # Push if this is a different aircraft from what's on the board,
        # OR if last push was the no-traffic screen,
        # OR if heartbeat interval has elapsed.
        is_new_aircraft = enriched.icao24 != state.last_pushed_icao24
        coming_from_no_traffic = state.last_pushed_no_traffic
        heartbeat_due = (time.time() - state.last_push_ts) >= settings.heartbeat_interval

        should_push = is_new_aircraft or coming_from_no_traffic or heartbeat_due

        # VIP path: if this tail is on the watch list, it bypasses both the
        # user's throttle setting AND the special-only filter. The whole point
        # of a watch list is "always alert me when this plane goes by."
        is_vip = watch_list.contains(enriched.registration)
        skip_reason = None

        if not is_vip:
            # Use effective_settings — a scheduled rule may override the manual setting
            user_cfg = scheduled_profiles.effective_settings()
            if should_push and user_cfg["filter_mode"] == "special":
                if not (enriched.is_rare or enriched.livery_name):
                    skip_reason = "filter=special, plane is ordinary"
                    should_push = False
            if should_push and user_cfg["refresh_rate"] != "every_flight":
                throttle = settings_state.throttle_seconds_for(user_cfg["refresh_rate"])
                since_last = time.time() - state.last_push_ts if state.last_push_ts else 1e9
                if since_last < throttle:
                    skip_reason = f"throttle={user_cfg['refresh_rate']}, {int(throttle - since_last)}s until next push allowed"
                    should_push = False

        if should_push:
            tag = "PREDICTED" if is_predicted else "current"
            vip_marker = " ⭐VIP" if is_vip else ""
            print(f"detected [{tag}]{vip_marker} {enriched.callsign} at {primary._distance_nm:.2f}nm, pushing...")
            await _push_aircraft(state, enriched, enricher, board)
        elif skip_reason:
            print(f"detected {enriched.callsign} but skipping push ({skip_reason})")
        else:
            print(
                f"same aircraft ({enriched.flight_number or enriched.callsign}), "
                f"no push (heartbeat in {int(settings.heartbeat_interval - (time.time() - state.last_push_ts))}s)"
            )
    else:
        # No traffic in view — leave whatever's on the board alone. Don't waste
        # flap on a "no traffic" screen; the last flight or the previous app's
        # content stays. When a new flight appears, the change-detection path
        # above will push it as usual.
        state.current_overhead = None
        print("no traffic in view — leaving board untouched")


async def _push_aircraft(
    state: SpotterState,
    enriched: EnrichedAircraft,
    enricher: FlightEnricher,
    board: VestaboardClient,
) -> None:
    view = to_aircraft_view(enriched)
    if view is None:
        print(f"skipping push — enrichment insufficient for {enriched.icao24}")
        return
    airport_status = await enricher.get_airport_status(settings.airport_code)
    airport_view = to_airport_view(settings.airport_code, airport_status)
    matrix = format_board(view, airport_view)
    await board.push(matrix)
    state.last_pushed_icao24 = enriched.icao24
    state.last_pushed_no_traffic = False
    state.last_push_ts = time.time()
    state.last_render_matrix = matrix
    board_state.save(matrix, enriched.icao24, False)
    print(
        f"pushed {view.airline_iata}{view.flight_number} "
        f"({view.origin_iata}>>{view.destination_iata}) "
        f"tail={view.tail_number}"
    )


async def start_potus_schedule_refresh_loop() -> None:
    """Keep the factba.se calendar cache warm. No-op when POTUS detector is
    disabled — saves a daily 2MB download for users outside DC."""
    if not settings.enable_potus_detector:
        print("POTUS schedule refresh skipped (detector disabled)")
        return
    print("POTUS schedule refresh loop starting")
    while True:
        try:
            await potus_schedule.ensure_fresh()
        except Exception as e:
            print(f"POTUS schedule refresh failed: {e}")
        await asyncio.sleep(potus_schedule.CACHE_TTL_SECONDS)


async def start_daily_snapshot_loop(airport_code: str) -> None:
    """Once per day shortly after local midnight, snapshot the day-that-just-ended.
    Also snapshots yesterday immediately on startup if missing — catches restarts."""
    print("daily snapshot loop starting")
    # Catch-up: snapshot yesterday if not already done
    try:
        from datetime import timedelta
        yesterday = (datetime.now(_LOCAL_TZ) - timedelta(days=1)).date().isoformat()
        snap = daily_history.snapshot_if_missing(yesterday, airport_code)
        if snap:
            print(f"daily snapshot wrote {yesterday}: {snap['total_flights']} flights")
    except Exception as e:
        print(f"startup daily snapshot failed: {e}")

    while True:
        # Sleep until 00:02 next local day, then snapshot yesterday
        now = datetime.now(_LOCAL_TZ)
        from datetime import timedelta
        tomorrow_2am = (now + timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0)
        wait_seconds = max(60, (tomorrow_2am - now).total_seconds())
        await asyncio.sleep(wait_seconds)
        try:
            yesterday = (datetime.now(_LOCAL_TZ) - timedelta(days=1)).date().isoformat()
            snap = daily_history.snapshot_if_missing(yesterday, airport_code)
            if snap:
                print(f"daily snapshot wrote {yesterday}: {snap['total_flights']} flights")
        except Exception as e:
            print(f"daily snapshot failed: {e}")


async def start_airport_ingest_loop(
    enricher: FlightEnricher,
    airport_code: str,
    interval_seconds: int,
    initial_delay_seconds: int = 30,
) -> None:
    """Background loop refreshing the local airport_movements DB. Runs forever.

    Initial delay protects against repeated container restarts hitting FA's
    per-minute rate-limit window with the same burst of paginated calls.
    """
    print(
        f"airport ingest scheduled for {airport_code} — first run in "
        f"{initial_delay_seconds}s, then every {interval_seconds}s"
    )
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            if is_quiet_hours():
                print(f"airport ingest skipped (quiet hours)")
            else:
                n_arr, n_dep = await airport_movements.ingest_today(enricher, airport_code)
                print(f"airport ingest {airport_code}: arrivals={n_arr} departures={n_dep}")
        except Exception as e:
            print(f"airport ingest failed: {e}")
        await asyncio.sleep(interval_seconds)


async def _push_potus_board(
    state: SpotterState,
    det_state: potus_detector.DetectorState,
    board: VestaboardClient,
) -> None:
    """Render and push the POTUS heads-up or imminent board.

    Two flavors depending on whether factba.se confirms a real POTUS trip:
      - Schedule-matched → patriotic POTUS HEADS UP / POTUS IMMINENT board
      - No schedule match → 'drill suspected' / 'helo leaving, false alarm' board.
        Also marks the detector to use shortened IMMINENT+POST so the board
        releases back to normal flight tracking in ~2min instead of 25min.
    """
    callsign = det_state.active_callsign or "(unknown)"
    sched = potus_schedule.lookup_nearby_movement()
    drill = sched is None  # no scheduled WH trip → probably routine patrol

    def _dep_lines(s):
        final = s.get("final_destination")
        immediate = s.get("destination")
        if final and immediate and final.upper() != immediate.upper():
            return (f"DEP TO {final}"[:22], f"VIA {immediate}"[:22])
        dest = final or immediate
        return (f"DEPARTING TO {dest}"[:22] if dest else "MOVEMENT", "")

    if det_state.state == "CONFIRMED":
        if drill:
            # Mark the detector so IMMINENT + POST use shorter timers
            _detector.state.is_drill_suspected = True
            matrix = format_potus_confirmed_board(
                title="WH HELO ACTIVITY",
                line2="NO POTUS TRIP SCHED",
                line3="LIKELY ROUTINE DRILL",
                footer="(PROBABLY NOT POTUS)",
            )
        else:
            if sched["kind"] == "departure":
                l2, l3_dest = _dep_lines(sched)
                line3 = l3_dest or f"WATCHING {callsign}"[:22]
            else:  # arrival
                l2 = "ARRIVING AT WH"
                line3 = f"WATCHING {callsign}"[:22]
            matrix = format_potus_confirmed_board(line2=l2, line3=line3)
    else:  # IMMINENT
        if drill or det_state.is_drill_suspected:
            matrix = format_potus_imminent_board(
                title="HELO HEADED OUT",
                line2="FALSE ALARM - DRILL?",
                line3="ENJOY THE QUIET",
                footer="(NOT POTUS AFTER ALL)",
            )
        else:
            if sched["kind"] == "departure":
                l2, l3_dest = _dep_lines(sched)
                line3 = l3_dest or "LOOK NOW"
            else:
                l2 = "ARRIVING AT WH"
                line3 = "LOOK NOW"
            matrix = format_potus_imminent_board(line2=l2, line3=line3)

    await board.push(matrix)
    state.last_push_ts = time.time()
    state.last_render_matrix = matrix
    board_state.save(matrix, None, False)
    mode = "drill" if drill or det_state.is_drill_suspected else "potus"
    sched_tag = f" sched={sched['kind']}→{sched.get('destination','?')}" if sched else ""
    print(f"pushed POTUS {det_state.state} board ({mode}, heli={callsign}{sched_tag})")


async def _push_no_traffic(
    state: SpotterState,
    enricher: FlightEnricher,
    board: VestaboardClient,
) -> None:
    airport_status = await enricher.get_airport_status(settings.airport_code)
    airport_view = to_airport_view(settings.airport_code, airport_status)
    matrix = format_no_traffic_board(airport_view)
    await board.push(matrix)
    state.last_pushed_icao24 = None
    state.last_pushed_no_traffic = True
    state.last_push_ts = time.time()
    state.last_render_matrix = matrix
    board_state.save(matrix, None, True)
    print("pushed no-traffic board")
