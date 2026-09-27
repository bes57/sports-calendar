"""ESPN scoreboard fetchers.

ESPN exposes a public JSON scoreboard at:
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard

`?dates=` takes one calendar month (YYYYMM) or one day (YYYYMMDD); a league
is fetched as the months touching its window, see _fetch_range for the
details and the fallback for months ESPN truncates.

Parser variants exported:
- fetch_espn       : one calendar event per ESPN event. Used for most leagues.
- fetch_espn_f1    : one calendar event per *session* within an F1 GP weekend
                     (FP1/FP2/FP3/Quali/Sprint/Race).
- fetch_espn_mma   : one calendar event per *card segment* of a fight night
                     (early prelims / prelims / main card).
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx

from db import Event
from timeutil import to_utc_iso

BASE = "https://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# ESPN's edge rate-limits a host that bursts at it with a flat 403 (seen
# 2026-08-29 on Railway after a run of redeploys, each of which re-fetched
# every league from an empty DB). Once we've been told off, keep quiet for a
# while instead of having every remaining league and chunk pile on — that
# only prolongs the block. The refresh scheduler tries again next cycle.
_BLOCK_STATUSES = {403, 429}
_BLOCK_COOLDOWN_S = 15 * 60
_blocked_until = 0.0
_block_lock = threading.Lock()


class EspnBlocked(RuntimeError):
    pass


def _check_block() -> None:
    if time.time() < _blocked_until:
        remaining = int((_blocked_until - time.time()) // 60) + 1
        raise EspnBlocked(f"ESPN is rate-limiting this host (403); retrying in ~{remaining} min")


def _note_block(status: int) -> None:
    global _blocked_until
    with _block_lock:
        _blocked_until = max(_blocked_until, time.time() + _BLOCK_COOLDOWN_S)


# ESPN dropped support for `?dates=YYYYMMDD-YYYYMMDD` ranges on 2026-09-25 —
# every such request now 400s with "Failed to get events endpoint", which
# took every ESPN-backed league's refresh down with it (the calendar kept
# showing the Aug-29 schedule while a rescheduled doubleheader was being
# played). What still works is one calendar month (`?dates=YYYYMM`) or one
# day (`?dates=YYYYMMDD`), so a league is fetched as the ~10 months touching
# its window instead of ~40 seven-day chunks.
#
# `limit` caps a month's events, and the largest value ESPN honours is 1000
# — or 500 once `groups` is in the query. Anything higher makes it silently
# answer with 25 events, as if no limit had been given, which is worse than
# a 400. College basketball has more than 1000 games in each of Nov–Feb, so
# a month that comes back at the cap is re-fetched one day at a time
# (_fetch_range); a single day never gets near it.
_MONTH_LIMIT = 1000
_MONTH_LIMIT_WITH_GROUPS = 500


def _limit(extra_params: dict | None) -> int:
    return _MONTH_LIMIT_WITH_GROUPS if (extra_params or {}).get("groups") else _MONTH_LIMIT


def _fetch_chunk(sport: str, league: str, dates: str,
                 extra_params: dict | None = None) -> list[dict]:
    """Fetch one scoreboard chunk — `dates` is ESPN's key for a month
    (YYYYMM) or a day (YYYYMMDD). Each call uses its own httpx.Client so
    chunks can be fetched in parallel (Client isn't safe to share across
    threads). A 404 means "no games in this window" — not an error — so we
    return [] for it. A 403/429 trips the block above; one polite retry
    first, in case it was a momentary limit."""
    url = f"{BASE}/{sport}/{league}/scoreboard"
    params = {
        "dates": dates,
        "limit": str(_limit(extra_params)),
        **(extra_params or {}),
    }
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for attempt in (0, 1):
            _check_block()
            r = client.get(url, params=params)
            if r.status_code == 404:
                return []
            if r.status_code in _BLOCK_STATUSES:
                if attempt == 0:
                    time.sleep(3)
                    continue
                _note_block(r.status_code)
                _check_block()
            r.raise_for_status()
            return r.json().get("events", []) or []
    return []


def _chunk_workers() -> int:
    """Max concurrent date-chunk requests within a single league fetch.
    Kept modest: with refresh.py's league pool on top, the product of the
    two is how hard we hit ESPN at once, and 6×8 got us blocked."""
    return max(1, int(os.getenv("ESPN_CHUNK_WORKERS", "3")))


def _multi_workers() -> int:
    """Max concurrent per-tour fetches inside fetch_espn_multi (cricket)."""
    return max(1, int(os.getenv("ESPN_MULTI_WORKERS", "6")))


def _fetch_range(sport: str, league: str, days_ahead: int, days_behind: int = 2,
                 extra_params: dict | None = None) -> list[dict]:
    """Fetch raw ESPN event dicts across a date range.

    `extra_params` are added to every scoreboard request — e.g. `groups` for
    college football, which ESPN otherwise limits to FBS.

    Most leagues are fetched one calendar month at a time (?dates=YYYYMM),
    falling back to single days for a month ESPN truncates. Cricket only
    answers ?dates=YYYY for a full season. Either way the result is wider
    than the window, so it's filtered locally afterwards.

    `days_behind` controls how far into the past the window starts — must be
    >= refresh.py's purge_old grace period, or a just-finished game gets
    pruned by prune_league_to a day before purge_old would naturally drop it.
    The default of 2 is only a fallback for callers that don't pass one
    explicitly; refresh_league always does, sourced from FETCH_DAYS_BEHIND.
    """
    now_date = datetime.now(timezone.utc).date()
    today = now_date - timedelta(days=days_behind)
    # Anchored to now_date, not `today` — otherwise a bigger days_behind
    # would eat into the forward-looking span by the same amount.
    end = now_date + timedelta(days=max(1, days_ahead) + 1)
    raw_events: list[dict] = []

    if sport == "cricket":
        # Cricket 404s on date ranges but accepts ?dates=YYYY for a full season.
        # That's only 1-2 requests, so keep it serial on a single client.
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            years = {today.year, end.year}  # next year too if window crosses Jan 1
            for y in years:
                _check_block()
                url = f"{BASE}/{sport}/{league}/scoreboard"
                r = client.get(url, params={"dates": str(y), "limit": "500",
                                            **(extra_params or {})})
                if r.status_code in _BLOCK_STATUSES:
                    _note_block(r.status_code)
                    _check_block()
                r.raise_for_status()
                data = r.json()
                raw_events.extend(data.get("events", []) or [])
    else:
        # One request per month touching the window, fetched in parallel —
        # the months are independent, so a 270-day window is a couple of
        # concurrent waves rather than a serial walk. ESPN 404s for months
        # outside a league's season; _fetch_chunk treats that as "no games".
        months = _months(today, end)
        workers = min(len(months), _chunk_workers()) or 1
        limit = _limit(extra_params)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            by_month = dict(zip(months, ex.map(
                lambda key: _fetch_chunk(sport, league, key, extra_params),
                months,
            )))
        for evs in by_month.values():
            raw_events.extend(evs)
        # A month that came back at the cap was truncated (college
        # basketball in season). Its days inside the window are fetched
        # one by one; the dedupe below folds them onto the month's rows.
        days = [
            day
            for key, evs in by_month.items() if len(evs) >= limit
            for day in _days_in_month(key, today, end)
        ]
        if days:
            workers = min(len(days), _chunk_workers()) or 1
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for evs in ex.map(
                    lambda key: _fetch_chunk(sport, league, key, extra_params),
                    days,
                ):
                    raw_events.extend(evs)

    # Dedupe by ESPN event id (in case chunks overlap)
    seen = set()
    out = []
    for e in raw_events:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            out.append(e)

    # Month and season chunks overshoot the window on both ends; trim to it
    # so the first month's stale games aren't upserted only for purge_old
    # to delete them again a moment later.
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
    return filtered


def _months(start: date, end: date) -> list[str]:
    """ESPN ?dates= keys (YYYYMM) for every month touching [start, end)."""
    out: list[str] = []
    last = end - timedelta(days=1)
    y, m = start.year, start.month
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days_in_month(key: str, start: date, end: date) -> list[str]:
    """ESPN ?dates= keys (YYYYMMDD) for the days of month `key` (YYYYMM)
    that fall inside [start, end)."""
    y, m = int(key[:4]), int(key[4:])
    first = date(y, m, 1)
    following = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    lo, hi = max(first, start), min(following, end)
    return [(lo + timedelta(days=i)).strftime("%Y%m%d") for i in range((hi - lo).days)]


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
    days_behind = source_args.get("days_behind", 2)
    # ESPN "groups" id (a division/conference filter). College football
    # defaults to FBS only; see leagues.py for the value that covers FCS too.
    extra_params = {"groups": str(source_args["groups"])} if source_args.get("groups") else None
    raw = _fetch_range(sport, league, days_ahead, days_behind, extra_params)
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
        extra = {
            "short_name": e.get("shortName"),
            "competitors": _competitors(comp),
        }
        playoff = _postseason(e, comp)
        if playoff:
            extra["playoff"] = playoff  # round label; the calendar flags the tile
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
            extra=extra,
            all_day=multi_day,
        ))
    if source_args.get("gameday_links"):
        try:
            from sources import mlb_gameday
            mlb_gameday.annotate(out)
        except Exception:
            pass  # ESPN links stay
    return out


def fetch_espn_f1(source_args: dict, days_ahead: int) -> list[Event]:
    """F1 events contain multiple sessions; flatten into one calendar event per session."""
    sport = source_args["sport"]
    league = source_args["league"]
    league_id = source_args.get("league_id_in_db") or "f1"
    days_behind = source_args.get("days_behind", 2)
    raw = _fetch_range(sport, league, days_ahead, days_behind)
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


# ESPN lists a fight card's bouts in running order — prelims first, main event
# last — and every bout in the same segment of the night shares one start time.
# So the groups of bouts, ordered by that time, ARE the night's segments, and
# naming them backwards from the final group is what makes "Main Card" reliable
# whether a card has one segment (Contender Series) or three (a numbered UFC).
_MMA_SEGMENTS = ("Main Card", "Prelims", "Early Prelims")

# On a busy evening a fight card gets one narrow lane among a dozen ball games,
# so the tile has room for a handful of characters — and ESPN's shortName spends
# 29 of them before saying anything ("Dana White's Contender Series" rendered as
# "Dan Whi Con Ser vs. Faz"). Tiles use the promotion's own abbreviation; the
# full name still shows on the popover.
_CARD_TILE_ALIASES = {"Dana White's Contender Series": "UFC DWCS"}


def fetch_espn_mma(source_args: dict, days_ahead: int) -> list[Event]:
    """MMA: one calendar event per card segment, headlined by its last bout.

    ESPN dates a fight card at its FIRST bout, so one block per event put UFC
    330 on the calendar at 5:30 PM ET (early prelims) and ended it at 8:30 —
    half an hour before Makhachev vs. Machado Garry walked out at 9. Splitting
    on the bout start times fixes the timing and gives every block a title you
    can pick out of a crowded evening, the same way fetch_espn_f1 splits a
    Grand Prix weekend into sessions.
    """
    sport = source_args["sport"]
    league = source_args["league"]
    league_id = source_args.get("league_id_in_db") or _league_id_from_args(sport, league)
    duration_hours = source_args.get("duration_hours") or _default_duration_hours(sport)
    days_behind = source_args.get("days_behind", 2)
    raw = _fetch_range(sport, league, days_ahead, days_behind)

    out: list[Event] = []
    for e in raw:
        segments = _mma_segments(e.get("competitions") or [])
        if not segments:
            continue
        card_name = e.get("name") or e.get("shortName") or "Fight card"
        short_card = e.get("shortName") or card_name
        for i, (start, comps) in enumerate(segments):
            bouts = _bouts(comps)
            if not bouts:
                continue
            # Index from the END of the night: 0 is always the main card, so a
            # card that later gains an early-prelims block doesn't renumber
            # (and re-key) the segments already on the calendar.
            from_end = len(segments) - 1 - i
            segment = _MMA_SEGMENTS[min(from_end, len(_MMA_SEGMENTS) - 1)]
            headline = bouts[-1]  # last bout of a segment tops it
            tile_card = _CARD_TILE_ALIASES.get(short_card, short_card)
            if from_end != 0:
                title = f"{short_card} — {segment}"
                short_name = f"{tile_card} — {segment}"
            elif ":" in card_name and " vs" in card_name:
                # ESPN already bills the main event in the event name, in the
                # promotion's own wording and surname-only ("UFC 330: Makhachev
                # vs. Machado Garry"). Prefer it over anything we'd assemble.
                title = card_name
                short_name = card_name if tile_card == short_card else f"{tile_card}: {card_name.split(':', 1)[1].strip()}"
            else:  # Contender Series and the like: no matchup in the name
                title = f"{short_card}: {headline['a']} vs. {headline['b']}"
                short_name = (f"{tile_card}: {_last_name(headline['a'])} "
                              f"vs. {_last_name(headline['b'])}")
            out.append(Event(
                league=league_id,
                source_id=str(e["id"]) if len(segments) == 1 else f"{e['id']}-{from_end}",
                title=title,
                subtitle=f"{card_name} — {segment}",
                start_utc=_to_iso(start),
                end_utc=_to_iso(_mma_segment_end(
                    start,
                    segments[i + 1][0] if i + 1 < len(segments) else None,
                    duration_hours,
                )),
                venue=_venue(comps[0]),
                broadcast=_broadcast(comps[0]),
                url=_event_url(e),
                status=_status(comps[-1]),  # the headliner decides when a card is over
                extra={
                    "short_name": short_name,
                    # Just the headliners: every fighter on the card would work
                    # here, but competitors is what feeds the favorites list and
                    # a dozen names per card would bury the team picker.
                    "competitors": _competitors(comps[-1]),
                    "segment": segment,
                    "card": card_name,
                    "bouts": bouts,
                },
            ))
    return out


def _mma_segments(comps: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group one event's bouts by start time → [(iso start, bouts), ...] earliest
    first. ESPN's timestamps share a format, so sorting them as strings is safe."""
    by_start: dict[str, list[dict]] = {}
    for c in comps:
        start = c.get("date") or c.get("startDate")
        if start:
            by_start.setdefault(start, []).append(c)
    return sorted(by_start.items())


