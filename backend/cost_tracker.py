"""FlightAware API call accounting.

Records every billable call to a SQLite table so we can show running monthly
spend on the dashboard. Per-endpoint pricing is hardcoded from FA's public rate
sheet (verified against the May 2026 invoice line items).

Schema is intentionally tiny — one row per call. Queries are cheap.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

_DATA_DIR = Path(os.environ.get("VESTASPOTTER_DATA_DIR") or Path(__file__).resolve().parent / "data")
_DB_PATH = _DATA_DIR / "fa_usage.db"
_lock = Lock()
from .config import settings as _settings
_LOCAL_TZ = ZoneInfo(_settings.local_timezone)


# Per-call USD cost on FA personal tier. Verified against May 2026 invoice.
# Keep this list in sync with whatever endpoints enrichment.py actually hits.
ENDPOINT_COSTS = {
    "/flights/{ident}": 0.005,
    "/aircraft/{reg}/owner": 0.002,
    "/aircraft/types/{type}": 0.10,
    "/airports/{id}/delays": 0.01,
    "/airports/{id}/flights/counts": 0.10,  # the $603 culprit in May
    "/airports/{id}/weather/observations": 0.002,
    "/airports/{id}/flights/arrivals": 0.10,  # disabled — never call
    "/airports/{id}/flights/departures": 0.10,  # disabled — never call
}
DEFAULT_COST = 0.05  # fallback for an endpoint we forgot to price


def _connect() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fa_calls ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  endpoint TEXT NOT NULL,"
            "  called_at TEXT NOT NULL"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_called_at ON fa_calls(called_at)")


def record(endpoint: str) -> None:
    """Log one FA call. Call this AFTER the request completes (regardless of
    status) — FA bills both successful and rate-limited responses."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO fa_calls (endpoint, called_at) VALUES (?, ?)",
                (endpoint, now_iso),
            )
    except Exception:
        pass  # never let cost tracking break the actual flight pipeline


def _local_month_start_utc_iso() -> str:
    """First day of current local-Eastern month, as UTC ISO."""
    local_now = datetime.now(_LOCAL_TZ)
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start.astimezone(timezone.utc).isoformat()


def _local_day_start_utc_iso() -> str:
    local_now = datetime.now(_LOCAL_TZ)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(timezone.utc).isoformat()


def _cost_for(endpoint: str) -> float:
    return ENDPOINT_COSTS.get(endpoint, DEFAULT_COST)


def month_summary() -> dict:
    """Per-endpoint counts + dollars for the current local month, plus a few
    derived stats useful for the dashboard card."""
    month_start = _local_month_start_utc_iso()
    day_start = _local_day_start_utc_iso()
    with _lock, _connect() as conn:
        month_rows = conn.execute(
            "SELECT endpoint, COUNT(*) AS n FROM fa_calls "
            "WHERE called_at >= ? GROUP BY endpoint ORDER BY n DESC",
            (month_start,),
        ).fetchall()
        today_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM fa_calls WHERE called_at >= ?",
            (day_start,),
        ).fetchone()

    by_endpoint = []
    month_total_calls = 0
    month_total_cost = 0.0
    for r in month_rows:
        n = int(r["n"])
        unit = _cost_for(r["endpoint"])
        cost = round(n * unit, 4)
        month_total_calls += n
        month_total_cost += cost
        by_endpoint.append({
            "endpoint": r["endpoint"],
            "calls": n,
            "unit_cost": unit,
            "cost": cost,
        })

    today_calls = int(today_rows["n"]) if today_rows else 0
    today_cost = 0.0
    with _lock, _connect() as conn:
        today_endpoints = conn.execute(
            "SELECT endpoint, COUNT(*) AS n FROM fa_calls "
            "WHERE called_at >= ? GROUP BY endpoint",
            (day_start,),
        ).fetchall()
    for r in today_endpoints:
        today_cost += int(r["n"]) * _cost_for(r["endpoint"])
    today_cost = round(today_cost, 4)

    # Project end-of-month using average daily rate so far this month
    local_now = datetime.now(_LOCAL_TZ)
    days_so_far = max(1, local_now.day)
    # Find days in this month (handles 28/29/30/31)
    if local_now.month == 12:
        next_month_first = local_now.replace(year=local_now.year + 1, month=1, day=1)
    else:
        next_month_first = local_now.replace(month=local_now.month + 1, day=1)
    days_in_month = (next_month_first - local_now.replace(day=1)).days
    projected_month_cost = round((month_total_cost / days_so_far) * days_in_month, 2)

    return {
        "month_label": local_now.strftime("%B %Y"),
        "month_calls": month_total_calls,
        "month_cost": round(month_total_cost, 2),
        "today_calls": today_calls,
        "today_cost": round(today_cost, 2),
        "days_so_far": days_so_far,
        "days_in_month": days_in_month,
        "projected_month_cost": projected_month_cost,
        "by_endpoint": by_endpoint,
    }
