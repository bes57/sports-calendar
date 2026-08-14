"""Valorant fetcher — VCT league + international tournaments only.

Only matches from events declared in `_vct_events.ALL_EVENTS` (the regional
leagues — Americas / EMEA / Pacific / CN — and international Masters /
Champions) appear in the calendar. No Game Changers, Challengers, qualifiers,
or random EWC events.

How it works:
  1. `live_events_today()` returns the events currently within their date
     window (with some pre/post-roll). Pulled from the bundled
     `_vct_events.py` so deploys (Railway, GitHub Actions, etc.) don't depend
     on any user-local file.
  2. For each live event with at least one populated VLR region URL, scrape
     /event/matches/{vlr_id}/{slug}/ for that event's matches — completed and
     live ones included, not just upcoming.
  3. Start times come from the event page itself (date header + the match
     row's clock). Only when VLR lists no time (TBD) do we fall back to the
     match page's `data-utc-ts`, reusing our DB's copy first so we don't
     refetch matches we've already seen.
  4. Every clock VLR gives us is a bare wall-clock in a timezone VLR picks per
     request, so `_site_utc_offset()` reads that timezone off a match page
     before any of them are converted. See the comment on `_FALLBACK_TZ`.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from db import Event, get_events
from timeutil import to_utc_iso
from sources._vct_events import ALL_EVENTS, live_events_today, _parse_vlr_stats_url  # noqa: F401


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.vlr.gg/",
}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
INTER_REQUEST_DELAY = 0.2  # seconds between VLR fetches (polite)

# VLR renders every time we scrape (event-page clocks and the match page's
# misnamed `data-utc-ts` alike) as a bare wall-clock — no offset, no zone —
# in ONE timezone per response. Which timezone that is depends on who asks:
# from a laptop the match page reads "5:00 PM EDT", but from Railway / GitHub
# Actions the same match rendered a UTC-5 clock, and assuming Eastern silently
# put EVERY VCT match on the calendar an hour early. So never assume the zone:
# `_site_utc_offset()` reads it back off the match page (whose header renders
# `h:mm A z` — the zone VLR actually used) and everything converts with that.
# This is only the last resort for when that probe fails.
_FALLBACK_TZ = ZoneInfo("America/New_York")

# The `z` moment-timezone prints is usually an abbreviation. Mapping each to a
# fixed offset is exact — the abbreviation already encodes DST (EDT vs EST).
# Zones a US/EU-hosted scrape could plausibly be served; anything else falls
# through to `_FALLBACK_TZ`. Ambiguous abbreviations are deliberately absent
# (IST = Ireland or India, JST vs. others), except CST: US Central in winter is
# far likelier for our hosts than the China Standard Time that shares it.
_ZONE_OFFSET_MINUTES = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "EDT": -4 * 60, "EST": -5 * 60,
    "CDT": -5 * 60, "CST": -6 * 60,
    "MDT": -6 * 60, "MST": -7 * 60,
    "PDT": -7 * 60, "PST": -8 * 60,
    "AKDT": -8 * 60, "AKST": -9 * 60, "HST": -10 * 60,
    "ADT": -3 * 60, "AST": -4 * 60,
    "NDT": -(2 * 60 + 30), "NST": -(3 * 60 + 30),
    "WET": 0, "WEST": 60, "BST": 60,
    "CET": 60, "CEST": 2 * 60,
    "EET": 2 * 60, "EEST": 3 * 60,
}

# "5:00 PM EDT" / "5:00 PM +05:30" — the match header's rendered start time.
_RENDERED_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[AP]M\s+(?P<zone>\S+)", re.I)
# Zones with no abbreviation render numerically: "+08", "-04:30", "GMT+2".
_NUMERIC_ZONE_RE = re.compile(
    r"^(?:UTC|GMT)?(?P<sign>[+-])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$", re.I
)


def fetch_valorant(source_args: dict, days_ahead: int) -> list[Event]:
    league_id = source_args.get("league_id_in_db") or "valorant"

    # 1) Collect (vlr_id, slug, region, event_label) targets for events
    #    currently in their live window.
    targets: list[tuple[str, str, str, str]] = []
    for ev in live_events_today():
        for region, url in (ev.get("regions") or {}).items():
            vlr_id, slug = _parse_vlr_stats_url(url)
            if vlr_id and slug:
                targets.append((vlr_id, slug, region, ev.get("label", "VCT")))

    if not targets:
        return []  # nothing live with a populated URL

    # 2) Rows we've already stored for this league — used as a fallback start
    #    time for matches VLR lists without one, and (further down) to keep
    #    past matches alive once their event ages out of the live window.
    cached = {e["source_id"]: e for e in get_events(leagues=[league_id])}

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max(1, days_ahead))
    # Same look-back the ESPN-backed leagues get, so finished VCT matches stay
    # on the calendar instead of vanishing the moment they end.
    floor = now - timedelta(days=max(1, int(source_args.get("days_behind") or 90)))

    out: list[Event] = []
    seen: set[str] = set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        # Scrape every event first: the match ids it yields are what we probe
        # for VLR's rendered timezone, and no wall-clock can be converted to
        # UTC until we know it.
        scraped: list[tuple[str, str, dict]] = []  # (region, event label, match)
        for vlr_id, slug, region, label in targets:
            try:
                matches = _scrape_event_matches(client, vlr_id, slug)
            except Exception:
                continue  # one bad event shouldn't break the rest
            scraped.extend((region, label, m) for m in matches)

        offset = _site_utc_offset(client, [m["match_id"] for _, _, m in scraped])

        for region, label, m in scraped:
            match_id = m["match_id"]
            start_iso = _wall_to_utc_iso(m["start_wall"], offset)
            if not start_iso:  # VLR had no time on the event page (TBD)
                prev = cached.get(match_id)
                if prev:
                    start_iso = prev["start_utc"]
                else:
                    start_iso = _scrape_match_utc(client, match_id)
                    time.sleep(INTER_REQUEST_DELAY)
            try:
                start = datetime.fromisoformat((start_iso or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if not floor <= start <= cutoff:
                continue
            seen.add(match_id)
            end = start + timedelta(hours=2)  # typical VCT BO3 length
            event_label = label if region == "International" else f"{label} — {region}"
            subtitle = (
                f"{event_label} — {m['stage']}" if m.get("stage") else event_label
            )
            out.append(Event(
                league=league_id,
                source_id=match_id,
                title=f"{m['team_a']} vs {m['team_b']}",
                subtitle=subtitle,
                start_utc=to_utc_iso(start),
                end_utc=to_utc_iso(end),
                venue=None,
                broadcast=None,
                url=f"https://www.vlr.gg/{match_id}/",
                status=m["status"],
                extra={
                    "event": label,
                    "region": region,
                    "stage": m.get("stage", ""),
                    "team_a": m["team_a"],
                    "team_b": m["team_b"],
                    # Same shape ESPN's fetcher uses (name/abbr/home_away) —
                    # db.get_teams() and the calendar's favorite-team
                    # matching both key off "competitors" specifically, so
                    # without this VCT teams can't be favorited or
                    # highlighted at all. VLR has no short code for teams,
                    # so abbr just reuses the full scraped name.
                    "competitors": [
                        {"name": m["team_a"], "abbr": m["team_a"], "home_away": "home"},
                        {"name": m["team_b"], "abbr": m["team_b"], "home_away": "away"},
                    ],
                },
            ))

    out.extend(_revive_past(cached, seen, floor, now))
    return out


def _revive_past(cached: dict[str, dict], seen: set[str],
                 floor: datetime, now: datetime) -> list[Event]:
    """Re-emit stored past matches this refresh's scrape didn't cover.

    refresh.py deletes any row whose source_id is missing from the fetcher's
    output, so the moment an event falls out of `live_events_today()`'s window
    (a split ends and the next one starts) we'd wipe its entire history. Only
    matches already in the past are revived — a *future* match VLR stopped
    listing was genuinely cancelled and should still disappear.
    """
    out: list[Event] = []
    for source_id, row in cached.items():
        if source_id in seen:
            continue
        try:
            start = datetime.fromisoformat((row["start_utc"] or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if not floor <= start < now:
            continue
        out.append(Event(
            league=row["league"],
            source_id=source_id,
            title=row["title"],
            subtitle=row["subtitle"],
            start_utc=row["start_utc"],
            end_utc=row["end_utc"],
            venue=row["venue"],
            broadcast=row["broadcast"],
            url=row["url"],
            status=row["status"],
            extra=row["extra"] or {},
            all_day=bool(row["all_day"]),
        ))
    return out


# "Fri, August 14, 2026" — the header above each day's card of matches. VLR
# appends "Today"/"Yesterday" to some of them, hence matching a prefix.
_DATE_LABEL_RE = re.compile(r"^[A-Za-z]{3},\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}")


def _scrape_event_matches(client: httpx.Client, vlr_id: str, slug: str) -> list[dict]:
    """Return {match_id, team_a, team_b, stage, status, start_wall} for every
    match at one event — completed and live ones included, because refresh.py
    prunes rows a fetcher stops returning.

    `start_wall` is naive on purpose: the event page states no timezone, so the
    caller attaches the one `_site_utc_offset()` found.
    """
    url = f"https://www.vlr.gg/event/matches/{vlr_id}/{slug}/"
    r = client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # The page alternates date header / that day's card of matches:
    #   <div class="wf-label mod-large">Fri, August 14, 2026</div>
    #   <div class="wf-card"><a class="match-item">5:00 PM …</a>…</div>
    # Pairing the two dates every match. Verified 2026-08-14: the card's clock
    # equals the match page's data-utc-ts, so this saves a fetch per match.
    day_by_card: dict[int, date] = {}
    for lab in soup.select("div.wf-label"):
        day = _parse_label_date(lab.get_text(" ", strip=True))
        if not day:
            continue
        card = lab.find_next_sibling()
        if card is not None and "wf-card" in (card.get("class") or []):
            day_by_card[id(card)] = day

    out: list[dict] = []
    for a in soup.select("a.wf-module-item.match-item"):
        href = a.get("href", "")
        m = re.match(r"^/(\d+)/", href)
        if not m:
            continue
        match_id = m.group(1)

        teams = a.select(".match-item-vs-team-name .text-of")
        if len(teams) < 2:
            continue
        team_a = teams[0].get_text(strip=True)
        team_b = teams[1].get_text(strip=True)
        if not team_a or not team_b or "TBD" in team_a or "TBD" in team_b:
            continue

        stage_el = a.select_one(".match-item-event-series")
        stage = stage_el.get_text(strip=True) if stage_el else ""

        status_el = a.select_one(".ml-status")
        raw_status = status_el.get_text(strip=True).lower() if status_el else ""
        status = {"completed": "final", "live": "in_progress"}.get(raw_status, "scheduled")

        card = a.find_parent("div", class_="wf-card")
        time_el = a.select_one(".match-item-time")
        out.append({
            "match_id": match_id,
            "team_a": team_a,
            "team_b": team_b,
            "stage": stage,
            "status": status,
            "start_wall": _combine_wall(
                day_by_card.get(id(card)) if card is not None else None,
                time_el.get_text(strip=True) if time_el else "",
            ),
        })
    return out


def _parse_label_date(text: str) -> date | None:
    """'Fri, August 14, 2026 Today' → date(2026, 8, 14); None if not a date."""
    m = _DATE_LABEL_RE.match(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.group(0).split()), "%a, %B %d, %Y").date()
    except ValueError:
        return None


def _combine_wall(day: date | None, clock: str) -> datetime | None:
    """Combine a date header with a match row's clock ('5:00 PM') into the naive
    wall-clock VLR printed. None when VLR lists no usable time (TBD)."""
    if day is None or not clock:
        return None
    try:  # whitespace-stripped so both "5:00 PM" and "5:00PM" parse
        t = datetime.strptime("".join(clock.upper().split()), "%I:%M%p").time()
    except ValueError:
        return None
    return datetime.combine(day, t)


def _wall_to_utc_iso(wall: datetime | None, offset: timedelta | None) -> str | None:
    """Pin one of VLR's naive wall-clocks to the timezone it was rendered in."""
    if wall is None:
        return None
    tz = timezone(offset) if offset is not None else _FALLBACK_TZ
    return to_utc_iso(wall.replace(tzinfo=tz))


