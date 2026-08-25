"""User-authored message card for the idle deck ("WELCOME TO DC, AILEOS!").

Set from the dashboard: up to 6 lines of 22 chars. Lines may contain tile
tokens like {red} {blue} {white} which render as colored squares — the same
mental model as Vestaboard's own message composer.

  pinned=True  → the deck shows ONLY this card (guest weekend, party, etc.)
  pinned=False → it joins the rotation like any other card

Persisted to data/custom_card.json. Expires automatically after `hours`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "custom_card.json"
_lock = Lock()

MAX_LINES = 6
MAX_HOURS = 24 * 14


def _read() -> Optional[dict]:
    if not _STATE_PATH.exists():
        return None
    try:
        with _STATE_PATH.open() as f:
            return json.load(f)
    except Exception:
        return None


def _write(state: Optional[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if state is None:
        if _STATE_PATH.exists():
            _STATE_PATH.unlink()
        return
    with _STATE_PATH.open("w") as f:
        json.dump(state, f)


def get() -> Optional[dict]:
    """Current card if set and not expired, else None (and clears it on disk)."""
    with _lock:
        state = _read()
        if not state:
            return None
        try:
            expires = datetime.fromisoformat(state["expires_at"])
        except Exception:
            _write(None)
            return None
        if datetime.now(timezone.utc) >= expires:
            _write(None)
            return None
        return state


def set_card(lines: list[str], hours: float, pinned: bool) -> dict:
    # 22 tile tokens ("{orange}" x22) is ~180 chars, so cap generously.
    cleaned = [(ln or "").rstrip()[:300] for ln in lines][:MAX_LINES]
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    if not any(cleaned):
        raise ValueError("card needs at least one non-empty line")
    hours = max(0.25, min(float(hours), MAX_HOURS))
    now = datetime.now(timezone.utc)
    state = {
        "lines": cleaned,
        "pinned": bool(pinned),
        "set_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=hours)).isoformat(),
    }
    with _lock:
        _write(state)
    return state


def clear() -> bool:
    with _lock:
        existed = _STATE_PATH.exists()
        _write(None)
        return existed
