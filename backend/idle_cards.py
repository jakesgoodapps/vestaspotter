"""Idle deck — what the board rests on when board_mode = 'cards'.

Instead of flapping for every flight, the board shows a rotating deck of
6x22 "cards" (one every IDLE_CADENCE_MINUTES) and only rare types, known
liveries and watch-list tails interrupt it. After a special flies by, the
deck resumes — landing on the specials checklist with that plane ticked.

Cards (each is skipped when there's nothing to say):

  specials     TODAY AT DCA — livery tails + curated special flights scheduled
               today, ⬜ pending → 🟩 seen (daily_specials.py)
  custom       user message from the dashboard (custom_card.py); may be pinned
  yesterday    yesterday's totals from daily_history
  fact:N       Claude-written trivia (fun_facts.py)
  sun          sunrise / sunset / golden hour (sun_times.py)
  hall_of_fame king tail, top type, all-time total + next milestone
  flow         runway flow inferred from the last few sightings
  week         last 7 days
  on_this_day  same date last year (once the history is a year old)

Renderers are pure functions of plain data so `python -m backend.render_test`
can draw them with sample inputs. build_deck() does the DB reads.

Rotation state (index, last push time, interrupt window) persists in
data/idle_state.json so a container restart doesn't re-flap the board.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from . import custom_card, daily_history, daily_specials, fun_facts, sightings_db, sun_times
from .airline_colors import BLUE, GREEN, ORANGE, RED, VIOLET, WHITE, YELLOW
from .config import settings
from .formatter import COLS, ROWS, AirportView, blank_row, format_row_6_airport, write_text

_LOCAL_TZ = ZoneInfo(settings.local_timezone)
_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_STATE_PATH = _DATA_DIR / "idle_state.json"
_lock = Lock()

MAX_SPECIAL_ROWS = 4


@dataclass
class Card:
    id: str
    title: str
    matrix: list[list[int]]


# ---------------------------------------------------------------------------
# Rotation state
# ---------------------------------------------------------------------------

@dataclass
class IdleState:
    index: int = -1
    last_card_push_ts: float = 0.0
    interrupt_until: float = 0.0
    last_card_id: Optional[str] = None
    last_card_date: Optional[str] = None   # local date the last card was pushed (new day → lead with specials)
    deck_size: int = 0

    def interrupt_remaining_s(self) -> int:
        return max(0, int(self.interrupt_until - time.time()))

    def next_rotation_in_s(self) -> Optional[int]:
        if not self.last_card_push_ts:
            return None
        due = self.last_card_push_ts + settings.idle_cadence_minutes * 60
        return max(0, int(due - time.time()))


def load_state() -> IdleState:
    if not _STATE_PATH.exists():
        return IdleState()
    try:
        with _STATE_PATH.open() as f:
            data = json.load(f)
        return IdleState(**{k: v for k, v in data.items() if k in IdleState.__dataclass_fields__})
    except Exception:
        return IdleState()


def save_state(state: IdleState) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _STATE_PATH.open("w") as f:
        json.dump(asdict(state), f)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _title_row(color: int, title: str, right: str = "") -> list[int]:
    """[tile] TITLE ........ RIGHT — tile in col 0, title from col 1,
    optional right-aligned text (e.g. the date)."""
    row = blank_row()
    row[0] = color
    right = (right or "")[: COLS - 3]
    avail = COLS - 1 - (len(right) + 1 if right else 0)
    write_text(row, 1, title[:avail])
    if right:
        write_text(row, COLS - len(right), right)
    return row


def _body(text: str) -> list[int]:
    """Body line, indented one column to sit under the title text."""
    row = blank_row()
    write_text(row, 1, text[: COLS - 1])
    return row


def _centered(text: str) -> list[int]:
    row = blank_row()
    text = text[:COLS]
    write_text(row, (COLS - len(text)) // 2, text)
    return row


def _date_label(d: date) -> str:
    return f"{d.strftime('%a').upper()} {d.month}/{d.day}"


def _clock(dt: datetime) -> str:
    """'315P' / '1005A' — 4-5 chars, the compact specials-row style."""
    h = dt.hour % 12 or 12
    return f"{h}{dt.minute:02d}{'A' if dt.hour < 12 else 'P'}"


def _hour_label(h: int) -> str:
    return f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"


def _pad(matrix: list[list[int]], footer: list[int]) -> list[list[int]]:
    """Ensure 6 rows; last row is the shared airport footer."""
    body = matrix[: ROWS - 1]
    while len(body) < ROWS - 1:
        body.append(blank_row())
    return body + [footer]


# ---------------------------------------------------------------------------
# Renderers (pure)
# ---------------------------------------------------------------------------

def render_specials_card(items: list[dict], today: date, footer: list[int]) -> list[list[int]]:
    """
    🟨TODAY AT DCA  MON 8/24
    ⬜ASTROJET    ARR  315P
    🟩TWA RETRO   DEP  940A
    ⬜757 FROM DEN ARR 622P
      1 SEEN / 2 TO GO
    🟩DCA 47 ARR / 35 DEP
    """
    rows = [_title_row(YELLOW, f"TODAY AT {settings.airport_code.upper()}", _date_label(today))]
    seen = sum(1 for i in items if i.get("seen_at"))
    show = items if len(items) <= MAX_SPECIAL_ROWS else items[: MAX_SPECIAL_ROWS - 1]
    for it in show:
        row = blank_row()
        row[0] = GREEN if it.get("seen_at") else WHITE
        try:
            when = _clock(datetime.fromisoformat(it["sched_local"]))
        except Exception:
            when = "?"
        tail = f" {it.get('direction', '')[:3]} {when}"      # " ARR 315P" / " DEP 1005A"
        width = COLS - 1 - len(tail)                          # label gets whatever is left (11-12 chars)
        label = (it.get("label") or "?").upper()[:width].ljust(width)
        write_text(row, 1, label + tail)
        rows.append(row)
    if len(items) > MAX_SPECIAL_ROWS:
        rows.append(_body(f"+{len(items) - len(show)} MORE / {seen} OF {len(items)} SEEN"))
    elif len(items) < MAX_SPECIAL_ROWS:
        todo = len(items) - seen
        rows.append(_body("ALL SEEN - NICE!" if todo == 0 else f"{seen} SEEN / {todo} TO GO"))
    return _pad(rows, footer)


def render_yesterday_card(snap: dict, footer: list[int]) -> list[list[int]]:
    d = date.fromisoformat(snap["date"])
    rows = [_title_row(BLUE, "YESTERDAY", _date_label(d))]
    rows.append(_body(f"{snap.get('total_flights', 0)} SEEN / {snap.get('unique_tails', 0)} TAILS"))
    if snap.get("top_tail"):
        rows.append(_body(f"TOP TAIL {snap['top_tail']} {snap.get('top_tail_count', 0)}X"))
    if snap.get("top_type"):
        rows.append(_body(f"TOP TYPE {snap['top_type']} {snap.get('top_type_count', 0)}X"))
    hourly = snap.get("hourly") or []
    if hourly and max(hourly) > 0:
        peak = max(range(len(hourly)), key=lambda h: hourly[h])
        rows.append(_body(f"PEAK HOUR {_hour_label(peak)} ({hourly[peak]}X)"))
    return _pad(rows, footer)


def render_hall_of_fame_card(stats: dict, footer: list[int]) -> list[list[int]]:
    rows = [_title_row(YELLOW, "HALL OF FAME")]
    if stats.get("king"):
        rows.append(_body(f"KING {stats['king']} {stats.get('king_count', 0)}X"))
    if stats.get("top_type"):
        rows.append(_body(f"TOP TYPE {stats['top_type']} {stats.get('top_type_count', 0)}X"))
    total = int(stats.get("total") or 0)
    rows.append(_body(f"TOTAL {total} SIGHTINGS"))
    step = 1000 if total < 20000 else 5000
    nxt = ((total // step) + 1) * step
    rows.append(_body(f"{nxt - total} MORE TO {nxt}"))
    return _pad(rows, footer)


def render_week_card(days: list[dict], footer: list[int]) -> list[list[int]]:
    days = [d for d in days if d.get("total_flights") is not None]
    rows = [_title_row(VIOLET, f"LAST {len(days)} DAYS")]
    total = sum(int(d["total_flights"]) for d in days)
    rows.append(_body(f"{total} FLIGHTS TOTAL"))
    rows.append(_body(f"AVG {round(total / len(days))} PER DAY"))
    best = max(days, key=lambda d: d["total_flights"])
    quiet = min(days, key=lambda d: d["total_flights"])
    rows.append(_body(f"BEST {_date_label(date.fromisoformat(best['date']))} {best['total_flights']}X"))
    rows.append(_body(f"QUIET {_date_label(date.fromisoformat(quiet['date']))} {quiet['total_flights']}X"))
    return _pad(rows, footer)


def render_on_this_day_card(snap: dict, footer: list[int]) -> list[list[int]]:
    d = date.fromisoformat(snap["date"])
    rows = [_title_row(ORANGE, "ONE YEAR AGO", _date_label(d))]
    rows.append(_body(f"{snap.get('total_flights', 0)} SEEN / {snap.get('unique_tails', 0)} TAILS"))
    if snap.get("top_tail"):
        rows.append(_body(f"TOP TAIL {snap['top_tail']} {snap.get('top_tail_count', 0)}X"))
    if snap.get("top_type"):
        rows.append(_body(f"TOP TYPE {snap['top_type']}"))
    return _pad(rows, footer)


_FLOW_TEXT = {
    "north": ("NORTH FLOW", "TAKEOFFS ROLL NORTH", "WATCH FOR CLIMB-OUTS"),
    "south": ("SOUTH FLOW", "LANDINGS FROM NORTH", "WATCH FOR APPROACHES"),
    "mixed": ("FLOW SHIFTING", "MIX OF ARR AND DEP", "RUNWAY CHANGE LIKELY"),
}


def render_flow_card(flow: dict, footer: list[int]) -> list[list[int]]:
    head, l2, l3 = _FLOW_TEXT.get(flow.get("flow"), ("FLOW UNKNOWN", "", ""))
    rows = [_title_row(BLUE, "RUNWAY FLOW NOW"), _body(head), _body(l2), _body(l3)]
    n = flow.get("sample_size", 0)
    rows.append(_body(f"LAST {n}: {flow.get('dep', 0)} DEP / {flow.get('arr', 0)} ARR"))
    return _pad(rows, footer)


def render_sun_card(sun: dict, today: date, footer: list[int]) -> list[list[int]]:
    rows = [_title_row(ORANGE, "SUN TODAY", _date_label(today))]
    rows.append(_body(f"SUNRISE {sun_times.fmt_clock(sun['sunrise'])}"))
    rows.append(_body(f"SUNSET {sun_times.fmt_clock(sun['sunset'])}"))
    rows.append(_body(f"GOLDEN HR {sun_times.fmt_clock(sun['golden_pm_start'])}-{sun_times.fmt_clock(sun['sunset'])}"))
    mins = sun["daylight_minutes"]
    rows.append(_body(f"DAYLIGHT {mins // 60}H {mins % 60}M"))
    return _pad(rows, footer)


def render_fact_card(fact: dict, footer: list[int]) -> list[list[int]]:
    rows = [_title_row(YELLOW, fact["title"])]
    lines = list(fact["lines"])[:4]
    # Centre short decks vertically-ish: 1-2 lines start on row 3.
    if len(lines) <= 2:
        rows.append(blank_row())
    rows += [_body(ln) for ln in lines]
    return _pad(rows, footer)


_TILE_TOKENS = {
    "red": RED, "orange": ORANGE, "yellow": YELLOW, "green": GREEN,
    "blue": BLUE, "violet": VIOLET, "purple": VIOLET, "white": WHITE,
    "black": 70, "filled": 71,
}
_TOKEN_RE = re.compile(r"\{(" + "|".join(_TILE_TOKENS) + r")\}", re.IGNORECASE)


def render_custom_line(text: str) -> list[int]:
    """One user line → 22 codes. '{red}' style tokens become tiles; plain
    text is written as-is (no centring — the user controls spacing)."""
    row = blank_row()
    pos = 0
    for part in _TOKEN_RE.split(text or ""):
        if not part:
            continue
        if part.lower() in _TILE_TOKENS:
            if pos < COLS:
                row[pos] = _TILE_TOKENS[part.lower()]
            pos += 1
        else:
            write_text(row, pos, part[: max(0, COLS - pos)])
            pos += len(part)
        if pos >= COLS:
            break
    return row


def render_custom_card(lines: list[str]) -> list[list[int]]:
    rows = [render_custom_line(ln) for ln in (lines or [])[:ROWS]]
    while len(rows) < ROWS:
        rows.append(blank_row())
    return rows


# ---------------------------------------------------------------------------
# Deck builder (DB reads happen here)
# ---------------------------------------------------------------------------

def _safe(fn, label: str):
    try:
        return fn()
    except Exception as e:
        print(f"idle-cards: {label} card skipped: {e}")
        return None


def build_deck(airport: AirportView, now: Optional[datetime] = None) -> list[Card]:
    """Assemble today's deck. Each provider is isolated so one bad query
    can't take the whole deck down. Order interleaves stats and facts."""
    now = now or datetime.now(_LOCAL_TZ)
    today = now.date()
    footer = format_row_6_airport(airport)

    custom = _safe(custom_card.get, "custom")
    if custom and custom.get("pinned"):
        return [Card("custom", "Your message (pinned)", render_custom_card(custom["lines"]))]

    cards: list[Card] = []

    def specials():
        items = daily_specials.items_for_today()
        if not items:
            return None
        return Card("specials", f"Today at {settings.airport_code.upper()}", render_specials_card(items, today, footer))

    def yesterday():
        y_iso = (today - timedelta(days=1)).isoformat()
        snap = next((h for h in daily_history.list_recent(limit=3) if h.get("date") == y_iso), None)
        if snap and snap.get("details_json") and not snap.get("hourly"):
            try:
                snap = dict(snap, hourly=json.loads(snap["details_json"]).get("hourly"))
            except Exception:
                pass
        if snap is None:
            snap = daily_history.compute_snapshot(y_iso, settings.airport_code)
        if not snap or not snap.get("total_flights"):
            return None
        return Card("yesterday", "Yesterday in review", render_yesterday_card(snap, footer))

    def hall_of_fame():
        total = sightings_db.total_sightings()
        if total < 10:
            return None
        king = (sightings_db.top_seen(limit=1) or [None])[0]
        top_type = (sightings_db.top_aircraft_types(limit=1) or [None])[0]
        stats = {
            "total": total,
            "king": king and king.get("registration"),
            "king_count": king and king.get("n"),
            "top_type": top_type and top_type.get("aircraft_type"),
            "top_type_count": top_type and top_type.get("n"),
        }
        return Card("hall_of_fame", "Hall of fame", render_hall_of_fame_card(stats, footer))

    def week():
        cutoff = (today - timedelta(days=7)).isoformat()
        days = [h for h in daily_history.list_recent(limit=8) if cutoff <= h.get("date", "") < today.isoformat()]
        if len(days) < 3:
            return None
        return Card("week", "Last 7 days", render_week_card(days, footer))

    def on_this_day():
        target = (today - timedelta(days=365)).isoformat()
        snap = next((h for h in daily_history.list_recent(limit=400) if h.get("date") == target), None)
        if not snap or not snap.get("total_flights"):
            return None
        return Card("on_this_day", "One year ago", render_on_this_day_card(snap, footer))

    def flow():
        f = sightings_db.recent_flow_signal(settings.airport_code, n=3)
        if f.get("flow") in (None, "unknown"):
            return None
        return Card("flow", "Runway flow", render_flow_card(f, footer))

    def sun():
        st = sun_times.sun_times(today, settings.latitude, settings.longitude, settings.local_timezone)
        if not st:
            return None
        return Card("sun", "Sun & golden hour", render_sun_card(st, today, footer))

    def custom_c():
        if not custom:
            return None
        return Card("custom", "Your message", render_custom_card(custom["lines"]))

    facts = [
        Card(f"fact:{i}", f"Did you know · {f['title'].title()}", render_fact_card(f, footer))
        for i, f in enumerate(_safe(fun_facts.get_facts, "facts") or [])
    ]

    stats_cards = [
        _safe(specials, "specials"),
        _safe(custom_c, "custom"),
        _safe(yesterday, "yesterday"),
        _safe(sun, "sun"),
        _safe(hall_of_fame, "hall_of_fame"),
        _safe(flow, "flow"),
        _safe(week, "week"),
        _safe(on_this_day, "on_this_day"),
    ]
    stats_cards = [c for c in stats_cards if c]

    # Interleave: stat, fact, stat, fact … then whatever's left over.
    fi = iter(facts)
    for c in stats_cards:
        cards.append(c)
        nxt = next(fi, None)
        if nxt:
            cards.append(nxt)
    cards.extend(fi)
    return cards


def pick_next_index(state: IdleState, deck: list[Card], after_interrupt: bool) -> int:
    """After a special flyover, land back on the checklist (so the tick is
    visible); otherwise just advance. Robust to the deck changing size."""
    if not deck:
        return -1
    if after_interrupt:
        for i, c in enumerate(deck):
            if c.id == "specials":
                return i
    # Continue from wherever the last card sits in the NEW deck (ids are stable).
    if state.last_card_id:
        for i, c in enumerate(deck):
            if c.id == state.last_card_id:
                return (i + 1) % len(deck)
    return (state.index + 1) % len(deck)
