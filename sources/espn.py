"""ESPN scoreboard fetchers.

ESPN exposes a public JSON scoreboard at:
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard

It accepts an optional `?dates=YYYYMMDD-YYYYMMDD` range. We fetch in 7-day
chunks because ESPN clamps single requests to ~10 days for most leagues.

Two parser variants are exported:
- fetch_espn       : one calendar event per ESPN event. Used for most leagues.
- fetch_espn_f1    : one calendar event per *session* within an F1 GP weekend
                     (FP1/FP2/FP3/Quali/Sprint/Race).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from db import Event
from timeutil import to_utc_iso

BASE = "https://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _fetch_range(sport: str, league: str, days_ahead: int) -> list[dict]:
    """Fetch raw ESPN event dicts across a date range.

    Most leagues accept ?dates=YYYYMMDD-YYYYMMDD in 7-day chunks.
    Cricket returns 404 on date ranges but accepts ?dates=YYYY for a full
    season, which we then filter locally.

    We start the window 1 day before UTC-today so a user in a western TZ
    doesn't lose "today's" games once UTC rolls past midnight.
    """
    today = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    end = today + timedelta(days=max(1, days_ahead) + 1)
    raw_events: list[dict] = []

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        if sport == "cricket":
            # Pull current year (and next year if window crosses Jan 1)
            years = {today.year, end.year}
            for y in years:
                url = f"{BASE}/{sport}/{league}/scoreboard"
                r = client.get(url, params={"dates": str(y), "limit": "500"})
                r.raise_for_status()
                data = r.json()
                raw_events.extend(data.get("events", []) or [])
        else:
            cursor = today
            while cursor < end:
                chunk_end = min(cursor + timedelta(days=7), end)
                url = f"{BASE}/{sport}/{league}/scoreboard"
                params = {
                    "dates": f"{cursor.strftime('%Y%m%d')}-{chunk_end.strftime('%Y%m%d')}",
                    "limit": "200",
                }
                r = client.get(url, params=params)
                # ESPN returns 404 for date ranges that fall entirely outside
                # a league's season (e.g. NCAAM in June). That's not an error
                # for us — it's "no games this window", so move on.
                if r.status_code == 404:
                    cursor = chunk_end + timedelta(days=1)
                    continue
                r.raise_for_status()
                data = r.json()
                raw_events.extend(data.get("events", []) or [])
                cursor = chunk_end + timedelta(days=1)

    # Dedupe by ESPN event id (in case chunks overlap)
    seen = set()
    out = []
    for e in raw_events:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            out.append(e)

    # For cricket (and as a general safety net) filter to our window locally
    today_iso = today.isoformat()
    end_iso = end.isoformat()
    filtered = []
    for e in out:
        d = e.get("date") or ""
        # ESPN dates look like "2026-05-31T14:00Z"; compare the YYYY-MM-DD prefix
        day = d[:10]
        if not day:
            continue
        if today_iso <= day < end_iso:
            filtered.append(e)
    return filtered if sport == "cricket" else out


def _status(comp: dict) -> str:
    """Normalize ESPN status into our small set."""
    state = (((comp or {}).get("status") or {}).get("type") or {}).get("state", "pre")
    completed = (((comp or {}).get("status") or {}).get("type") or {}).get("completed", False)
    if state == "pre":
        return "scheduled"
    if state == "in":
        return "in_progress"
    if completed or state == "post":
        return "final"
    return state or "scheduled"


def _broadcast(comp: dict) -> str | None:
    if not comp:
        return None
    # Try the canonical broadcasts list first
    bs = comp.get("broadcasts") or []
    names: list[str] = []
    for b in bs:
        for n in b.get("names") or []:
            if n not in names:
                names.append(n)
    if names:
        return ", ".join(names)
    # Fallback: top-level "broadcast" string used by some sports
    return comp.get("broadcast") or None


def _venue(comp: dict) -> str | None:
    v = (comp or {}).get("venue") or {}
    parts = []
    if v.get("fullName"):
        parts.append(v["fullName"])
    addr = v.get("address") or {}
    city = addr.get("city")
    if city and city not in (parts[0] if parts else ""):
        parts.append(city)
    return ", ".join(parts) if parts else None


def _event_url(e: dict) -> str | None:
    links = e.get("links") or []
    for l in links:
        if l.get("href") and ("desktop" in (l.get("rel") or []) or "web" in (l.get("rel") or [])):
            return l["href"]
    return links[0]["href"] if links and links[0].get("href") else None


def fetch_espn(source_args: dict, days_ahead: int) -> list[Event]:
    sport = source_args["sport"]
    league = source_args["league"]
    league_id = source_args.get("league_id_in_db") or _league_id_from_args(sport, league)
    duration_hours = source_args.get("duration_hours")  # league override
    multi_day = bool(source_args.get("multi_day"))
    # Optional case-insensitive filter on event note headlines (e.g. CWS games
    # inside the broader NCAA baseball feed are tagged with notes containing
    # "Men's College World Series"). Drop anything that doesn't match.
    note_contains = source_args.get("note_contains")
    raw = _fetch_range(sport, league, days_ahead)
    out: list[Event] = []
    for e in raw:
        comps = e.get("competitions") or []
        comp = comps[0] if comps else {}
        if note_contains:
            notes = comp.get("notes") or []
            blob = " ".join((n.get("headline") or "") for n in notes).lower()
            if note_contains.lower() not in blob:
                continue
        start = e.get("date") or comp.get("date")
        if not start:
            continue
        end = e.get("endDate") or comp.get("endDate")
        if multi_day:
            # ESPN's endDate is start-of-final-day (e.g. for a Thu-Sun
            # tournament, end = "Sun 00:00 ET"). FullCalendar treats allDay
            # `end` as EXCLUSIVE, so without +24h Sunday gets dropped from
            # the banner. Default to 4 days when ESPN gives no end at all.
            if end:
                end_iso = _add_hours(end, 24)
            else:
                end_iso = _add_hours(start, 24 * 4)
        else:
            end_iso = _normalize_end(start, end, sport, duration_hours)
        title = e.get("name") or e.get("shortName") or "Untitled event"
        subtitle = e.get("description") or comp.get("description") or ""
        out.append(Event(
            league=league_id,
            source_id=str(e["id"]),
            title=title,
            subtitle=subtitle,
            start_utc=_to_iso(start),
            end_utc=_to_iso(end_iso) if end_iso else None,
            venue=_venue(comp),
            broadcast=_broadcast(comp),
            url=_event_url(e),
            status=_status(comp),
            extra={
                "short_name": e.get("shortName"),
                "competitors": _competitors(comp),
            },
            all_day=multi_day,
        ))
    return out


def fetch_espn_f1(source_args: dict, days_ahead: int) -> list[Event]:
    """F1 events contain multiple sessions; flatten into one calendar event per session."""
    sport = source_args["sport"]
    league = source_args["league"]
    league_id = source_args.get("league_id_in_db") or "f1"
    raw = _fetch_range(sport, league, days_ahead)
    out: list[Event] = []
    for e in raw:
        gp_name = e.get("name") or e.get("shortName") or "Grand Prix"
        for comp in e.get("competitions") or []:
            start = comp.get("date") or comp.get("startDate")
            if not start:
                continue
            session_type = ((comp.get("type") or {}).get("abbreviation")
                            or (comp.get("type") or {}).get("text")
                            or "Session")
            # Heuristic session length: practice/quali ~ 1h, race ~ 2h, sprint ~ 1h
            dur_h = 2 if "race" in session_type.lower() else 1
            end_iso = _add_hours(start, dur_h)
            out.append(Event(
                league=league_id,
                source_id=str(comp["id"]),
                title=f"{gp_name} — {session_type}",
                subtitle="Formula 1",
                start_utc=_to_iso(start),
                end_utc=_to_iso(end_iso),
                venue=_venue(comp),
                broadcast=_broadcast(comp),
                url=_event_url(e),
                status=_status(comp),
                extra={
                    "gp": gp_name,
                    "session": session_type,
                },
            ))
    return out


def _competitors(comp: dict) -> list[dict]:
    out = []
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        athlete = c.get("athlete") or {}
        out.append({
            "name": team.get("displayName") or athlete.get("displayName") or c.get("displayName"),
            "abbr": team.get("abbreviation") or athlete.get("shortName"),
            "home_away": c.get("homeAway"),
        })
    return out


def _normalize_end(start_iso: str, end_iso: str | None, sport: str,
                   override_hours: float | None = None) -> str | None:
    """Compute end time. League override wins; else ESPN's end if reasonable; else sport default."""
    target = override_hours if override_hours is not None else _default_duration_hours(sport)
    if not end_iso:
        return _add_hours(start_iso, target)
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        # If ESPN's window is more than ~12h, it's a placeholder day-end — override
        if (e - s).total_seconds() > 12 * 3600:
            return _add_hours(start_iso, target)
        # If override is provided, use it (assume league knows better than ESPN's open-ended end)
        if override_hours is not None:
            return _add_hours(start_iso, override_hours)
        return end_iso
    except Exception:
        return end_iso


