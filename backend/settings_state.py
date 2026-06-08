"""Persistent user settings for VestaSpotter board updates.

Two independent controls:
  - refresh_rate: throttle pushes ('every_flight' | '5min' | '10min' | '30min')
  - filter_mode: which flights flap the board ('all' | 'special')

Stored as JSON in data/settings_state.json. Survives container restarts.
"""
import json
import os
from pathlib import Path
from threading import Lock

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "settings_state.json"
_lock = Lock()

VALID_REFRESH_RATES = {"every_flight", "5min", "10min", "30min"}
VALID_FILTER_MODES = {"all", "special"}

THROTTLE_SECONDS = {
    "every_flight": 0,
    "5min": 300,
    "10min": 600,
    "30min": 1800,
}

DEFAULTS = {
    "refresh_rate": "every_flight",
    "filter_mode": "all",
}


def _read() -> dict:
    if not _STATE_PATH.exists():
        return dict(DEFAULTS)
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
    except Exception:
        return dict(DEFAULTS)
    return {
        "refresh_rate": data.get("refresh_rate") if data.get("refresh_rate") in VALID_REFRESH_RATES else DEFAULTS["refresh_rate"],
        "filter_mode": data.get("filter_mode") if data.get("filter_mode") in VALID_FILTER_MODES else DEFAULTS["filter_mode"],
    }


def _write(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as f:
        json.dump(state, f)


def get_settings() -> dict:
    with _lock:
        return _read()


def update_settings(refresh_rate: str | None = None, filter_mode: str | None = None) -> dict:
    with _lock:
        cur = _read()
        if refresh_rate is not None:
            if refresh_rate not in VALID_REFRESH_RATES:
                raise ValueError(f"invalid refresh_rate, must be one of {VALID_REFRESH_RATES}")
            cur["refresh_rate"] = refresh_rate
        if filter_mode is not None:
            if filter_mode not in VALID_FILTER_MODES:
                raise ValueError(f"invalid filter_mode, must be one of {VALID_FILTER_MODES}")
            cur["filter_mode"] = filter_mode
        _write(cur)
        return cur


def throttle_seconds_for(refresh_rate: str) -> int:
    return THROTTLE_SECONDS.get(refresh_rate, 0)
