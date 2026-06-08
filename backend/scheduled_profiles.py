"""Time-of-day rules that auto-change refresh_rate + filter_mode.

E.g., "Morning rush 06:00-09:00: every_flight + all" makes the board lively
during your coffee, then "Workday 09:00-17:00: 10min + all" quiets it down
while you're heads-down at work.

When `effective_settings()` is called from the scheduler each tick, the first
matching rule wins; if no rule matches, fall through to the user's manual
settings_state defaults.

Stored as JSON list in data/scheduled_profiles.json.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from . import settings_state

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "scheduled_profiles.json"
_lock = Lock()
from .config import settings as _settings_cfg
_LOCAL_TZ = ZoneInfo(_settings_cfg.local_timezone)


def _read() -> list[dict]:
    if not _STATE_PATH.exists():
        return []
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def _write(rules: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as f:
        json.dump(rules, f, indent=2)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _hhmm_to_minutes(s: str) -> int:
    h, m = _parse_hhmm(s)
    return h * 60 + m


def _time_in_window(start: str, end: str, now_minutes: int) -> bool:
    s, e = _hhmm_to_minutes(start), _hhmm_to_minutes(end)
    if s <= e:
        return s <= now_minutes < e
    return now_minutes >= s or now_minutes < e


def list_rules() -> list[dict]:
    with _lock:
        return _read()


def add_rule(name: str, start: str, end: str, refresh_rate: str, filter_mode: str) -> dict:
    if refresh_rate not in settings_state.VALID_REFRESH_RATES:
        raise ValueError(f"invalid refresh_rate {refresh_rate!r}")
    if filter_mode not in settings_state.VALID_FILTER_MODES:
        raise ValueError(f"invalid filter_mode {filter_mode!r}")
    _parse_hhmm(start); _parse_hhmm(end)  # raises on bad format
    rule = {
        "id": f"rule_{int(datetime.now().timestamp())}",
        "name": name or "Untitled",
        "start": start,
        "end": end,
        "refresh_rate": refresh_rate,
        "filter_mode": filter_mode,
    }
    with _lock:
        rules = _read()
        rules.append(rule)
        _write(rules)
    return rule


def remove_rule(rule_id: str) -> bool:
    with _lock:
        rules = _read()
        new = [r for r in rules if r.get("id") != rule_id]
        if len(new) == len(rules):
            return False
        _write(new)
        return True


def effective_settings() -> dict:
    """Return the active settings right now, considering any matching rule."""
    rules = list_rules()
    now_local = datetime.now(_LOCAL_TZ)
    now_minutes = now_local.hour * 60 + now_local.minute
    for r in rules:
        try:
            if _time_in_window(r["start"], r["end"], now_minutes):
                return {
                    "refresh_rate": r["refresh_rate"],
                    "filter_mode": r["filter_mode"],
                    "source": "rule",
                    "rule_name": r.get("name"),
                    "rule_id": r.get("id"),
                }
        except Exception:
            continue
    base = settings_state.get_settings()
    return {
        "refresh_rate": base["refresh_rate"],
        "filter_mode": base["filter_mode"],
        "source": "manual",
        "rule_name": None,
        "rule_id": None,
    }
