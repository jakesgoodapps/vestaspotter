"""Today's specials — which known liveries / curated special flights are
scheduled through the airport today, and which of them we've already seen.

Feeds the "TODAY AT DCA" checklist card in idle_cards.py.

How the list is built (cards mode only, see scheduler.start_idle_refresh_loop):

  1. Every tail in liveries.yaml → FA /flights/{tail}?ident_type=registration
     with a 2-day lookahead. $0.005 per tail, so ~16 tails ≈ 8¢ per sweep.
  2. Every flight in airport_specials_{CODE}.yaml (hand-curated flight
     numbers that are known to bring a rare type — e.g. the once-a-day 757)
     → FA /flights/{ident}. Same price.

  Because FA looks 2 days ahead, one sweep covers today AND tomorrow, so
  sweeps run every other day. A hard cap (MAX_LOOKUPS_PER_SWEEP) and a
  one-sweep-per-day guard make the worst case a few dozen cents.

  ⚠️  This deliberately does NOT use the paginated /airports/{id}/flights/*
  endpoints — see airport_movements.py for why ($750 incident).

State lives in data/daily_specials.json and survives restarts.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

import yaml

from . import custom_data
from .config import settings
from .models import EnrichedAircraft

_LOCAL_TZ = ZoneInfo(settings.local_timezone)
_HERE = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or _HERE / "data")
_STATE_PATH = _DATA_DIR / "daily_specials.json"
_SPECIAL_FLIGHTS_PATH = _HERE / f"airport_specials_{settings.airport_code.upper()}.yaml"
_lock = Lock()

MAX_LOOKUPS_PER_SWEEP = 40


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _read() -> dict:
    if not _STATE_PATH.exists():
        return {"items": [], "sweeps": {}, "covered_through": None, "fetched_at": None, "last_error": None}
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
        data.setdefault("items", [])
        data.setdefault("sweeps", {})
        data.setdefault("covered_through", None)
        data.setdefault("fetched_at", None)
        data.setdefault("last_error", None)
        return data
    except Exception:
        return {"items": [], "sweeps": {}, "covered_through": None, "fetched_at": None, "last_error": None}


def _write(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as f:
        json.dump(state, f)


def load_special_flights() -> dict[str, str]:
    """ident → label from airport_specials_{CODE}.yaml (may be empty)."""
    if not _SPECIAL_FLIGHTS_PATH.exists():
        return {}
    try:
        with _SPECIAL_FLIGHTS_PATH.open() as f:
            data = yaml.safe_load(f) or {}
        return {str(k).upper().replace(" ", ""): str(v) for k, v in data.items()}
    except Exception as e:
        print(f"daily-specials: failed to load {_SPECIAL_FLIGHTS_PATH.name}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _today() -> date:
    return datetime.now(_LOCAL_TZ).date()


def items_for(day: date) -> list[dict]:
    iso = day.isoformat()
    with _lock:
        items = [i for i in _read()["items"] if i.get("date") == iso]
    items.sort(key=lambda i: i.get("sched_local") or "")
    return items


def items_for_today() -> list[dict]:
    return items_for(_today())


def status() -> dict:
    with _lock:
        state = _read()
    today = _today().isoformat()
    todays = sorted((i for i in state["items"] if i.get("date") == today), key=lambda i: i.get("sched_local") or "")
    return {
        "enabled": settings.daily_specials_enabled,
        "fetched_at": state.get("fetched_at"),
        "covered_through": state.get("covered_through"),
        "last_error": state.get("last_error"),
        "livery_tails": len(custom_data.all_liveries()),
        "special_flights": len(load_special_flights()),
        "today": todays,
        "seen_today": sum(1 for i in todays if i.get("seen_at")),
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def _parse_fa_time(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_LOCAL_TZ)
    except Exception:
        return None


def _items_from_instances(instances: list[dict], label: str, kind: str, keep_dates: set[str]) -> list[dict]:
    """Turn FA flight instances into checklist items touching our airport."""
    code = settings.airport_code.upper()
    out = []
    for inst in instances:
        if inst.get("cancelled"):
            continue
        origin = ((inst.get("origin") or {}).get("code_iata") or "").upper()
        dest = ((inst.get("destination") or {}).get("code_iata") or "").upper()
        if origin == code:
            direction, other, when = "DEP", dest, _parse_fa_time(inst.get("scheduled_out") or inst.get("scheduled_off"))
        elif dest == code:
            direction, other, when = "ARR", origin, _parse_fa_time(inst.get("scheduled_in") or inst.get("scheduled_on"))
        else:
            continue
        if when is None or when.date().isoformat() not in keep_dates:
            continue
        reg = (inst.get("registration") or "").upper() or None
        ident = (inst.get("ident") or "").upper() or None
        out.append({
            "key": f"{reg or ident}:{when.date().isoformat()}:{direction}",
            "label": label,
            "kind": kind,
            "registration": reg,
            "ident": ident,
            "ident_iata": (inst.get("ident_iata") or "").upper() or None,
            "ident_icao": (inst.get("ident_icao") or "").upper() or None,
            "aircraft_type": (inst.get("aircraft_type") or "").upper() or None,
            "direction": direction,
            "other_airport": other or None,
            "sched_local": when.isoformat(),
            "date": when.date().isoformat(),
            "seen_at": None,
        })
    return out


async def ensure_fresh(enricher) -> bool:
    """Run the FA sweep if today isn't covered yet. Returns True if a sweep ran.

    Safe to call every few minutes — it's a no-op unless (a) the feature is
    on, (b) today is past `covered_through`, and (c) no sweep ran today yet.
    """
    if not settings.daily_specials_enabled or not getattr(enricher, "api_key", None):
        return False  # feature off, or no FA key (dry-run installs) — don't mark anything covered
    today = _today()
    today_iso = today.isoformat()
    with _lock:
        state = _read()
    covered = state.get("covered_through")
    if covered and covered >= today_iso:
        return False
    if state["sweeps"].get(today_iso, 0) >= 1:
        return False  # one attempt per day, even if it failed

    tails = custom_data.all_liveries()
    flights = load_special_flights()
    lookups = [("registration", t, name, "livery") for t, name in sorted(tails.items())]
    lookups += [(None, ident, label, "flight") for ident, label in sorted(flights.items())]
    if len(lookups) > MAX_LOOKUPS_PER_SWEEP:
        print(f"daily-specials: capping sweep at {MAX_LOOKUPS_PER_SWEEP} of {len(lookups)} lookups")
        lookups = lookups[:MAX_LOOKUPS_PER_SWEEP]

    tomorrow = today + timedelta(days=1)
    keep = {today_iso, tomorrow.isoformat()}
    start = today_iso
    end = (today + timedelta(days=2)).isoformat()

    # Mark the attempt BEFORE calling out so a crash mid-sweep can't loop.
    with _lock:
        state = _read()
        state["sweeps"][today_iso] = state["sweeps"].get(today_iso, 0) + 1
        _write(state)

    print(f"daily-specials: sweeping {len(lookups)} lookups for {today_iso}..{tomorrow.isoformat()}")
    new_items: list[dict] = []
    errors = 0
    for ident_type, ident, label, kind in lookups:
        try:
            instances = await enricher.get_flight_instances(ident, ident_type=ident_type, start=start, end=end)
        except Exception as e:
            errors += 1
            print(f"daily-specials: lookup {ident} failed: {e}")
            continue
        new_items.extend(_items_from_instances(instances, label, kind, keep))

    with _lock:
        state = _read()
        # Preserve seen_at + keep items for dates we didn't re-fetch (yesterday's history is harmless).
        old_by_key = {i["key"]: i for i in state["items"]}
        merged: dict[str, dict] = {}
        for i in state["items"]:
            # Keep history for dates we didn't re-fetch, and ALWAYS keep
            # hand-added items — they never come back from FA.
            if i.get("date") not in keep or i.get("kind") == "manual":
                merged[i["key"]] = i
        for i in new_items:
            prev = old_by_key.get(i["key"])
            if prev and prev.get("seen_at"):
                i["seen_at"] = prev["seen_at"]
            merged[i["key"]] = i
        # Trim history to the last week so the file stays tiny.
        cutoff = (today - timedelta(days=7)).isoformat()
        state["items"] = [i for i in merged.values() if (i.get("date") or "") >= cutoff]
        state["covered_through"] = tomorrow.isoformat()
        state["fetched_at"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = f"{errors} lookup(s) failed" if errors else None
        _write(state)

    todays = [i for i in new_items if i["date"] == today_iso]
    print(f"daily-specials: {len(todays)} special(s) today, {len(new_items) - len(todays)} tomorrow ({errors} errors)")
    return True


# ---------------------------------------------------------------------------
# Tick-off
# ---------------------------------------------------------------------------

def mark_seen(ac: EnrichedAircraft) -> Optional[dict]:
    """If this aircraft matches one of today's checklist items, tick it.
    Returns the item ticked (or None). Idempotent."""
    code = settings.airport_code.upper()
    reg = (ac.registration or "").upper()
    idents = {s.upper().strip() for s in (ac.flight_number, ac.callsign) if s}
    direction = "DEP" if (ac.origin_iata or "").upper() == code else "ARR"
    today = _today().isoformat()
    with _lock:
        state = _read()
        candidates = [
            i for i in state["items"]
            if i.get("date") == today and not i.get("seen_at") and (
                (reg and i.get("registration") == reg)
                or (idents & {i.get("ident"), i.get("ident_iata"), i.get("ident_icao")} - {None})
            )
        ]
        if not candidates:
            return None
        # Prefer the leg in the direction we actually observed.
        candidates.sort(key=lambda i: (i.get("direction") != direction, i.get("sched_local") or ""))
        item = candidates[0]
        item["seen_at"] = datetime.now(timezone.utc).isoformat()
        _write(state)
    print(f"daily-specials: ticked '{item['label']}' ({item['direction']} {item.get('ident') or item.get('registration')})")
    return item


def add_manual(label: str, direction: str, sched_local_hhmm: str, day: Optional[date] = None) -> dict:
    """Dashboard helper: add a one-off item ("C-17 ARR 2:30P") to today's list
    without any FA call. Never auto-ticked unless the tail is known."""
    day = day or _today()
    direction = direction.upper()
    if direction not in ("ARR", "DEP"):
        raise ValueError("direction must be ARR or DEP")
    h, m = sched_local_hhmm.split(":")
    when = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=_LOCAL_TZ)
    item = {
        "key": f"manual:{label.upper()}:{day.isoformat()}:{direction}:{when.strftime('%H%M')}",
        "label": label.strip()[:40],
        "kind": "manual",
        "registration": None,
        "ident": None,
        "ident_iata": None,
        "ident_icao": None,
        "aircraft_type": None,
        "direction": direction,
        "other_airport": None,
        "sched_local": when.isoformat(),
        "date": day.isoformat(),
        "seen_at": None,
    }
    with _lock:
        state = _read()
        state["items"] = [i for i in state["items"] if i["key"] != item["key"]] + [item]
        _write(state)
    return item


def remove_item(key: str) -> bool:
    with _lock:
        state = _read()
        before = len(state["items"])
        state["items"] = [i for i in state["items"] if i.get("key") != key]
        changed = len(state["items"]) != before
        if changed:
            _write(state)
    return changed


def toggle_seen(key: str) -> Optional[dict]:
    """Dashboard helper: manually tick / untick an item."""
    with _lock:
        state = _read()
        for i in state["items"]:
            if i.get("key") == key:
                i["seen_at"] = None if i.get("seen_at") else datetime.now(timezone.utc).isoformat()
                _write(state)
                return i
    return None
