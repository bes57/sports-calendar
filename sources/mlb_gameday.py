"""MLB.com Gameday links for MLB games.

ESPN is where the schedule comes from, but the place to *watch* a baseball
game's page is MLB.com. MLB's public Stats API (no key) lists every game
with its gamePk, and https://www.mlb.com/gameday/{gamePk} is that game's
Gameday page. Matching is by the two club names — identical between ESPN's
displayName and MLB's team name for all 30 clubs (checked 2026-08-29) — and
the closest first pitch, which separates doubleheaders.

Best-effort: on any failure the ESPN link stays.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from db import Event

API = "https://statsapi.mlb.com/api/v1/schedule"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def gameday_url(game_pk: int | str) -> str:
    return f"https://www.mlb.com/gameday/{game_pk}"


def annotate(events: list[Event]) -> int:
    """Point each MLB event's `url` at its Gameday page, keeping ESPN's link
    in extra["espn_url"]. Returns how many events were re-pointed."""
    starts = [_parse(e.start_utc) for e in events]
    starts = [s for s in starts if s]
    if not starts:
        return 0
    try:
        games = _fetch_schedule(min(starts).date() - timedelta(days=1),
                                max(starts).date() + timedelta(days=1))
    except Exception:
        return 0

    # (frozenset of club names) -> [(first pitch, gamePk)]
    by_matchup: dict[frozenset, list[tuple[datetime, int]]] = {}
    for g in games:
        try:
            names = frozenset((g["teams"]["away"]["team"]["name"],
                               g["teams"]["home"]["team"]["name"]))
            when = _parse(g["gameDate"])
            pk = int(g["gamePk"])
        except (KeyError, TypeError, ValueError):
            continue
        if when:
            by_matchup.setdefault(names, []).append((when, pk))

    linked = 0
    for ev in events:
        comps = (ev.extra or {}).get("competitors") or []
        names = frozenset(c.get("name") for c in comps if c.get("name"))
        start = _parse(ev.start_utc)
        cands = by_matchup.get(names) if len(names) == 2 else None
        if not cands or start is None:
            continue
        when, pk = min(cands, key=lambda c: abs(c[0] - start))
        if abs(when - start) > timedelta(hours=6):
            continue  # same clubs, different day — not this game
        if ev.url and "mlb.com/gameday" not in ev.url:
            ev.extra["espn_url"] = ev.url
        ev.url = gameday_url(pk)
        linked += 1
    return linked


def _fetch_schedule(start, end) -> list[dict]:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(API, params={
            "sportId": 1,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        })
        r.raise_for_status()
        data = r.json()
    return [g for day in data.get("dates") or [] for g in day.get("games") or []]


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