def _zone_offset(token: str) -> timedelta | None:
    """'EDT' → -4h, '+05:30' → 5h30m, 'GMT+2' → 2h. None if unrecognized."""
    tok = (token or "").strip().rstrip(".").upper()
    if tok in _ZONE_OFFSET_MINUTES:
        return timedelta(minutes=_ZONE_OFFSET_MINUTES[tok])
    m = _NUMERIC_ZONE_RE.match(tok)
    if not m:
        return None
    minutes = int(m.group("h")) * 60 + int(m.group("m") or 0)
    return timedelta(minutes=-minutes if m.group("sign") == "-" else minutes)


def _site_utc_offset(client: httpx.Client, match_ids: list[str]) -> timedelta | None:
    """The UTC offset VLR is rendering its wall-clocks in, read off a match page.

    Every clock on the event pages is bare — "5:00 PM", no zone — and VLR
    serves that clock in a timezone of its own choosing, so guessing it wrong
    shifts every VCT match on the calendar by the difference. The match page is
    the one place VLR names the zone: its header renders `h:mm A z`
    ("5:00 PM EDT"). One probe per refresh sets the zone for every clock.

    Returns None (caller falls back to Eastern) if no candidate page yields a
    zone we recognize — a few ids are tried so one dead match page can't
    silently skew a whole refresh.
    """
    for match_id in match_ids[:3]:
        _, offset = _scrape_match_header(client, match_id)
        time.sleep(INTER_REQUEST_DELAY)
        if offset is not None:
            return offset
    return None


