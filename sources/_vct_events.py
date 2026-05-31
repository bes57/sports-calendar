"""Bundled VCT event registry + live-window helpers.

Originally lived in /Users/benny_es1/PythonTest/MoreTestingMaybeFiles.py (a
local-only file). Inlined here so K-Cal is portable — Railway, GitHub Actions
and any other deploy target can fetch Valorant matches without needing the
external project on disk.

To add a new VCT event, append an entry to `ALL_EVENTS` with the correct
VLR IDs and start/end dates. The Valorant fetcher uses `live_events_today()`
to decide which events to scrape.
"""

from __future__ import annotations

import datetime as _dt
import re as _re


ALL_EVENTS = [
    # ── 2026 ──────────────────────────────────────────────────────────
    {
        "id": "2026_champions",
        "label": "2026 Champions",
        "year": 2026,
        "start": "2026-09-24",
        "end":   "2026-10-18",
        "regions": {
            # Fill VLR URL when the event is posted on VLR. RefreshLiveData
            # will skip entries whose region URL is empty.
            "International": "",
        },
    },
    {
        "id": "2026_stage2",
        "label": "2026 Stage 2",
        "year": 2026,
        "start": "2026-07-15",
        "end":   "2026-09-06",
        "regions": {
            "Americas": "",
            "EMEA":     "",
            "Pacific":  "",
            "CN":       "",
        },
    },
    {
        "id": "2026_masters_london",
        "label": "2026 Masters London",
        "year": 2026,
        "start": "2026-06-05",
        "end":   "2026-06-21",
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2765/valorant-masters-london-2026",
        },
    },
    {
        "id": "2026_stage1",
        "label": "2026 Stage 1",
        "year": 2026,
        "start": "2026-04-01",
        "end":   "2026-05-25",
        "regions": {
            "EMEA":     "https://www.vlr.gg/event/stats/2863/vct-2026-emea-stage-1",
            "Americas": "https://www.vlr.gg/event/stats/2860/vct-2026-americas-stage-1",
            "Pacific":  "https://www.vlr.gg/event/stats/2775/vct-2026-pacific-stage-1",
            "CN":       "https://www.vlr.gg/event/stats/2864/vct-2026-china-stage-1",
        },
    },
    {
        "id": "2026_masters_santiago",
        "label": "2026 Masters Santiago",
        "year": 2026,
        "start": "2026-02-28",
        "end":   "2026-03-15",
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2760/valorant-masters-santiago-2026",
        },
    },
    {
        "id": "2026_china_kickoff",
        "label": "2026 China Kickoff",
        "year": 2026,
        "start": "2026-01-21",
        "end":   "2026-02-09",
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2685/vct-2026-china-kickoff",
        },
    },
    {
        "id": "2026_kickoff",
        "label": "2026 Kickoff",
        "year": 2026,
        "start": "2026-01-15",
        "end":   "2026-02-16",
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2682/vct-2026-americas-kickoff",
            "EMEA":     "https://www.vlr.gg/event/stats/2684/vct-2026-emea-kickoff",
            "Pacific":  "https://www.vlr.gg/event/stats/2683/vct-2026-pacific-kickoff",
        },
    },
    # ── 2025 ──────────────────────────────────────────────────────────
    {
        "id": "2025_champions",
        "label": "2025 Champions",
        "year": 2025,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2283/valorant-champions-2025",
        },
    },
    {
        "id": "2025_stage2",
        "label": "2025 Stage 2",
        "year": 2025,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2501/vct-2025-americas-stage-2",
            "EMEA":     "https://www.vlr.gg/event/stats/2498/vct-2025-emea-stage-2",
            "Pacific":  "https://www.vlr.gg/event/stats/2500/vct-2025-pacific-stage-2",
        },
    },
    {
        "id": "2025_china_stage2",
        "label": "2025 China Stage 2",
        "year": 2025,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2499/vct-2025-china-stage-2",
        },
    },
    {
        "id": "2025_masters_toronto",
        "label": "2025 Masters Toronto",
        "year": 2025,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2282/valorant-masters-toronto-2025",
        },
    },
    {
        "id": "2025_stage1",
        "label": "2025 Stage 1",
        "year": 2025,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2347/vct-2025-americas-stage-1",
            "EMEA":     "https://www.vlr.gg/event/stats/2380/vct-2025-emea-stage-1",
            "Pacific":  "https://www.vlr.gg/event/stats/2379/vct-2025-pacific-stage-1",
        },
    },
    {
        "id": "2025_china_stage1",
        "label": "2025 China Stage 1",
        "year": 2025,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2359/vct-2025-china-stage-1",
        },
    },
    {
        "id": "2025_masters_bangkok",
        "label": "2025 Masters Bangkok",
        "year": 2025,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2281/valorant-masters-bangkok-2025",
        },
    },
    {
        "id": "2025_kickoff",
        "label": "2025 Kickoff",
        "year": 2025,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2274/vct-2025-americas-kickoff",
            "EMEA":     "https://www.vlr.gg/event/stats/2276/vct-2025-emea-kickoff",
            "Pacific":  "https://www.vlr.gg/event/stats/2277/champions-tour-2025-pacific-kickoff",
        },
    },
    {
        "id": "2025_china_kickoff",
        "label": "2025 China Kickoff",
        "year": 2025,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2275/vct-2025-china-kickoff",
        },
    },
    # ── 2024 ──────────────────────────────────────────────────────────
    {
        "id": "2024_champions",
        "label": "2024 Champions",
        "year": 2024,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/2097/valorant-champions-2024",
        },
    },
    {
        "id": "2024_stage2",
        "label": "2024 Stage 2",
        "year": 2024,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2095/champions-tour-2024-americas-stage-2",
            "EMEA":     "https://www.vlr.gg/event/stats/2094/champions-tour-2024-emea-stage-2",
            "Pacific":  "https://www.vlr.gg/event/stats/2005/champions-tour-2024-pacific-stage-2",
        },
    },
    {
        "id": "2024_china_stage2",
        "label": "2024 China Stage 2",
        "year": 2024,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2096/champions-tour-2024-china-stage-2",
        },
    },
    {
        "id": "2024_stage1",
        "label": "2024 Stage 1",
        "year": 2024,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/2004/champions-tour-2024-americas-stage-1",
            "EMEA":     "https://www.vlr.gg/event/stats/1998/champions-tour-2024-emea-stage-1",
            "Pacific":  "https://www.vlr.gg/event/stats/2002/champions-tour-2024-pacific-stage-1",
        },
    },
    {
        "id": "2024_china_stage1",
        "label": "2024 China Stage 1",
        "year": 2024,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/2006/champions-tour-2024-china-stage-1",
        },
    },
    {
        "id": "2024_masters_shanghai",
        "label": "2024 Masters Shanghai",
        "year": 2024,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/1999/champions-tour-2024-masters-shanghai",
        },
    },
    {
        "id": "2024_masters_madrid",
        "label": "2024 Masters Madrid",
        "year": 2024,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/1921/champions-tour-2024-masters-madrid",
        },
    },
    {
        "id": "2024_kickoff",
        "label": "2024 Kickoff",
        "year": 2024,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/1923/champions-tour-2024-americas-kickoff",
            "EMEA":     "https://www.vlr.gg/event/stats/1925/champions-tour-2024-emea-kickoff",
            "Pacific":  "https://www.vlr.gg/event/stats/1924/champions-tour-2024-pacific-kickoff",
        },
    },
    {
        "id": "2024_china_kickoff",
        "label": "2024 China Kickoff",
        "year": 2024,
        "regions": {
            "CN": "https://www.vlr.gg/event/stats/1926/champions-tour-2024-china-kickoff",
        },
    },
    # ── 2023 ──────────────────────────────────────────────────────────
    {
        "id": "2023_champions",
        "label": "2023 Champions",
        "year": 2023,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/1657/valorant-champions-2023",
        },
    },
    {
        "id": "2023_masters_tokyo",
        "label": "2023 Masters Tokyo",
        "year": 2023,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/1494/champions-tour-2023-masters-tokyo",
        },
    },
    {
        "id": "2023_league",
        "label": "2023 League",
        "year": 2023,
        "regions": {
            "Americas": "https://www.vlr.gg/event/stats/1189/champions-tour-2023-americas-league",
            "EMEA":     "https://www.vlr.gg/event/stats/1190/champions-tour-2023-emea-league",
            "Pacific":  "https://www.vlr.gg/event/stats/1191/champions-tour-2023-pacific-league",
        },
    },
    {
        "id": "2023_lock_in",
        "label": "2023 LOCK//IN",
        "year": 2023,
        "regions": {
            "International": "https://www.vlr.gg/event/stats/1188/champions-tour-2023-lock-in-s-o-paulo",
        },
    },
]



