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
     /event/matches/{vlr_id}/{slug}/ for upcoming matches.
  3. For each upcoming match, fetch its match page once to read the
     `data-utc-ts` attribute — cached in our DB so subsequent refreshes
     don't refetch matches we've already seen.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
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

    # 2) Reuse cached UTC timestamps for matches we've already seen.
    existing = {
        e["source_id"]: e["start_utc"]
        for e in get_events(leagues=[league_id])
    }

    cutoff = datetime.now(timezone.utc) + timedelta(days=max(1, days_ahead))

    out: list[Event] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        for vlr_id, slug, region, label in targets:
            try:
                upcoming = _scrape_event_upcoming(client, vlr_id, slug)
            except Exception:
                continue  # one bad event shouldn't break the rest
            for m in upcoming:
                match_id = m["match_id"]
                # Reuse cached timestamp; otherwise fetch the match page once.
                if match_id in existing:
                    start_iso = existing[match_id]
                else:
                    start_iso = _scrape_match_utc(client, match_id)
                    time.sleep(INTER_REQUEST_DELAY)
                if not start_iso:
                    continue
                try:
                    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if start > cutoff:
                    continue
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
                    status="scheduled",
                    extra={
                        "event": label,
                        "region": region,
                        "stage": m.get("stage", ""),
                        "team_a": m["team_a"],
                        "team_b": m["team_b"],
                    },
                ))
    return out


def _scrape_event_upcoming(client: httpx.Client, vlr_id: str, slug: str) -> list[dict]:
    """Return list of {match_id, team_a, team_b, stage} for upcoming matches at one event."""
    url = f"https://www.vlr.gg/event/matches/{vlr_id}/{slug}/"
    r = client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out: list[dict] = []
    for a in soup.select("a.wf-module-item.match-item"):
        status_el = a.select_one(".ml-status")
        status = status_el.get_text(strip=True).lower() if status_el else ""
        if status in ("completed", "live"):
            continue

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

        out.append({
            "match_id": match_id,
            "team_a": team_a,
            "team_b": team_b,
            "stage": stage,
        })
    return out


_VLR_TZ = ZoneInfo("America/New_York")


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
