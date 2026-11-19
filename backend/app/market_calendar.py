"""Exchange calendar (Asia/Kolkata): trading days, holidays, session phases.

NSE 2026 holiday list (weekday closures). Source: smallcase summary of the NSE circular;
verify against the official NSE circular each year and add extra dates to
DATA_DIR/holidays.txt (one YYYY-MM-DD per line) for special closures.
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
    "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
}

MARKET_OPEN = time(9, 15)
FNO_CLOSE = time(15, 40)  # NSE F&O close from 3 Aug 2026 (was 15:30)
EQ_CLOSE = time(15, 30)


def _extra_holidays(data_dir: Path | None) -> set[str]:
    if data_dir is None:
        return set()
    p = data_dir / "holidays.txt"
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def is_trading_day(d: date, data_dir: Path | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in (NSE_HOLIDAYS_2026 | _extra_holidays(data_dir))


def parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def phase(now: datetime, analysis_start: str, entry_start: str, entry_end: str, exit_time: str,
          data_dir: Path | None = None) -> str:
    """CLOSED_DAY | PRE_MARKET | ANALYSIS | ENTRY | MANAGE_ONLY | AFTER_CUTOFF."""
    if not is_trading_day(now.date(), data_dir):
        return "CLOSED_DAY"
    t = now.time()
    if t < parse_hm(analysis_start):
        return "PRE_MARKET"
    if t < parse_hm(entry_start):
        return "ANALYSIS"
    if t < parse_hm(entry_end):
        return "ENTRY"
    if t < parse_hm(exit_time):
        return "MANAGE_ONLY"
    return "AFTER_CUTOFF"
