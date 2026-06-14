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

from . import board_state, city_lookup, daily_history, followed_flight, pause_state, potus_detector, potus_schedule, scheduled_profiles, settings_state, turbulence, watch_list
from .config import settings
from .data_pipeline import to_aircraft_view, to_airport_view
from .enrichment import FlightEnricher
from .formatter import format_board, format_followed_flight_board, format_no_traffic_board, format_potus_confirmed_board, format_potus_imminent_board, render_ascii
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

    # Mode conflict: if a followed flight is currently OWNING THE BOARD, we
    # still enrich + record overhead sightings (stats DB stays accurate), but
    # we don't render anything for them — the followed-flight loop is
    # responsible for what shows on the Vestaboard.
    #
    # Note: a pre-queued PRE_FLIGHT flight outside the 3-hour render window
    # is "active" (refreshing in background) but is_active_for_render() is
    # False, so overhead rendering proceeds normally until the window opens.
    if followed_flight.is_active_for_render():
        if overhead:
            primary = overhead[0]
            try:
                enriched = await enricher.enrich(primary)
                state.current_overhead = enriched
            except Exception as e:
                print(f"followed-mode sighting enrich failed: {e}")
        else:
            state.current_overhead = None
        return

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


_FOLLOWED_SCAN_INTERVAL = 60  # seconds between queue scans


async def start_followed_flight_loop(
    state: SpotterState,
    enricher: FlightEnricher,
    board: VestaboardClient,
) -> None:
    """Background loop driving the followed-flight queue (v0.2.0).

    Scan-based: every _FOLLOWED_SCAN_INTERVAL seconds we
      1. Prune any flights that have finished POST_LANDED + cooldown
      2. For each queued flight, poll FA + position if its phase-cadence is
         overdue since last_poll_at
      3. Ask the queue which flight should currently OWN the board (priority
         resolver in followed_flight.get_active_for_render)
      4. Push that flight's board if EITHER it just got polled OR it differs
         from whoever was rendering last tick (handover case)

    When the queue empties (or only contains queued PRE_FLIGHT outside the
    3h render window), is_active_for_render() returns False and overhead
    rendering resumes on the next regular _tick().

    Does NOT respect quiet hours — if the user is actively tracking, they
    want the data. They can remove the flight manually if they want quiet.
    """
    print("followed-flight loop started (queue scanner)")
    from datetime import datetime, timezone

    while True:
        try:
            # 1. Drop completed flights
            removed = followed_flight.prune_idle()
            if removed > 0:
                print(f"followed-flight: pruned {removed} completed flight(s) from queue")

            # 2. Poll each queued flight whose cadence is overdue
            polled_ids: set[str] = set()
            for flight in followed_flight.list_all():
                phase = flight.derive_phase()
                cadence = followed_flight.POLL_CADENCE_SECONDS.get(phase.value, 300)
                last_poll = _parse_followed_iso(flight.last_poll_at)
                now = datetime.now(timezone.utc)
                overdue = (last_poll is None) or ((now - last_poll).total_seconds() >= cadence)
                if not overdue:
                    continue
                try:
                    await _poll_followed_flight(flight, enricher)
                    followed_flight.update_flight(flight)
                    polled_ids.add(flight.fa_flight_id)
                except Exception as e:
                    print(f"followed-flight: poll failed for {flight.fa_flight_id}: {e}")

            # 3. Pick who currently owns the board + push when needed
            active = followed_flight.get_active_for_render()
            last_render_id = getattr(state, "_followed_render_id", None)
            if active is not None:
                handover = active.fa_flight_id != last_render_id
                refreshed = active.fa_flight_id in polled_ids
                if handover or refreshed:
                    await _push_followed_flight(state, active, board)
                    state._followed_render_id = active.fa_flight_id
                    if handover:
                        print(
                            f"followed-flight: board now owned by "
                            f"{active.user_ident} '{active.label}' "
                            f"(phase={active.derive_phase().value})"
                        )
            else:
                if last_render_id is not None:
                    # All flights pruned or all back in pre-render window
                    print("followed-flight: no active renderer; overhead rendering resumes next tick")
                    state._followed_render_id = None

            await asyncio.sleep(_FOLLOWED_SCAN_INTERVAL)
        except Exception as e:
            print(f"followed-flight loop scan failed: {e}")
            await asyncio.sleep(_FOLLOWED_SCAN_INTERVAL)


def _parse_followed_iso(s: Optional[str]) -> Optional["datetime"]:
    """Minimal ISO parser for last_poll_at strings. Returns None on failure."""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


async def _poll_followed_flight(flight, enricher: FlightEnricher) -> None:
    """Refresh FA data + position + city + turbulence for the given flight (in place)."""
    instances = await enricher.get_flight_instances(flight.user_ident)
    if instances:
        matching = next(
            (i for i in instances if i.get("fa_flight_id") == flight.fa_flight_id),
            None,
        )
        if matching:
            followed_flight.apply_fa_refresh(flight, matching)

    phase = flight.derive_phase()
    needs_position = phase in (
        followed_flight.Phase.AIRBORNE,
        followed_flight.Phase.APPROACH,
    )
    if needs_position:
        position = await enricher.get_flight_position(flight.fa_flight_id)
        if position:
            followed_flight.apply_position(flight, position)
            if flight.last_latitude is not None and flight.last_longitude is not None:
                # City below — best-effort, ok if it fails
                try:
                    city = await city_lookup.lookup_city(
                        flight.last_latitude, flight.last_longitude
                    )
                    if city:
                        flight.last_city = city
                except Exception as e:
                    print(f"city lookup failed: {e}")
                # Turbulence at current pos + frontier tile color update
                try:
                    severity = await turbulence.get_turbulence_at(
                        flight.last_latitude,
                        flight.last_longitude,
                        flight.last_altitude_100s_ft,
                    )
                    flight.update_progress_tile(severity)
                    flight.current_weather_severity = severity.value
                except Exception as e:
                    print(f"turbulence lookup failed: {e}")


async def _push_followed_flight(
    state: SpotterState,
    flight,
    board: VestaboardClient,
) -> None:
    """Render the current-phase board for a followed flight and push it."""
    matrix = format_followed_flight_board(flight)
    await board.push(matrix)
    state.last_push_ts = time.time()
    state.last_render_matrix = matrix
    state.last_pushed_icao24 = None
    state.last_pushed_no_traffic = False
    board_state.save(matrix, None, False)
    phase = flight.derive_phase().value
    print(
        f"pushed followed-flight board "
        f"(phase={phase}, ident={flight.user_ident}, label='{flight.label}')"
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