def _mma_segment_end(start: str, next_start: str | None, duration_hours: float) -> str | None:
    """A segment runs until the next one starts — that's what "prelims end when
    the main card begins" means. Falls back to the league's duration for the
    last segment of the night, or if ESPN's gap is too big to be a real one."""
    if next_start:
        try:
            gap = (datetime.fromisoformat(next_start.replace("Z", "+00:00"))
                   - datetime.fromisoformat(start.replace("Z", "+00:00")))
        except ValueError:
            gap = None
        if gap is not None and timedelta(0) < gap <= timedelta(hours=8):
            return next_start
    return _add_hours(start, duration_hours)


def _bouts(comps: list[dict]) -> list[dict]:
    """[{weight, a, b}] for one segment, in ESPN's running order (headliner last)."""
    out = []
    for c in comps:
        names = [(x.get("athlete") or {}).get("displayName") or x.get("displayName")
                 for x in (c.get("competitors") or [])]
        names = [n for n in names if n]
        if len(names) < 2:
            continue
        kind = c.get("type") or {}
        out.append({
            "weight": kind.get("abbreviation") or kind.get("text") or "",
            "a": names[0],
            "b": names[1],
        })
    return out


def _last_name(name: str) -> str:
    """'Ian Machado Garry' -> 'Garry'. Tile space is scarce; the surname is what
    a reader scans for, and full names are still on the popover."""
    parts = (name or "").split()
    return parts[-1] if parts else (name or "")