def _default_duration_hours(sport: str) -> float:
    return {
        "baseball": 3.0,
        "basketball": 2.5,
        "hockey": 2.5,
        "mma": 5.0,
        "cricket": 4.0,
        "racing": 2.0,
        "golf": 8.0,
    }.get(sport, 3.0)


def _add_hours(iso: str, hours: float) -> str:
    try:
        canonical = to_utc_iso(iso)
        if canonical is None:
            return iso
        dt = datetime.fromisoformat(canonical) + timedelta(hours=hours)
        return to_utc_iso(dt) or iso
    except Exception:
        return iso


def _to_iso(iso: str) -> str:
    """Thin compatibility wrapper around timeutil.to_utc_iso() so anything
    that used to call _to_iso() still gets canonical output."""
    return to_utc_iso(iso) or iso


def _league_id_from_args(sport: str, league: str) -> str:
    """Best-effort map (sport, league) -> our internal league id.

    The dispatcher passes source_args verbatim; we don't always know our own id,
    so reconstruct it. The caller (refresh.py) overrides this by passing
    'league_id_in_db' in source_args.
    """
    if league == "8048" and sport == "cricket":
        return "ipl"
    return league


def fetch_espn_multi(source_args: dict, days_ahead: int) -> list[Event]:
    """Fetch multiple ESPN league IDs and merge them under a single internal
    league. Needed for cricket ODIs — ESPN gives every bilateral tour its own
    league ID, so "ODI cricket" is really an aggregation of many tour IDs.

    Expected source_args:
      - sport: ESPN sport slug (e.g. "cricket")
      - leagues: list of ESPN league IDs (strings) to fetch and merge
      - league_id_in_db: our internal league id (passed by refresh.py)
      - other fetch_espn args (duration_hours, multi_day, note_contains, …)
        forwarded verbatim to each sub-fetch.
    """
    leagues = source_args.get("leagues") or []
    if not leagues:
        return []
    seen_ids: set[str] = set()
    out: list[Event] = []
    for lg in leagues:
        sub_args = dict(source_args)
        sub_args.pop("leagues", None)
        sub_args["league"] = str(lg)
        try:
            events = fetch_espn(sub_args, days_ahead)
        except Exception:
            # One bad/expired tour ID shouldn't kill the whole ODI fetch —
            # ESPN returns 404 once a series falls out of its data window.
            continue
        for e in events:
            if e.source_id in seen_ids:
                continue
            seen_ids.add(e.source_id)
            out.append(e)
    return out
