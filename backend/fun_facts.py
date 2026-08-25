"""Claude-generated "did you know" cards for the idle deck.

Once a day (cards mode only) we hand Claude a compact JSON snapshot of what
the spotter has actually seen — yesterday's totals, the king tail, rare
types and liveries from the last week, today's specials, runway flow, sun
times — and ask for a handful of board-sized trivia cards grounded in that
data. Each card is a title (≤21 chars) + 2-4 lines (≤22 chars) in the
Vestaboard charset.

Cost: one call, ~2k input / ~500 output tokens on claude-opus-5 ≈ 1-2¢/day.
Facts are cached in data/fun_facts.json so restarts never re-bill; a failed
attempt backs off for an hour.

Off unless ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from . import custom_data, daily_history, daily_specials, sightings_db, sun_times
from .config import settings
from .enrichment import RARE_TYPES
from .formatter import CHAR_MAP

_LOCAL_TZ = ZoneInfo(settings.local_timezone)
_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "fun_facts.json"
_lock = Lock()

MAX_FACTS = 6
TITLE_MAX = 21   # row 1 = 1 tile + 21 chars
LINE_MAX = 22
RETRY_BACKOFF_S = 3600

_SYSTEM_PROMPT = """You write tiny trivia cards for a Vestaboard split-flap display in a plane spotter's apartment near an airport. The spotter's own software feeds you a JSON snapshot of what it has seen. Your job: turn that into delightful, specific, TRUE facts.

Rules:
- Ground every card in the data provided or in well-established aviation knowledge (aircraft types, airlines, liveries, routes, airports). Never invent statistics, dates or records. If you are not confident a fact is true, pick a different fact.
- Mix it up: at least one card about the spotter's own numbers (records, milestones, streaks), the rest about the specific aircraft types, liveries, airlines, routes or airport that appear in the data. Prefer things that make someone look up at the sky.
- Format is brutal: title at most 21 characters; 2 to 4 lines, each at most 22 characters. Break lines at word boundaries. No trailing punctuation gymnastics — plain, punchy phrasing.
- Allowed characters ONLY: letters, digits, space, and ! @ # $ ( ) - + & = ; : ' " % , . / ? °  — no emoji, no asterisks, no underscores, no angle brackets, no curly quotes, no em dashes.
- Everything will be displayed in UPPERCASE; write normally and we uppercase it.
- Do not mention AI, Claude, or that these are generated. Do not address the reader by name.
- Return between 3 and 6 cards."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "lines"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _read() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        with _STATE_PATH.open() as f:
            return json.load(f)
    except Exception:
        return {}


