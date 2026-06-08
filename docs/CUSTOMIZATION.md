# Customizing VestaSpotter for your airport

VestaSpotter ships with sensible defaults for DCA (Washington National), but the cool stuff — flagging the *specific* rare planes at *your* airport, recognizing the heritage liveries you actually care about — depends on you populating a few YAML files. This guide walks you through what to add and where to find the data.

There are three customization files, all in `backend/`:

| File | What it does |
|---|---|
| `airport_rare_{CODE}.yaml` | Aircraft types that are notable AT YOUR AIRPORT. Trigger the 🟨 yellow-bookend "rare" callout on the board. |
| `liveries.yaml` | Specific tail numbers with special paint jobs. Trigger 🟨 yellow-bookend "LIVERY" callout. |
| `aircraft_names.yaml` | Short display names for ICAO type codes (overrides FlightAware's verbose names). |

Plus a global rare list baked into `enrichment.py` (`RARE_TYPES`) — A380, 747, 777, 787, A330/350, MD-11, Concorde — that fires anywhere. You only need to add types that are rare at YOUR airport but not globally rare.

---

## 1. `airport_rare_{CODE}.yaml` — what's rare HERE?

The file is selected automatically based on your `AIRPORT_CODE` env var. If you set `AIRPORT_CODE=JFK`, the app looks for `backend/airport_rare_JFK.yaml`. If the file doesn't exist, no airport-specific rare flagging happens — just the global rare list.

### Format

```yaml
# ICAO type code -> human label (shown on the board's row 5)
B752: "BOEING 757-200"
A332: "AIRBUS A330-200"
C17:  "MILITARY C-17"
```

Keys must be the **ICAO type code** (4-character, like `B752` for a 757-200). NOT the friendly name. ICAO codes are what FlightAware returns and what we store in the DB.

### How to figure out what's rare at your airport

**Step 1 — understand your airport's bread-and-butter traffic.** Anything that flies hourly is NOT rare. The rare list should flag aircraft that pass maybe a few times a day at most.

Quick research sources:
- **[Wikipedia airport page](https://en.wikipedia.org/wiki/List_of_busiest_airports_in_the_United_States)** — under "Top destinations" tells you main carriers and route mix
- **[planespotters.net airport overview](https://www.planespotters.net/airport/)** — shows recent sightings by type
- **[Flightradar24 stats](https://www.flightradar24.com/data/airports)** — top routes, common aircraft
- **Reddit `r/aviation` + airport-specific subs** (e.g., r/SeaTac, r/LosAngeles) — locals know what's unusual
- **YouTube plane-spotter channels** filming at your airport — comments often discuss rare visitors

**Step 2 — categorize.** The general buckets:

| Type | Typical at most US airports? | When to add to rare list |
|---|---|---|
| 737/A320 family narrowbody | YES, everywhere | Never add |
| 737 MAX, A320/321neo | YES at major airports | Don't add unless they're rare AT YOUR airport |
| Regional jets (CRJ, ERJ, A220) | YES at hubs | Don't add |
| 757 | Rare at small airports, common at JFK/LAX | Add at small/regional airports |
| 767 | Mostly cargo + some intl | Add unless you have heavy cargo nearby |
| Widebodies (787, 777, A330, A350) | ALREADY in global rare list | Don't duplicate |
| A380 | ALREADY in global rare list | Don't duplicate |
| Military (C-17, KC-135, etc.) | Rare almost everywhere | Add — these are always cool |
| VIP/Private (Gulfstream, Falcon) | COMMON at hub airports near corporate centers | Don't add if you're near DC/NYC/LAX |
| Air Force One (VC25) | RARE EVERYWHERE | Add |

**Step 3 — look up ICAO codes.** Once you know what to flag, look up the codes:
- **[ICAO Aircraft Type Designators](https://en.wikipedia.org/wiki/List_of_aircraft_type_designators)** — Wikipedia table of all of them
- **[FlightAware aircraft types](https://www.flightaware.com/resources/registration/)** — search by name
- Or just look at your dashboard's "Most-Seen Aircraft Types" — every code in your DB has its 4-letter designator visible

### Example: how DCA's file was built

DCA has a **perimeter rule** (most flights must be ≤ 1250 statute miles) AND short runways. This means:
- Almost all traffic is narrowbody (737, A320 family) or regional (CRJ, ERJ)
- 757s are notable — only United runs one daily from Denver
- Widebodies (767, A330) only show up as diversions or special charters
- Bell 412s are USPP helicopters around the White House

Result: see `backend/airport_rare_DCA.yaml`.

### Don't have the local knowledge yet?

Start with the template at `backend/airport_rare_TEMPLATE.yaml`. Most of it is commented out — uncomment the rows you want to enable, and **prune the rest based on what you observe over a week**. You'll see your "Most-Seen Aircraft Types" leaderboard fill in real fast. Anything in your top 5 is NOT rare. Anything that shows up <5 times in a month probably IS.

### Sharing back

If you build a good `airport_rare_{CODE}.yaml` for your airport, **please send a PR**. The goal is for every major US airport to have a community-maintained file shipped in the repo so future users at that airport get a working starter.

---

## 2. `liveries.yaml` — special paint jobs

This file maps **specific tail numbers** (not types) to human-readable livery names. When that exact aircraft flies past, the board shows the livery name with yellow bookends.

### Format

```yaml
# Tail number (no spaces or dashes) -> Livery name to display
N905NN: "AA ASTROJET RETRO"
N177DZ: "DL BREAST CANCER PINK"
N76502: "UA CALIFORNIA"
```

### Where to find tail numbers

Heritage and special-paint liveries are extensively documented. Best sources:

- **[planespotters.net livery galleries](https://www.planespotters.net/airline/)** — search for "{airline} liveries" and filter
- **[airlinerlist.com](https://www.airlinerlist.com/)** — exhaustive aircraft photo database
- **Wikipedia "{airline} fleet" pages** — most have a "Special liveries" subsection with tail numbers
  - [American Airlines fleet](https://en.wikipedia.org/wiki/American_Airlines_fleet#Heritage_liveries) — TWA, Air Cal, Piedmont, Reno Air, AA Astrojet
  - [Delta fleet](https://en.wikipedia.org/wiki/Delta_Air_Lines_fleet#Special_liveries) — 80 Years, Breast Cancer Pink, Spirit of Delta
  - [United fleet](https://en.wikipedia.org/wiki/United_Airlines_fleet#Special_liveries) — California, Friend Ship
  - [JetBlue fleet](https://en.wikipedia.org/wiki/JetBlue_fleet) — many themed (NYC, Boston, BluesMobile, etc.)
- **Reddit `r/aviation`** — search "{airline} liveries"
- **Local plane-spotter Discord servers** — usually have running lists of cool tails

### Tips

- **Use the registration WITHOUT dashes or spaces.** `N12345A` not `N12-345A`.
- **Capitalize the value.** Vestaboard text is uppercase anyway, but it keeps the file consistent.
- **Keep names short.** Row 5 has at most 18-20 chars of text room after the bookends. "LIVERY: AA ASTROJET" works. "LIVERY: AMERICAN AIRLINES ASTROJET HERITAGE FLEET PIECE" does not.
- **Watch your DB's leaderboard.** Once you've been running a few weeks, you'll notice the same tails keep flying by. Cross-reference any of those that LOOK cool against livery galleries.

---

## 3. `aircraft_names.yaml` — pretty type names

When FlightAware returns "Canadair Regional Jet" for a CRJ-700, that's 21 chars and overflows the board. We swap it for "BOMBARDIER CRJ-700" (18 chars) via this file.

### Format

```yaml
# ICAO type code -> display name (must fit row 5 with bookends, so ≤ 18 chars ideal)
CRJ7: "BOMBARDIER CRJ-700"
E75L: "EMBRAER 175"
```

### When to add an entry

- FA's verbose name overflows the board row → swap for a shorter one
- FA returns something cryptic (just the ICAO code, no friendly name) → add a friendly name
- You want a more recognizable common name (e.g., `BCS3` → `AIRBUS A220-300` — technically a Bombardier CSeries by ICAO designation, but Airbus has rebranded)

### What's already covered

Look at the file as it ships — common Boeing, Airbus, Embraer, Bombardier, and Gulfstream types are there. You probably only need to add entries for unusual types specific to your area.

---

## 4. Quick-start: building your customization in week 1

```
Day 1   — Set AIRPORT_CODE to your IATA. Copy airport_rare_TEMPLATE.yaml
          to airport_rare_{YOURCODE}.yaml. Leave it mostly empty.

Day 1-7 — Run VestaSpotter. Watch what shows up in your "Most-Seen Aircraft
          Types" leaderboard and "Recent Activity" feed.

Day 7+  — Anything in your top 5 types is NOT rare — leave it out of the rare file.
          Anything that appeared <5 times across the whole week PROBABLY is rare.
          Add those ICAO codes to your airport_rare YAML.

Ongoing — Each cool spot, check the tail vs. livery sources. If it's a known
          heritage livery, add it to liveries.yaml.
```

Restart the container (`docker compose up -d --build`) to pick up YAML changes.

---

## 5. Going further

**Sharing with the community.** If you build a good airport_rare YAML or accumulate a meaningful liveries list, please PR them back. Goals:
- Every major US airport has a curated `airport_rare_{CODE}.yaml` shipped in the repo
- `liveries.yaml` grows into a community-maintained livery database

**Adding airline brand colors.** If you frequently see an airline whose tile color is wrong (defaults to white), edit `backend/airline_colors.py`'s `AIRLINE_COLORS` dict. PR welcome.

**Adding rare types globally.** If a type is rare *everywhere* in the world (not just at one airport), add it to `RARE_TYPES` in `backend/enrichment.py`. PR welcome.
