# VestaSpotter — architecture notes for contributors / AI assistants

> User-facing docs live in `README.md`. This file is a deeper reference for
> people (and AI assistants) working in the codebase.

## What this project is

A self-hosted FastAPI service that detects aircraft passing a configured
observer location (apartment window, house deck, backyard, rooftop, etc.),
enriches them with FlightAware metadata, and renders them onto a [Vestaboard](https://www.vestaboard.com/)
split-flap display via its Cloud Read/Write API. Plus a web dashboard for
remote control, statistics, history, and (optionally for DC users) a POTUS
movement detector.

## The board layout (6 rows × 22 cols)

```
🟦UA 1234    IAD -- DCA      <- airline tile + flight + route
DEP  842P // 848P 🟨+6M      <- scheduled // actual departure + delay tile
ARR  945P // 950P 🟨+5M      <- scheduled // actual arrival + delay tile
🟨N12345  2018  7X SEEN      <- crown (king tail) + tail + year + sightings
⬜⬜  BOEING 737-800  ⬜⬜      <- aircraft type, white bookends
🟩DCA 47 ARR / 35 DEP        <- status tile + personal arr/dep counts today
```

**Row 1 tile:** airline brand color (`airline_colors.py`).
**Rows 2-3 delay tiles:** green ≤0min, yellow 1-14min, red 15+ min.
**Row 4:** crown is yellow tile if this tail has the max sighting count in the DB.
**Row 5 bookends:** white for normal aircraft; yellow for rare types or known custom liveries.
**Row 6 tile:** mapped from FlightAware's airport-delay color.

## Data flow

```
OpenSky Network ──> Aircraft positions
        |
        v
AircraftTracker (tracker.py)
  • filter by distance, altitude, FOV
  • predictive lookahead: project trajectories forward N seconds
        |
        v
FlightEnricher (enrichment.py)
  • FlightAware /flights/{ident} — route, schedule, type
  • FlightAware /aircraft/{reg}/owner — owner info
  • FlightAware /aircraft/types/{type} — friendly type name (skipped if we have an override)
  • OpenSky metadata — year built
  • custom_data — liveries.yaml, airport_rare_{CODE}.yaml, aircraft_names.yaml
  • sightings_db — sighting count for this tail
        |
        v
data_pipeline.to_aircraft_view (data_pipeline.py)
  • EnrichedAircraft → AircraftView (board-shaped, lots of formatting + sentinel handling)
        |
        v
formatter.format_board (formatter.py)
  • AircraftView + AirportView → 6×22 list of int Vestaboard char codes
        |
        v
VestaboardClient.push (vestaboard.py)
  • POST https://rw.vestaboard.com/ with the matrix
```

## Module map

| File | Purpose |
|---|---|
| `main.py` | FastAPI app + lifespan + all HTTP endpoints |
| `config.py` | All env-driven settings (pydantic-settings) |
| `tracker.py` | OpenSky polling + overhead filter + predictive lookahead |
| `enrichment.py` | FlightAware API calls + caching + per-call cost tracking |
| `faa_registry.py` | Year-built lookup via OpenSky metadata (cached forever per icao24) |
| `data_pipeline.py` | EnrichedAircraft → AircraftView translation |
| `formatter.py` | All board-rendering functions (regular, no-traffic, POTUS variants) |
| `airline_colors.py` | IATA airline code → Vestaboard color code |
| `custom_data.py` | Loads liveries.yaml, airport_rare_{CODE}.yaml, aircraft_names.yaml |
| `sightings_db.py` | SQLite sightings + all the leaderboard / flow / helicopter queries |
| `cost_tracker.py` | SQLite log of every FA call + per-endpoint pricing → monthly estimate |
| `daily_history.py` | End-of-day snapshots into a separate SQLite table |
| `scheduled_profiles.py` | Time-of-day rules that override manual settings |
| `settings_state.py` | Manual user settings (refresh_rate, filter_mode) persisted to JSON |
| `pause_state.py` | Pause-with-auto-resume state persisted to JSON |
| `watch_list.py` | VIP tail whitelist (bypasses throttle + filter) persisted to JSON |
| `board_state.py` | Last-pushed frame persistence (survives container restart) |
| `airport_movements.py` | **DORMANT** — paginated FA arrivals/departures. Caused the $750 incident; preserved with a ⚠️ banner as a cautionary reference. Nothing imports it. |
| `potus_detector.py` | Helicopter orbital-pattern state machine (opt-in via ENABLE_POTUS_DETECTOR) |
| `potus_schedule.py` | factba.se POTUS calendar fetch + lookup (paired with potus_detector) |
| `templates/index.html` | Single-file dashboard with inline CSS + JS |
| `configure.py` | Interactive .env setup wizard |

## API endpoints

User-facing dashboard at `/` (full controls) and `/live` (read-only guest view).

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | HTML dashboard |
| GET | `/live` | Same dashboard, controls hidden (safe to share) |
| GET | `/api/status` | Everything-bundled status + current overhead + last push |
| GET | `/api/preview` | Last-pushed matrix (raw + ASCII) |
| GET | `/api/current` | Current overhead aircraft (full EnrichedAircraft JSON) |
| GET | `/api/nearby` | All aircraft in bbox (debug) |
| POST | `/push` | Force a push to the board (bypasses change detection) |
| GET | `/api/sightings` | Top tails leaderboard |
| GET | `/api/types` | Top aircraft types |
| GET | `/api/recent` | Recent activity feed |
| GET | `/api/heatmap` | Hourly counts today |
| GET | `/api/cost` | Monthly FA spend estimate |
| GET | `/api/history` | Recent daily snapshots |
| GET | `/api/helicopters` | Helicopter-specific stats |
| GET/POST | `/api/settings` | Get/update manual settings (refresh_rate, filter_mode) |
| GET/POST | `/api/profiles*` | List/add/remove scheduled rules |
| GET/POST | `/api/watchlist*` | List/add/remove VIP tails |
| GET | `/api/potus` | POTUS detector state (returns `{"enabled": false}` if disabled) |
| GET/POST | `/pause` | Pause polling+pushing for N hours |
| GET/POST | `/resume` | Resume immediately |

## Key design decisions

**Predictive detection.** We project each aircraft's current heading + speed
forward `PREDICT_SECONDS_AHEAD` seconds and check if the *projected* position
is in the FOV. If so, push NOW, before the plane is actually visible. With
Vestaboard's ~10s flap-settle time + ~5s push latency, you want the board
fully rendered by the time you look up at the sky. Tunable per install.

**No-traffic = no push.** When the FOV is empty, the board stays on whatever
was last shown. We never push a blank/"no traffic" frame — that just wastes
flap cycles.

**Push only on change + heartbeat.** Same plane staying in view = no re-push.
Heartbeat (every 10 min default) refreshes the same plane's frame so airport
status etc. stays current.

**Quiet hours = total skip.** No polling, no pushing, no FA calls, no
factba.se refresh. Saves ~50% of the daily OpenSky budget overnight.

**API cost is tracked per-endpoint.** `cost_tracker.py` instruments every
FA HTTP call so the dashboard shows live spend. Hardcoded pricing matches
FA's published rates and the May 2026 invoice this project was originally
built against. There's a `_AIRPORT_MOVEMENTS_INCIDENT_DOCS` reference in
the code explaining why paginated `/flights/arrivals` calls are off-limits.

**POTUS detector is geometric only.** Watches for any helicopter doing
circular laps around the configured `POTUS_ORBIT_CENTER_LAT/LON`, then
cross-references factba.se to distinguish real POTUS movements from routine
patrols. Type-agnostic — typically catches Park Police Bell 412s but doesn't
require any specific tail/callsign.

## Persistence layout

Every persistence module resolves its directory the same way:

```python
_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
```

So the on-disk location depends on how you're running the app:

| How you run it | Resolved data dir |
|---|---|
| `docker compose up` (default) | `/app/data` inside the container → `./data/` on host (per the compose volume mount) |
| Direct `python -m backend.main` with no env var | `backend/data/` (fallback) |
| Anywhere with `VESTASPOTTER_DATA_DIR=/wherever` set | `/wherever/` |

The directory is gitignored so you won't see it in a fresh clone — it's created on first write. Contents:

```
{data_dir}/
├── sightings.db              # every aircraft we've pushed (or attempted to)
├── registry.db               # icao24 → year_built cache
├── fa_usage.db               # one row per FA API call
├── daily_history.db          # end-of-day snapshots
├── pause_state.json          # current pause window
├── settings_state.json       # user's manual refresh_rate / filter_mode
├── scheduled_profiles.json   # time-based settings rules
├── watch_list.json           # VIP tail whitelist
├── last_render.json          # last-pushed matrix (for board preview restoration)
├── potus_schedule_cache.json # factba.se daily download (if POTUS enabled)
└── potus_schedule_meta.json
```

All databases are SQLite — single-file, zero-config, fast enough at this scale.

## Vestaboard character set caveats

The Vestaboard charset is `A-Z 0-9 ! @ # $ ( ) - + & = ; : ' " % , . / ? °` plus 9 colored tiles. **No `>`, `<`, `*`, no underscores.** Our renderer uses:
- `--` for route arrows (row 1)
- `//` for scheduled-vs-actual separator (rows 2-3)
- `/` for the ARR/DEP separator (row 6)

If you add new render templates, double-check every character against the supported set in `formatter.CHAR_MAP`.

## How to test locally without a real Vestaboard

```bash
# Visual test of all sample render scenarios in your terminal
python3 -m backend.render_test

# Or run the full app with DRY_RUN=true (the default for new installs)
# — no actual board pushes, just logs the rendered matrix.
docker compose up
```

## Common contribution tasks

**Add an airport-rare YAML** for a new airport: copy `airport_rare_DCA.yaml`,
rename it to `airport_rare_{CODE}.yaml`, edit the contents for what's
uncommon at that airport. The custom_data loader picks it up automatically
based on the configured `AIRPORT_CODE`.

**Add to liveries.yaml**: just add `N12345: "AIRLINE LIVERY NAME"`. Restart container.

**Add to aircraft_names.yaml**: same pattern — `ICAOTYPE: "FRIENDLY NAME"`.

**Add a new dashboard card**: define an API endpoint in `main.py`, add a
JS fetcher in `templates/index.html`'s script block, add the card HTML +
CSS. The existing cards are good copy-paste references.
