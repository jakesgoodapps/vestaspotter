"""Persistent pause state for VestaSpotter pushes.

Stored as a tiny JSON file next to the SQLite DBs. Survives container restarts.
Either an absolute resume time (timed pause) or null (not paused).

The scheduler checks `is_paused()` at the top of each tick — when True, the
poll, the airport ingest, and the Vestaboard push are all skipped. Quiet, free.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "pause_state.json"
_lock = Lock()


def _read() -> Optional[datetime]:
    if not _STATE_PATH.exists():
        return None
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
    except Exception:
        return None
    s = data.get("resume_at")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _write(resume_at: Optional[datetime]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"resume_at": resume_at.isoformat() if resume_at else None}
    with _STATE_PATH.open("w") as f:
        json.dump(payload, f)


def pause_for(hours: float) -> datetime:
    """Pause until now + hours. Returns the resume timestamp (UTC)."""
    resume_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    with _lock:
        _write(resume_at)
    return resume_at


def resume_now() -> None:
    """Clear any active pause immediately."""
    with _lock:
        _write(None)


def is_paused() -> bool:
    """True if pause is currently active. Auto-clears expired pauses."""
    with _lock:
        resume_at = _read()
        if not resume_at:
            return False
        if datetime.now(timezone.utc) >= resume_at:
            _write(None)
            return False
        return True


def status() -> dict:
    """Return current pause state for status/debug endpoints."""
    with _lock:
        resume_at = _read()
    if not resume_at:
        return {"paused": False, "resume_at": None, "remaining_seconds": None}
    now = datetime.now(timezone.utc)
    if now >= resume_at:
        return {"paused": False, "resume_at": None, "remaining_seconds": None}
    return {
        "paused": True,
        "resume_at": resume_at.isoformat(),
        "remaining_seconds": int((resume_at - now).total_seconds()),
    }
