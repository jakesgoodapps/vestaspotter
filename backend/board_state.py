"""Persistent snapshot of the last rendered board frame.

Saved as JSON in data/ on each successful push. Loaded on container startup
into SpotterState so the dashboard preview survives restarts — otherwise
every restart blanks the "Board Preview" card until the next live flap.

Tiny file (~150 bytes), one write per push (which is at most every few minutes).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "last_render.json"
_lock = Lock()


def save(
    matrix: list[list[int]],
    last_pushed_icao24: Optional[str],
    last_pushed_no_traffic: bool,
    kind: str = "aircraft",
) -> None:
    """Persist the most recently pushed frame + the metadata needed to avoid
    double-pushing on restart. `kind` is what the frame IS ('aircraft',
    'card', 'potus', 'followed') so cards mode knows whether the deck is
    already showing after a container restart."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "matrix": matrix,
        "last_pushed_icao24": last_pushed_icao24,
        "last_pushed_no_traffic": last_pushed_no_traffic,
        "kind": kind,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock, _STATE_PATH.open("w") as f:
        json.dump(payload, f)


def load() -> Optional[dict]:
    """Load the saved frame, if any. Returns None on first-ever start."""
    if not _STATE_PATH.exists():
        return None
    try:
        with _lock, _STATE_PATH.open() as f:
            return json.load(f)
    except Exception:
        return None