def _scrape_match_header(
    client: httpx.Client, match_id: str
) -> tuple[datetime | None, timedelta | None]:
    """(naive start wall-clock, rendered UTC offset) from a match page.

    `data-utc-ts` is misnamed: the value is the match's wall-clock time in
    whatever zone VLR rendered the page in, not UTC. Verified 2026-08-14:
    data-utc-ts "2026-08-14 17:00:00" renders as "5:00 PM EDT" here — while the
    same page fetched from our deploy host rendered a UTC-5 clock instead.
    """
    try:
        r = client.get(f"https://www.vlr.gg/{match_id}/")
        r.raise_for_status()
    except Exception:
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")

    wall = offset = None
    for el in soup.select("div.moment-tz-convert[data-utc-ts]"):
        if wall is None:
            try:
                wall = datetime.strptime(el["data-utc-ts"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        # Only the `z`-formatted copy names the zone; its sibling is date-only.
        if offset is None and "z" in (el.get("data-moment-format") or ""):
            m = _RENDERED_TIME_RE.search(el.get_text(" ", strip=True))
            if m:
                offset = _zone_offset(m.group("zone"))
        if wall is not None and offset is not None:
            break
    return wall, offset


def _scrape_match_utc(client: httpx.Client, match_id: str) -> str | None:
    """Return the match's start time as ISO UTC, or None on failure. Used only
    for matches the event page listed without a time, so it reads both the
    clock and the zone off that match's own page."""
    wall, offset = _scrape_match_header(client, match_id)
    return _wall_to_utc_iso(wall, offset)
