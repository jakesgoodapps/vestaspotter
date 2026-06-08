#!/usr/bin/env python3
"""VestaSpotter interactive setup wizard.

Walks you through configuring the app for your apartment + window + airport,
then writes a `.env` file you can either commit to your container env or feed
to docker-compose directly.

Re-run any time to update individual settings — existing values become defaults.

  python3 configure.py
"""
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

# Common IANA timezones (offer a short menu; user can type any IANA string).
TIMEZONES = [
    ("1", "America/New_York", "Eastern (DC, NYC, ATL, MIA)"),
    ("2", "America/Chicago", "Central (ORD, DFW, IAH)"),
    ("3", "America/Denver", "Mountain (DEN, SLC)"),
    ("4", "America/Phoenix", "Arizona (PHX — no DST)"),
    ("5", "America/Los_Angeles", "Pacific (LAX, SFO, SEA)"),
    ("6", "Europe/London", "UK"),
    ("7", "Europe/Paris", "CET (Paris, Frankfurt, AMS)"),
    ("8", "Asia/Tokyo", "Japan"),
    ("9", "Australia/Sydney", "AEDT"),
]


def cyan(s): return f"\033[36m{s}\033[0m"
def green(s): return f"\033[32m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"
def dim(s): return f"\033[2m{s}\033[0m"
def bold(s): return f"\033[1m{s}\033[0m"


def prompt(label: str, default=None, validator=None, secret=False) -> str:
    """Prompt with a default value. Validator returns (ok, error_msg)."""
    default_disp = f" [{default}]" if default not in (None, "") else ""
    while True:
        raw = input(f"  {label}{dim(default_disp)}: ").strip()
        val = raw if raw else (str(default) if default is not None else "")
        if not val:
            print(yellow("    value required"))
            continue
        if validator:
            ok, msg = validator(val)
            if not ok:
                print(yellow(f"    {msg}"))
                continue
        return val


def prompt_bool(label: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"  {label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(yellow("    answer y or n"))


def read_existing_env() -> dict:
    if not ENV_PATH.exists():
        return {}
    out = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(values: dict) -> None:
    """Write .env preserving comments from .env.example structure."""
    if ENV_EXAMPLE.exists():
        template = ENV_EXAMPLE.read_text()
    else:
        template = ""
    # Rewrite each KEY= line in the template with our values; preserve
    # everything else (comments, blank lines).
    out_lines = []
    seen = set()
    for line in template.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            key = m.group(1)
            seen.add(key)
            out_lines.append(f"{key}={values[key]}")
        else:
            out_lines.append(line)
    # Append any values we have that weren't in the template
    extras = [(k, v) for k, v in values.items() if k not in seen]
    if extras:
        out_lines.append("")
        out_lines.append("# Additional config")
        for k, v in extras:
            out_lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out_lines) + "\n")


