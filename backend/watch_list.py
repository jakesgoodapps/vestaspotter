"""Watch list of VIP tail numbers — they bypass the user's throttle + filter.

Stored as JSON in data/watch_list.json. Keys are uppercase tail numbers
(with hyphens/spaces stripped) so lookups are uniform regardless of input format.

When the scheduler sees a watch-listed tail overhead, it pushes immediately
regardless of refresh_rate or filter_mode. Use it for "always tell me when
this specific plane goes by" — Air Force One regulars, custom liveries you
love, that one specific 757 that does the once-a-day DCA run, etc.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "watch_list.json"
_lock = Lock()


def _normalize(tail: str) -> str:
    return tail.upper().replace("-", "").replace(" ", "").strip()


def _read() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
        # Defensive: dict only
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _write(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as f:
        json.dump(state, f, indent=2)


def contains(tail: Optional[str]) -> bool:
    if not tail:
        return False
    with _lock:
        return _normalize(tail) in _read()


def add(tail: str, note: str = "") -> dict:
    key = _normalize(tail)
    if not key:
        raise ValueError("empty tail")
    with _lock:
        state = _read()
        state[key] = {
            "tail": key,
            "note": note,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        _write(state)
        return state[key]


def remove(tail: str) -> bool:
    key = _normalize(tail)
    with _lock:
        state = _read()
        if key not in state:
            return False
        del state[key]
        _write(state)
        return True


def list_all() -> list[dict]:
    """Return all watch-listed tails, newest-first."""
    with _lock:
        state = _read()
    items = list(state.values())
    items.sort(key=lambda r: r.get("added_at", ""), reverse=True)
    return items
