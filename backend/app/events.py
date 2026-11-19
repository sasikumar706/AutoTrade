"""Market-moving event days (Indian session dates). Big gaps and sudden IV changes make option buying risky.

Built-in: RBI policy announcement days (RBI press release, schedule for 2026-27), the day after US Fed (FOMC)
decisions (announced ~23:30 IST, so India reacts the next morning; 2027 dates are the Fed's tentative
schedule) and the Union Budget. Add your own in data/events.txt, one per line: 2026-12-04 RBI policy
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

BUILTIN = {
    # RBI MPC decision days
    "2026-04-08": "RBI policy", "2026-06-05": "RBI policy", "2026-08-05": "RBI policy", "2026-10-07": "RBI policy",
    "2026-12-04": "RBI policy", "2027-02-05": "RBI policy",
    # Indian session after each FOMC decision
    "2026-01-29": "US Fed decision", "2026-03-19": "US Fed decision", "2026-04-30": "US Fed decision",
    "2026-06-18": "US Fed decision", "2026-07-30": "US Fed decision", "2026-09-17": "US Fed decision",
    "2026-10-29": "US Fed decision", "2026-12-10": "US Fed decision",
    "2027-01-28": "US Fed decision", "2027-03-18": "US Fed decision", "2027-04-29": "US Fed decision",
    "2027-06-10": "US Fed decision", "2027-07-29": "US Fed decision", "2027-09-16": "US Fed decision",
    "2027-10-28": "US Fed decision", "2027-12-09": "US Fed decision",
    # Union Budget
    "2026-02-01": "Union Budget", "2027-02-01": "Union Budget",
}


def user_events(data_dir) -> dict[str, str]:
    out: dict[str, str] = {}
    if not data_dir:
        return out
    f = Path(data_dir) / "events.txt"
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d, _, note = line.partition(" ")
        try:
            date.fromisoformat(d)
        except ValueError:
            continue
        out[d] = note.strip() or "Event"
    return out


def event_on(d: date, data_dir=None) -> str | None:
    k = d.isoformat()
    return user_events(data_dir).get(k) or BUILTIN.get(k)


def upcoming(today: date, data_dir=None, n: int = 8) -> list[dict]:
    allev = {**BUILTIN, **user_events(data_dir)}
    return [{"day": k, "event": v} for k, v in sorted(allev.items()) if k >= today.isoformat()][:n]


def save_user_events(data_dir, items: list[dict]):
    f = Path(data_dir) / "events.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Your own event days: YYYY-MM-DD note"]
    for it in items:
        date.fromisoformat(it["day"])
        lines.append(f"{it['day']} {str(it.get('event') or 'Event').strip()[:60]}")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