def _write(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w") as f:
        json.dump(state, f)


def _today_iso() -> str:
    return datetime.now(_LOCAL_TZ).date().isoformat()


def enabled() -> bool:
    return bool(settings.anthropic_api_key)


def get_facts() -> list[dict]:
    """Today's facts (empty if none generated yet / feature off)."""
    if not enabled():
        return []
    with _lock:
        state = _read()
    if state.get("date") != _today_iso():
        return []
    return list(state.get("facts") or [])


def status() -> dict:
    with _lock:
        state = _read()
    return {
        "enabled": enabled(),
        "model": settings.fun_facts_model if enabled() else None,
        "date": state.get("date"),
        "generated_at": state.get("generated_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "count": len(state.get("facts") or []) if state.get("date") == _today_iso() else 0,
        "usage": state.get("usage"),
    }


# ---------------------------------------------------------------------------
# Sanitizing — the board charset is unforgiving
# ---------------------------------------------------------------------------

_SUBSTITUTIONS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "×": "X",
    "*": "", "_": " ", "<": "", ">": "", "[": "(", "]": ")", "{": "(", "}": ")",
    "|": "/", "\\": "/", "~": "-", "`": "'", "^": "",
}


def sanitize_line(text: str) -> str:
    out = []
    for ch in (text or ""):
        ch = _SUBSTITUTIONS.get(ch, ch)
        for c in ch:
            c = c.upper()
            if c in CHAR_MAP:
                out.append(c)
    return " ".join("".join(out).split())  # collapse runs of whitespace


def sanitize_fact(raw: dict) -> Optional[dict]:
    """Return a board-safe {title, lines} or None if it can't fit."""
    title = sanitize_line(str(raw.get("title") or ""))[:TITLE_MAX].strip()
    lines = [sanitize_line(str(ln)) for ln in (raw.get("lines") or [])]
    lines = [ln for ln in lines if ln]
    if not title or not (1 <= len(lines) <= 4):
        return None
    if any(len(ln) > LINE_MAX for ln in lines):
        return None
    return {"title": title, "lines": lines}


# ---------------------------------------------------------------------------
# Context — everything Claude gets to work with (all local DB reads)
# ---------------------------------------------------------------------------

def build_context() -> dict:
    now = datetime.now(_LOCAL_TZ)
    today = now.date()
    yesterday_iso = (today - timedelta(days=1)).isoformat()

    history = daily_history.list_recent(limit=8)
    yesterday = next((h for h in history if h.get("date") == yesterday_iso), None)
    if yesterday is None:
        try:
            yesterday = daily_history.compute_snapshot(yesterday_iso, settings.airport_code)
        except Exception:
            yesterday = None
    week = [
        {k: h.get(k) for k in ("date", "total_flights", "arrivals", "departures", "unique_tails", "top_tail", "top_tail_count", "top_type")}
        for h in history if h.get("date") != today.isoformat()
    ][:7]

    rare_types = set(RARE_TYPES) | set(custom_data.all_airport_rare())
    liveries = custom_data.all_liveries()
    rare_seen = sightings_db.recent_matching(days=7, types=rare_types, limit=15)
    livery_seen = sightings_db.recent_matching(days=7, registrations=set(liveries), limit=15)
    for s in livery_seen:
        s["livery"] = liveries.get((s.get("registration") or "").upper())
    for s in rare_seen:
        s["why_rare"] = RARE_TYPES.get(s.get("aircraft_type")) or custom_data.get_airport_rare(s.get("aircraft_type"))

    sun = None
    try:
        st = sun_times.sun_times(today, settings.latitude, settings.longitude, settings.local_timezone)
        if st:
            sun = {
                "sunrise": sun_times.fmt_clock(st["sunrise"]),
                "sunset": sun_times.fmt_clock(st["sunset"]),
                "daylight_minutes": st["daylight_minutes"],
            }
    except Exception:
        pass

    top_tails = sightings_db.top_seen(limit=5)
    top_types = sightings_db.top_aircraft_types(limit=8)
    for t in top_types:
        t["name"] = custom_data.get_aircraft_name(t.get("aircraft_type"))

    return {
        "today": today.isoformat(),
        "weekday": now.strftime("%A"),
        "airport": settings.airport_code.upper(),
        "observer": "apartment window near the airport",
        "all_time_sightings": sightings_db.total_sightings(),
        "top_tails_all_time": top_tails,
        "top_types_all_time": top_types,
        "yesterday": yesterday and {k: yesterday.get(k) for k in ("date", "total_flights", "arrivals", "departures", "unique_tails", "top_tail", "top_tail_count", "top_type", "top_type_count", "hourly")},
        "last_7_days": week,
        "rare_types_seen_last_7_days": rare_seen,
        "special_liveries_seen_last_7_days": livery_seen,
        "specials_scheduled_today": [
            {k: i.get(k) for k in ("label", "kind", "registration", "ident", "aircraft_type", "direction", "other_airport", "sched_local")}
            for i in daily_specials.items_for_today()
        ],
        "runway_flow_now": sightings_db.recent_flow_signal(settings.airport_code, n=3),
        "sun": sun,
        "known_liveries_catalog": liveries,
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

async def _call_claude(context: dict) -> tuple[list[dict], dict]:
    """One Messages API call → (raw facts, usage). Raises on API failure."""
    import anthropic  # lazy: keep the app importable without the SDK installed

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_msg = (
        "Here is today's spotter snapshot as JSON. Write the cards.\n\n"
        + json.dumps(context, default=str, indent=1)
    )
    kwargs = dict(
        model=settings.fun_facts_model,
        max_tokens=4000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    try:
        # Server-side refusal fallback: if the primary model declines, the API
        # re-runs the request on a fallback model inside the same call.
        response = await client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    except anthropic.BadRequestError:
        # Older API surface / beta not available for this org — plain call.
        response = await client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", None),
        "output_tokens": getattr(response.usage, "output_tokens", None),
        "model": getattr(response, "model", None),
    }
    return list(data.get("facts") or []), usage


async def ensure_fresh(force: bool = False) -> bool:
    """Generate today's facts if we don't have them. Returns True if a call was made."""
    if not enabled():
        return False
    today = _today_iso()
    with _lock:
        state = _read()
    if not force and state.get("date") == today and state.get("facts"):
        return False
    last_attempt = state.get("last_attempt_at")
    if not force and last_attempt:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_attempt) < timedelta(seconds=RETRY_BACKOFF_S):
                return False
        except Exception:
            pass

    with _lock:
        state = _read()
        state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        _write(state)

    try:
        context = build_context()
        raw_facts, usage = await _call_claude(context)
    except Exception as e:
        print(f"fun-facts: generation failed: {e}")
        with _lock:
            state = _read()
            state["last_error"] = str(e)[:300]
            _write(state)
        return True

    facts = [f for f in (sanitize_fact(r) for r in raw_facts) if f][:MAX_FACTS]
    dropped = len(raw_facts) - len(facts)
    with _lock:
        state = {
            "date": today,
            "facts": facts,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_error": None if facts else "all facts failed board-charset validation",
            "usage": usage,
        }
        _write(state)
    print(f"fun-facts: {len(facts)} card(s) for {today} ({dropped} dropped by sanitizer, usage={usage})")
    return True
