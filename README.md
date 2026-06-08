# VestaSpotter

> Watch real airplanes fly past your apartment on your real Vestaboard.

VestaSpotter is a self-hosted service that turns your [Vestaboard](https://www.vestaboard.com/) into a live split-flap aircraft tracker for the planes passing your window. Point it at your apartment's coordinates and your nearest airport, and the board flips to show you exactly what's overhead in real time — airline, flight number, route, aircraft type, tail number, sighting count, and a status footer.

It also includes a web dashboard for remote control + statistics, daily history, watch lists, custom-livery callouts, and (if you live near DC) a POTUS movement detector that catches the Park Police helicopter pattern preceding presidential helicopter departures.

## What it looks like

The board, rendering a real flight passing the window:

```
🟦UA 1234    IAD -- DCA
DEP  842P // 848P 🟨+6M
ARR  945P // 950P 🟨+5M
🟨N12345  2018  7X SEEN
⬜⬜  BOEING 737-800  ⬜⬜
🟩DCA 47 ARR / 35 DEP
```

- 🟦 = airline brand tile (United blue, American red, Southwest orange, etc.)
- DEP/ARR rows: scheduled // actual times + delay tile (green ≤ 0min, yellow 1-14, red 15+)
- Yellow tile next to tail = "king of the hill" (most-seen tail in your DB)
- Row 5: white tile bookends for normal aircraft, yellow for rare types or custom liveries
- Row 6: green/yellow/red status tile based on FlightAware airport-delay color + your personal arr/dep counts for today

## Features

**Live tracking**
- Predictive detection — projects trajectories forward so the board is *already flapping* by the time the plane enters your view
- Configurable view geometry (orientation, FOV, radius, altitude bounds)
- Auto-detects runway flow from the ratio of your recent arrivals vs departures
- Skips polling + pushing during configurable quiet hours (overnight API budget saver)

**Dashboard** (`/`)
- Live board preview
- Today's stats: planes seen, ARR/DEP split, runway flow indicator
- Hourly heatmap, daily history
- King-of-the-hill leaderboard, aircraft type leaderboard
- Helicopter activity tracking
- Recent activity feed
- FlightAware spend monitor (so you don't get a surprise bill)
- Pause controls, watch list, scheduled mode profiles
- Public guest view at `/live` (controls + costs hidden — safe to share)

**Customization** (drop YAML files, no code required)
- `liveries.yaml` — tail numbers → custom livery names (Astrojet retro, breast cancer pink, etc.)
- `airport_rare_{CODE}.yaml` — aircraft types that are rare *at your specific airport*
- `aircraft_names.yaml` — short display names for ICAO type codes

📖 **See [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md)** for a complete guide on what to put in these files, where to find livery data, and how to research what's rare at YOUR specific airport. A starter template ships at `backend/airport_rare_TEMPLATE.yaml`.

**Optional: POTUS detector** (DC-area users)
- Detects when Park Police helicopters do orbital patrol patterns over the White House
- Cross-references factba.se's POTUS schedule to differentiate real movements from routine drills
- Pre-empts normal flight display with a red-white-blue alert board

## Hardware you'll need

1. **A Vestaboard** with a Vestaboard+ subscription (for the Cloud Read/Write API key)
2. **Something to run the app on** — a Mac Mini, Raspberry Pi, NUC, $5 VPS, etc. The app is lightweight (~50MB RAM)
3. Internet access for the Vestaboard cloud, FlightAware AeroAPI, and OpenSky Network

Optional but recommended: a separate domain or subdomain pointed at your server for the dashboard.

## API accounts you'll need

| Service | Cost | Used for |
|---|---|---|
| [Vestaboard](https://www.vestaboard.com/) | Hardware + Vestaboard+ sub | The board itself + Cloud R/W API key |
| [FlightAware AeroAPI](https://flightaware.com/commercial/aeroapi/) | Free tier ($5/mo credit), then pay-as-you-go | Flight enrichment (route, aircraft type, tail, delay) |
| [OpenSky Network](https://opensky-network.org/) | Free | Real-time aircraft positions |

Expected FA cost on default settings: **~$10-15/month** for a single-apartment install. The dashboard's built-in cost monitor will warn you if you trend higher.

## Quick start (10 min)

### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/jakesgoodapps/vestaspotter
cd vestaspotter

# Run the interactive setup wizard — writes .env for you
python3 configure.py

# Build + start
docker compose up -d --build

# Visit the dashboard
open http://localhost:8011/
```

Leave `DRY_RUN=true` for the first session. Watch the dashboard's board preview match what you'd expect from looking out your window. When confident, flip `DRY_RUN=false` in `.env` and restart.

### Option B — Mac Mini / Pi running 24/7

Same as Option A, plus put it behind a reverse proxy of your choice (Traefik, nginx, Caddy) if you want a public URL for the dashboard. Pin your container as a systemd / launchd service if your platform doesn't restart Docker on boot.

If you're running on a server with an existing Docker reverse-proxy network, copy `docker-compose.override.example.yml` to `docker-compose.override.yml` (gitignored) and adjust the networks/binding to match your setup — Docker Compose will merge it with the base file automatically.

### Option C — $5 VPS (Fly.io, Railway, Render)

VestaSpotter is a single FastAPI container — drops into any Docker-hosting platform. The `data/` directory should be a persistent volume (~10MB). No DB required.

## Configuration reference

See `.env.example` for the full annotated list. Key vars:

| Var | What |
|---|---|
| `VESTABOARD_API_KEY` | From the Vestaboard app → Settings → Developer |
| `DRY_RUN` | `true` = log renders without touching the physical board. Use this first. |
| `FLIGHTAWARE_API_KEY` | Personal-tier AeroAPI key |
| `OPENSKY_CLIENT_ID/SECRET` | Optional but recommended — 10× the rate limit |
| `LATITUDE` / `LONGITUDE` | YOUR window's coordinates |
| `ORIENTATION_DEG` | Compass bearing your window faces (0=N, 90=E, 180=S, 270=W) |
| `FIELD_OF_VIEW_DEG` | Angular width of your window's view (120° default) |
| `RADIUS_NM` / `MAX_ALTITUDE_FT` / `MIN_ALTITUDE_FT` | Detection bounds |
| `LOCAL_TIMEZONE` | IANA name for "today" math (e.g., `America/New_York`) |
| `AIRPORT_CODE` | Nearest IATA airport for arr/dep classification + footer |
| `PREDICT_SECONDS_AHEAD` | Trajectory lookahead. Higher = more lead time, more false positives. 100 is a good default. |
| `ENABLE_POTUS_DETECTOR` | DC-only. Default `false`. |
| `QUIET_HOURS_START/END` | Skip everything during this window (HH:MM local) |

## Architecture

```
OpenSky Network ──> Aircraft positions
        |
        v
AircraftTracker ──> filter to overhead (distance, altitude, FOV, predictive lookahead)
        |
        v
FlightEnricher ──> FlightAware enrichment (route, schedule, type, owner)
                     + OpenSky metadata (year built)
                     + custom_data (liveries.yaml, airport_rare_{code}.yaml, aircraft_names.yaml)
                     + sightings_db (sighting count for this tail)
        |
        v
data_pipeline ──> EnrichedAircraft → AircraftView (board-shaped)
        |
        v
formatter ──> 6×22 int matrix (Vestaboard char codes)
        |
        v
VestaboardClient (Cloud) ──> POST https://rw.vestaboard.com/
```

All state persists in `data/` as SQLite databases + small JSON files. Wipe `data/` to reset everything except your `.env`.

## Contributing

PRs welcome. Especially:
- **New `airport_rare_{CODE}.yaml` files** for airports beyond DCA — see [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) for the format + sourcing guide. Goal: every major US airport ships with a community-maintained file.
- **Additions to `liveries.yaml`** for cool tail-specific paint jobs (heritage liveries, breast cancer pink, etc.)
- **Additions to `airline_colors.py`** for international or regional carriers whose tile color is wrong
- **Translations** to other languages (UI text is short — would be fun)
- **Bug reports + feature ideas** in GitHub Issues

By submitting a contribution you agree it can be distributed under the project's PolyForm Noncommercial license (and re-licensed by the maintainer under commercial terms for paid installs).

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). See `LICENSE`.

Free for personal use, hobby projects, education, research, and nonprofits — install it on your own Vestaboard at home, in your hackerspace, in a school, etc.

**Commercial use requires a separate license.** If you're a hotel, restaurant, aviation venue, museum, airline lounge, real estate office, or any other for-profit deployment, please reach out: **jake@jakesgoodapps.com**. Happy to license it (often cheaply) for venue installs, with optional support / custom branding / multi-board sync on top.

## Acknowledgments

- [Vestaboard](https://www.vestaboard.com/) — the physical board hardware
- [FlightAware AeroAPI](https://flightaware.com/commercial/aeroapi/) — flight enrichment
- [OpenSky Network](https://opensky-network.org/) — aircraft positions
- [factba.se](https://rollcall.com/factbase/) — POTUS schedule data
- [hexdb.io](https://hexdb.io/) — aircraft registry (we now use OpenSky metadata directly, but hexdb was the first attempt)

---

Built and tested against DCA. If you build a cool airport-specific YAML or livery list for your own area, send a PR!