def _parse_vlr_stats_url(url):
    """Extract (vlr_id, slug) from a VLR stats URL — returns (None, None) if blank."""
    if not url:
        return None, None
    m = _re.search(r"/event/(?:stats|matches)/(\d+)/([^/?#]+)", url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def live_events_today(today=None, lead_days=7, trail_days=3):
    """Return events whose date window contains today (with `lead_days` pre-roll
    and `trail_days` post-roll). If nothing is currently within window, returns
    the single most-recent past event so upcoming-match scraping always has a
    surface to listen on between splits."""
    if today is None:
        today = _dt.date.today()
    elif isinstance(today, str):
        today = _dt.date.fromisoformat(today)

    dated = []
    for ev in ALL_EVENTS:
        s = ev.get("start"); e = ev.get("end")
        if not (s and e):
            continue
        try:
            sd = _dt.date.fromisoformat(s)
            ed = _dt.date.fromisoformat(e)
        except ValueError:
            continue
        dated.append((sd, ed, ev))

    live = [ev for sd, ed, ev in dated
            if (sd - _dt.timedelta(days=lead_days)) <= today
            <= (ed + _dt.timedelta(days=trail_days))]
    if live:
        return live

    past = [(sd, ed, ev) for sd, ed, ev in dated if ed <= today]
    if past:
        past.sort(key=lambda t: t[1], reverse=True)
        return [past[0][2]]
    return []
