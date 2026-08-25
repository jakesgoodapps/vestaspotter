"""Persistent user settings for VestaSpotter board updates.

Three independent controls:
  - board_mode: what the board does when nothing special is happening
      'live'  → classic spotter: every overhead flight flaps the board
                (subject to refresh_rate + filter_mode below). Demo mode.
      'cards' → idle deck: the board rests on a rotating deck of cards
                (today's specials checklist, yesterday's stats, fun facts…)
                and only rare types / liveries / watch-list tails interrupt.
                See idle_cards.py.
  - refresh_rate: throttle pushes ('every_flight' | '5min' | '10min' | '30min')
                  (live mode only)
  - filter_mode: which flights flap the board ('all' | 'special')
                 (live mode only — cards mode is implicitly special-only)

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
VALID_BOARD_MODES = {"live", "cards"}

THROTTLE_SECONDS = {
    "every_flight": 0,
    "5min": 300,
    "10min": 600,
    "30min": 1800,
}

DEFAULTS = {
    "board_mode": "live",
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
        "board_mode": data.get("board_mode") if data.get("board_mode") in VALID_BOARD_MODES else DEFAULTS["board_mode"],
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


def update_settings(
    refresh_rate: str | None = None,
    filter_mode: str | None = None,
    board_mode: str | None = None,
) -> dict:
    with _lock:
        cur = _read()
        if board_mode is not None:
            if board_mode not in VALID_BOARD_MODES:
                raise ValueError(f"invalid board_mode, must be one of {VALID_BOARD_MODES}")
            cur["board_mode"] = board_mode
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


def board_mode() -> str:
    """Current board mode ('live' | 'cards'). Not affected by scheduled
    profiles — those only cover refresh_rate / filter_mode."""
    return get_settings()["board_mode"]