# Soccer seasons aren't typed 1/2/3 — `season.type` is a season id and the
# slug names the stage — so the knockout stages are recognised by name.
# Seen: eastern-conference-playoffs---round-one, mls-cup (MLS);
# knockout-round-playoffs, round-of-16, quarterfinals, semifinals, final
# (UCL); round-of-32 … 3rd-place-match (World Cup). League play is
# regular-season / league-phase / 2025-26-laliga, none of which match.
_POSTSEASON_SLUG_RE = re.compile(
    r"playoff|play-in|post-?season|round-of|knockout|quarterfinal|semifinal"
    r"|final|3rd-place|third-place|mls-cup|wild-?card"
)
# Cricket matches carry the stage in the description: "Qualifier 1 (N),
# Indian Premier League at Dharamsala, May 26 2026" vs "70th Match (N), …".
_CRICKET_STAGE_RE = re.compile(r"^(qualifier|eliminator|semi|final|playoff)", re.I)
_SLUG_ACRONYMS = {"mls", "nba", "nhl", "nfl", "mlb", "wnba", "uefa", "fifa", "ucl"}
_SLUG_SMALL_WORDS = {"of", "the", "and", "at", "in", "vs"}


def _postseason(e: dict, comp: dict) -> str | None:
    """The round label for a postseason game ("ALDS - Game 1", "Wild Card
    Playoffs", "Round of 16 · 2nd Leg"), or None for anything else.

    US leagues type their seasons: 1 preseason, 2 regular, 3 postseason,
    and the NBA's play-in is 5 while college baseball counts regionals
    through the championship series as 3–6 — so 3 and up is postseason and
    the note headline names the round. Conference tournaments and
    conference title games stay type 2, which matches how ESPN files them.
    """
    season = e.get("season") or {}
    stype = season.get("type")
    slug = (season.get("slug") or "").lower()
    notes = [n.get("headline") for n in (comp.get("notes") or []) if n.get("headline")]
    if isinstance(stype, int) and not isinstance(stype, bool) and stype < 100:
        if stype < 3:
            return None
        return notes[0] if notes else "Postseason"
    if not slug:
        # Cricket: season.type is the league id and there's no slug.
        desc = (e.get("description") or comp.get("description") or "").split(",", 1)[0]
        stage = re.sub(r"\s*\([^)]*\)\s*$", "", desc).strip()  # drop "(N)" / "(D/N)"
        return stage if _CRICKET_STAGE_RE.match(stage) else None
    if not _POSTSEASON_SLUG_RE.search(slug):
        return None
    label = _humanize_slug(slug)
    # Two-legged ties note which leg this is ("1st Leg", "2nd Leg - X advance
    # 3-1 on aggregate"); keep the leg, not the result.
    leg = next((n.split(" - ")[0] for n in notes if n.lower().startswith(("1st leg", "2nd leg"))), None)
    return f"{label} · {leg}" if leg else label


