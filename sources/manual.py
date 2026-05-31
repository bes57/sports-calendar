"""Manual one-off events — for things that aren't on any public scoreboard
API (NBA Draft, NFL Draft, Super Bowl ceremony date, etc.).

To add or update an event, just edit the EVENTS dict below. Each top-level
key is referenced from a League entry in leagues.py via
`source_args={"event_key": "..."}`.

All datetimes are ISO 8601 with explicit offset; they get normalized to
canonical UTC ISO automatically via timeutil.to_utc_iso().
"""

from __future__ import annotations

from db import Event
from timeutil import to_utc_iso


EVENTS: dict[str, list[dict]] = {
    "mlb_allstar_2026": [
        # 2026 All-Star Week at Citizens Bank Park, Philadelphia (host: Phillies).
        # All-Star Game itself (Tue Jul 14) already comes from ESPN as "AL VS NL".
        # Listed here: the satellite events MLB.com publishes around it.
        {
            "source_id": "mlb_futures_2026",
            "title": "MLB Futures Game",
            "subtitle": "2026 All-Star Week — top prospects showcase",
            "start": "2026-07-11T19:00:00-04:00",   # ~7 PM ET, Saturday before
            "end":   "2026-07-11T22:00:00-04:00",
            "venue": "Citizens Bank Park, Philadelphia, PA",
            "broadcast": "MLB Network, MLB.TV",
            "url": "https://www.mlb.com/all-star",
        },
        {
            "source_id": "mlb_hr_derby_2026",
            "title": "Home Run Derby",
            "subtitle": "2026 MLB All-Star Week",
            "start": "2026-07-13T20:00:00-04:00",   # 8 PM ET, Monday before the game
            "end":   "2026-07-13T23:00:00-04:00",
            "venue": "Citizens Bank Park, Philadelphia, PA",
            "broadcast": "ESPN",
            "url": "https://www.mlb.com/all-star",
        },
    ],
    "nba_draft": [
        {
            "source_id": "nba_draft_2026_r1",
            "title": "NBA Draft — Round 1",
            "subtitle": "2026 NBA Draft",
            "start": "2026-06-24T23:00:00-04:00",   # 8 PM ET, typical
            "end":   "2026-06-25T03:00:00-04:00",   # ~midnight ET
            "venue": "Barclays Center, Brooklyn, NY",
            "broadcast": "ESPN, ABC",
            "url": "https://www.nba.com/draft",
        },
        {
            "source_id": "nba_draft_2026_r2",
            "title": "NBA Draft — Round 2",
            "subtitle": "2026 NBA Draft",
            "start": "2026-06-25T15:00:00-04:00",   # 3 PM ET, typical
            "end":   "2026-06-25T19:00:00-04:00",   # 7 PM ET
            "venue": "Barclays Center, Brooklyn, NY",
            "broadcast": "ESPN",
            "url": "https://www.nba.com/draft",
        },
    ],
}


def fetch_manual(source_args: dict, days_ahead: int) -> list[Event]:
    league_id = source_args.get("league_id_in_db") or "manual"
    key = source_args.get("event_key")
    if not key:
        return []
    out: list[Event] = []
    for e in EVENTS.get(key, []):
        out.append(Event(
            league=league_id,
            source_id=e["source_id"],
            title=e["title"],
            subtitle=e.get("subtitle", ""),
            start_utc=to_utc_iso(e["start"]),
            end_utc=to_utc_iso(e.get("end")),
            venue=e.get("venue"),
            broadcast=e.get("broadcast"),
            url=e.get("url"),
            status="scheduled",
            extra={"manual": True},
            all_day=bool(e.get("all_day")),
        ))
    return out
