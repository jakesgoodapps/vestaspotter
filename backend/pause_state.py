"""Persistent pause state for VestaSpotter pushes.

Stored as a tiny JSON file next to the SQLite DBs. Survives container restarts.
Three shapes:
  {"resume_at": "<iso>"}                      timed pause, auto-clears
  {"resume_at": null, "indefinite": true}     OFF until someone hits /resume
  {"resume_at": null}                         not paused

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


def _read() -> tuple[Optional[datetime], bool]:
    """Returns (resume_at, indefinite)."""
    if not _STATE_PATH.exists():
        return None, False
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
    except Exception:
        return None, False
    if data.get("indefinite"):
        return None, True
    s = data.get("resume_at")
    if not s:
        return None, False
    try:
        return datetime.fromisoformat(s), False
    except Exception:
        return None, False


def _write(resume_at: Optional[datetime], indefinite: bool = False) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"resume_at": resume_at.isoformat() if resume_at else None}
    if indefinite:
        payload["indefinite"] = True
    with _STATE_PATH.open("w") as f:
        json.dump(payload, f)


def pause_for(hours: float) -> datetime:
    """Pause until now + hours. Returns the resume timestamp (UTC)."""
    resume_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    with _lock:
        _write(resume_at)
    return resume_at


def pause_indefinitely() -> None:
    """Turn the whole thing off — no polls, no pushes, no FA calls — until
    someone explicitly calls resume_now(). Vacation mode. Survives restarts."""
    with _lock:
        _write(None, indefinite=True)


def resume_now() -> None:
    """Clear any active pause (timed or indefinite) immediately."""
    with _lock:
        _write(None)


def is_paused() -> bool:
    """True if pause is currently active. Auto-clears expired timed pauses;
    an indefinite pause never auto-clears."""
    with _lock:
        resume_at, indefinite = _read()
        if indefinite:
            return True
        if not resume_at:
            return False
        if datetime.now(timezone.utc) >= resume_at:
            _write(None)
            return False
        return True


_NOT_PAUSED = {"paused": False, "indefinite": False, "resume_at": None, "remaining_seconds": None}


def status() -> dict:
    """Return current pause state for status/debug endpoints."""
    with _lock:
        resume_at, indefinite = _read()
    if indefinite:
        return {"paused": True, "indefinite": True, "resume_at": None, "remaining_seconds": None}
    if not resume_at:
        return dict(_NOT_PAUSED)
    now = datetime.now(timezone.utc)
    if now >= resume_at:
        return dict(_NOT_PAUSED)
    return {
        "paused": True,
        "indefinite": False,
        "resume_at": resume_at.isoformat(),
        "remaining_seconds": int((resume_at - now).total_seconds()),
    }