def _humanize_slug(slug: str) -> str:
    """'eastern-conference-playoffs---round-one' -> 'Eastern Conference
    Playoffs - Round One'; 'round-of-16' -> 'Round of 16'; 'mls-cup' -> 'MLS Cup'."""
    parts = []
    for part in slug.split("---"):
        words = []
        for i, w in enumerate(part.split("-")):
            if not w:
                continue
            if w in _SLUG_ACRONYMS:
                words.append(w.upper())
            elif i > 0 and w in _SLUG_SMALL_WORDS:
                words.append(w)
            else:
                words.append(w[:1].upper() + w[1:])
        parts.append(" ".join(words))
    return " - ".join(p for p in parts if p)


def _competitors(comp: dict) -> list[dict]:
    out = []
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        athlete = c.get("athlete") or {}
        entry = {
            "name": team.get("displayName") or athlete.get("displayName") or c.get("displayName"),
            "abbr": team.get("abbreviation") or athlete.get("shortName"),
            "home_away": c.get("homeAway"),
        }
        # College feeds carry the AP/coaches poll position as curatedRank
        # (99 = unranked). Kept only when ranked, so the sidebar's "ranked
        # NCAA games" filter can test `competitors.some(c => c.rank)`.
        rank = (c.get("curatedRank") or {}).get("current")
        if isinstance(rank, int) and 1 <= rank <= 25:
            entry["rank"] = rank
        out.append(entry)
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

    def _sub_fetch(lg) -> list[Event]:
        sub_args = dict(source_args)
        sub_args.pop("leagues", None)
        sub_args["league"] = str(lg)
        try:
            return fetch_espn(sub_args, days_ahead)
        except Exception:
            # One bad/expired tour ID shouldn't kill the whole ODI fetch —
            # ESPN returns 404 once a series falls out of its data window.
            return []

    # ODI/T20I aggregate 20-25 separate tour IDs; fetching them serially is the
    # single biggest contributor to refresh time. They're independent, so run
    # them concurrently and merge.
    workers = min(len(leagues), _multi_workers()) or 1
    seen_ids: set[str] = set()
    out: list[Event] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for events in ex.map(_sub_fetch, leagues):
            for e in events:
                if e.source_id in seen_ids:
                    continue
                seen_ids.add(e.source_id)
                out.append(e)
    return out
