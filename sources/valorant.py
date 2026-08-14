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
     row's clock, both US Eastern). Only when VLR lists no time (TBD) do we
     fall back to the match page's `data-utc-ts`, reusing our DB's copy first
     so we don't refetch matches we've already seen.
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
# misnamed `data-utc-ts` alike) as US Eastern wall-clock, DST-aware.
_VLR_TZ = ZoneInfo("America/New_York")


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
        for vlr_id, slug, region, label in targets:
            try:
                matches = _scrape_event_matches(client, vlr_id, slug)
            except Exception:
                continue  # one bad event shouldn't break the rest
            for m in matches:
                match_id = m["match_id"]
                start_iso = m["start_iso"]
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
    """Return {match_id, team_a, team_b, stage, status, start_iso} for every
    match at one event — completed and live ones included, because refresh.py
    prunes rows a fetcher stops returning.
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
            "start_iso": _combine_et(
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


def _combine_et(day: date | None, clock: str) -> str | None:
    """Combine a date header with a match row's clock ('5:00 PM') — both US
    Eastern — into canonical UTC. None when VLR lists no usable time (TBD)."""
    if day is None or not clock:
        return None
    try:  # whitespace-stripped so both "5:00 PM" and "5:00PM" parse
        t = datetime.strptime("".join(clock.upper().split()), "%I:%M%p").time()
    except ValueError:
        return None
    return to_utc_iso(datetime.combine(day, t, tzinfo=_VLR_TZ))


def _scrape_match_utc(client: httpx.Client, match_id: str) -> str | None:
    """Return the match's start time as ISO UTC, or None on failure.

    `data-utc-ts` is misnamed: vlr.gg's value is the match's wall-clock time in
    US Eastern (DST-aware), not UTC. Verified on 2026-06-06: data-utc-ts
    "2026-06-07 10:00:00" renders as "7:00 AM PDT" on the page, i.e. 10 EDT.
    """
    url = f"https://www.vlr.gg/{match_id}/"
    try:
        r = client.get(url)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    el = soup.find("div", class_="moment-tz-convert", attrs={"data-utc-ts": True})
    if not el:
        return None
    ts = el["data-utc-ts"]  # "2026-06-06 10:00:00" — Eastern, despite the attribute name
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_VLR_TZ)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat()