def geocode_address(address: str):
    """Look up lat/lon via OpenStreetMap Nominatim (free, no key)."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1}
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VestaSpotter-setup/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(yellow(f"    geocoding failed: {e}"))
        return None
    if not data:
        return None
    r = data[0]
    return float(r["lat"]), float(r["lon"]), r.get("display_name", "")


def validate_lat(v):
    try:
        f = float(v)
        if -90 <= f <= 90:
            return True, ""
        return False, "latitude must be -90 to 90"
    except ValueError:
        return False, "must be a number"


def validate_lon(v):
    try:
        f = float(v)
        if -180 <= f <= 180:
            return True, ""
        return False, "longitude must be -180 to 180"
    except ValueError:
        return False, "must be a number"


def validate_degree(v):
    try:
        f = float(v)
        if 0 <= f <= 360:
            return True, ""
        return False, "must be 0-360 degrees"
    except ValueError:
        return False, "must be a number"


def validate_airport(v):
    if re.fullmatch(r"[A-Za-z]{3,4}", v):
        return True, ""
    return False, "IATA (3 chars) or ICAO (4 chars) airport code"


def validate_hhmm(v):
    if re.fullmatch(r"\d{1,2}:\d{2}", v):
        h, m = map(int, v.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return True, ""
    return False, "format HH:MM (24-hour)"


def main():
    print()
    print(bold("VestaSpotter setup wizard"))
    print(dim("This will write a .env file in the project root."))
    print()

    existing = read_existing_env()
    if existing and not prompt_bool("Existing .env found. Update it (defaults are current values)?", True):
        print("Aborting.")
        return

    values = {}

    # ---- Vestaboard ----
    print(cyan("\n[1/7] Vestaboard"))
    print(dim("    Find your API key in the Vestaboard app: Settings → Developer."))
    values["VESTABOARD_API_KEY"] = prompt("Vestaboard Read/Write API key", existing.get("VESTABOARD_API_KEY"))
    values["DRY_RUN"] = "true" if prompt_bool(
        "Start in DRY_RUN mode (recommended — verify before pushing to board)?",
        existing.get("DRY_RUN", "true").lower() == "true",
    ) else "false"

    # ---- FlightAware ----
    print(cyan("\n[2/7] FlightAware"))
    print(dim("    Sign up at flightaware.com/commercial/aeroapi/ for a personal key."))
    print(dim("    $5/mo free credit covers a single-apartment install easily."))
    values["FLIGHTAWARE_API_KEY"] = prompt("FlightAware AeroAPI key", existing.get("FLIGHTAWARE_API_KEY"))

    # ---- OpenSky ----
    print(cyan("\n[3/7] OpenSky Network"))
    print(dim("    Free; OAuth registered tier gives 4000 calls/day vs 400 anonymous."))
    print(dim("    Register at opensky-network.org for client_id/secret."))
    if prompt_bool("Have OpenSky OAuth credentials?", bool(existing.get("OPENSKY_CLIENT_ID"))):
        values["OPENSKY_CLIENT_ID"] = prompt("OpenSky client_id", existing.get("OPENSKY_CLIENT_ID"))
        values["OPENSKY_CLIENT_SECRET"] = prompt("OpenSky client_secret", existing.get("OPENSKY_CLIENT_SECRET"))
        values["OPENSKY_USERNAME"] = ""
        values["OPENSKY_PASSWORD"] = ""
    else:
        print(dim("    Anonymous tier (~400 calls/day) will be used."))
        values["OPENSKY_CLIENT_ID"] = ""
        values["OPENSKY_CLIENT_SECRET"] = ""
        values["OPENSKY_USERNAME"] = existing.get("OPENSKY_USERNAME", "")
        values["OPENSKY_PASSWORD"] = existing.get("OPENSKY_PASSWORD", "")

    # ---- Observer location ----
    print(cyan("\n[4/7] Your location"))
    print(dim("    Coords of YOUR window. You can paste an address or enter lat/lon directly."))
    if existing.get("LATITUDE") and existing.get("LONGITUDE"):
        print(dim(f"    Current: {existing['LATITUDE']}, {existing['LONGITUDE']}"))
    use_addr = prompt_bool("Look up by street address (uses OpenStreetMap)?", not existing.get("LATITUDE"))
    if use_addr:
        addr = prompt("Street address (e.g. '1600 Pennsylvania Ave NW, Washington DC')", None)
        result = geocode_address(addr)
        if result:
            lat, lon, display = result
            print(green(f"    Found: {display}"))
            print(green(f"    Coords: {lat:.4f}, {lon:.4f}"))
            if prompt_bool("Use these coordinates?", True):
                values["LATITUDE"] = f"{lat:.4f}"
                values["LONGITUDE"] = f"{lon:.4f}"
            else:
                values["LATITUDE"] = prompt("Latitude", existing.get("LATITUDE"), validate_lat)
                values["LONGITUDE"] = prompt("Longitude", existing.get("LONGITUDE"), validate_lon)
        else:
            print(yellow("    couldn't geocode that address, falling back to manual entry"))
            values["LATITUDE"] = prompt("Latitude", existing.get("LATITUDE"), validate_lat)
            values["LONGITUDE"] = prompt("Longitude", existing.get("LONGITUDE"), validate_lon)
    else:
        values["LATITUDE"] = prompt("Latitude", existing.get("LATITUDE"), validate_lat)
        values["LONGITUDE"] = prompt("Longitude", existing.get("LONGITUDE"), validate_lon)

    # ---- Window orientation ----
    print(cyan("\n[5/7] Window orientation"))
    print(dim("    Compass bearing your window faces. Open a compass app on your phone,"))
    print(dim("    point it out the window, read the degrees. 0=N, 90=E, 180=S, 270=W."))
    values["ORIENTATION_DEG"] = prompt("Bearing (degrees)", existing.get("ORIENTATION_DEG", "180"), validate_degree)
    values["FIELD_OF_VIEW_DEG"] = prompt(
        "Field of view width (degrees, default 120 = roughly what you can see through one window)",
        existing.get("FIELD_OF_VIEW_DEG", "120"),
        validate_degree,
    )
    values["RADIUS_NM"] = prompt(
        "Detection radius in nautical miles (3-10 typical)",
        existing.get("RADIUS_NM", "5.0"),
    )
    values["MIN_ALTITUDE_FT"] = prompt(
        "Minimum altitude to consider (excludes ground/just-landed)",
        existing.get("MIN_ALTITUDE_FT", "200"),
    )
    values["MAX_ALTITUDE_FT"] = prompt(
        "Maximum altitude (excludes high cruisers)",
        existing.get("MAX_ALTITUDE_FT", "8000"),
    )

    # ---- Airport + timezone ----
    print(cyan("\n[6/7] Your airport + timezone"))
    values["AIRPORT_CODE"] = prompt(
        "Nearest IATA airport code (e.g. DCA, JFK, LAX)",
        existing.get("AIRPORT_CODE", "DCA"),
        validate_airport,
    ).upper()
    print(dim("    Timezone for 'today' math and quiet hours."))
    for num, tz, label in TIMEZONES:
        print(dim(f"      {num}) {tz} — {label}"))
    tz_in = prompt(
        "Timezone (number from list, or full IANA string)",
        existing.get("LOCAL_TIMEZONE", "America/New_York"),
    )
    if tz_in.isdigit():
        match = next((tz for n, tz, _ in TIMEZONES if n == tz_in), None)
        values["LOCAL_TIMEZONE"] = match or "America/New_York"
    else:
        values["LOCAL_TIMEZONE"] = tz_in

    # ---- POTUS detector + quiet hours ----
    print(cyan("\n[7/7] Optional features"))
    is_dc = values["AIRPORT_CODE"] in ("DCA", "IAD", "BWI")
    if is_dc:
        print(dim("    Looks like you're in the DC area. The POTUS detector watches for"))
        print(dim("    a Park Police helicopter doing laps over the White House before a"))
        print(dim("    presidential helicopter movement. Only useful within ~5mi of WH."))
    values["ENABLE_POTUS_DETECTOR"] = "true" if prompt_bool(
        "Enable POTUS detector?",
        existing.get("ENABLE_POTUS_DETECTOR", "false").lower() == "true",
    ) else "false"

    print(dim("    Quiet hours = window where polling/pushing pause (saves API budget overnight)."))
    values["QUIET_HOURS_START"] = prompt(
        "Quiet hours start (HH:MM, 24-hour local)",
        existing.get("QUIET_HOURS_START", "00:30"),
        validate_hhmm,
    )
    values["QUIET_HOURS_END"] = prompt(
        "Quiet hours end (HH:MM)",
        existing.get("QUIET_HOURS_END", "08:00"),
        validate_hhmm,
    )

    # Defaults for poll interval, predict, heartbeat — fine for most users
    values.setdefault("POLL_INTERVAL", existing.get("POLL_INTERVAL", "20"))
    values.setdefault("HEARTBEAT_INTERVAL", existing.get("HEARTBEAT_INTERVAL", "600"))
    values.setdefault("PREDICT_SECONDS_AHEAD", existing.get("PREDICT_SECONDS_AHEAD", "100"))

    # ---- Write it out ----
    print()
    if ENV_PATH.exists():
        backup = ENV_PATH.with_suffix(".env.bak")
        shutil.copy2(ENV_PATH, backup)
        print(dim(f"    backed up old .env to {backup.name}"))
    write_env(values)
    print(green(f"✓ Wrote {ENV_PATH}"))
    print()
    print(bold("Next steps:"))
    print(f"  1. Review {ENV_PATH.name} (the values you just entered)")
    print(f"  2. Start the app:  docker compose up -d --build")
    print(f"  3. Visit:          http://localhost:8011/  (or your reverse-proxied URL)")
    print(f"  4. With DRY_RUN=true, the board WON'T be touched. Verify the dashboard")
    print(f"     looks right, then flip DRY_RUN=false in {ENV_PATH.name} and restart.")
    print()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
