"""Refresh runner: iterates leagues, calls the right fetcher, upserts to DB."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Callable

from db import init_db, upsert_events, record_refresh, purge_old, prune_league_to
from leagues import LEAGUES, by_id
from sources import get_fetcher
import kalshi


def refresh_league(league_id: str, days_ahead: int, days_behind: int = 90) -> tuple[int, str]:
    league = by_id(league_id)
    if not league:
        return 0, f"unknown league {league_id}"
    try:
        fetcher = get_fetcher(league.source)
        # Pass our internal league id + per-league behavior so the fetcher tags rows correctly
        args = dict(league.source_args)
        args["league_id_in_db"] = league.id
        args["duration_hours"] = league.duration_hours
        args["multi_day"] = league.multi_day
        # How far into the past ESPN-backed fetchers look (sources/espn.py);
        # sources that don't use it (valorant, manual) just ignore the key.
        args["days_behind"] = days_behind
        # Per-league lookahead override — used for leagues whose season is
        # months away (NFL, NBA, etc.) so the schedule still surfaces during
        # the off-season window.
        effective_days = league.fetch_days_ahead or days_ahead
        events = fetcher(args, effective_days)
        # Attach Kalshi market links (extra["kalshi_url"]) before the rows
        # land. Best-effort: a Kalshi outage just means no links this pass.
        try:
            linked = kalshi.annotate(league.id, events)
        except Exception:
            linked = 0
        n = upsert_events(events)
        # Drop DB rows for this league whose source_id is no longer in the
        # fetcher's output. Without this, source-side changes (e.g. a stricter
        # filter, a cancelled/relocated game) leave stale rows on the calendar.
        purged = prune_league_to(league.id, {e.source_id for e in events})
        msg = f"{n} events"
        if linked:
            msg += f", {linked} with Kalshi links"
        if purged:
            msg += f" ({purged} pruned)"
        record_refresh(league.id, True, msg)
        return n, "ok"
    except Exception as exc:
        record_refresh(league.id, False, str(exc)[:300])
        return 0, f"error: {exc}"


def refresh_all(
    days_ahead: int | None = None,
    days_behind: int | None = None,
    progress: Callable[[int, int, str, int], None] | None = None,
) -> dict:
    """Refresh every league. Leagues are fetched concurrently on a thread pool
    (the work is network-bound), which cuts wall-clock time roughly to the
    slowest single league instead of the sum of all of them.

    `progress`, if given, is called as each league finishes with
    (completed, total, league_id, event_count) — used to drive the SSE
    progress bar on the calendar page.
    """
    init_db()
    if days_ahead is None:
        days_ahead = int(os.getenv("FETCH_DAYS_AHEAD", "180"))
    if days_behind is None:
        days_behind = int(os.getenv("FETCH_DAYS_BEHIND", "90"))
    results: dict[str, dict] = {}
    total = 0
    total_leagues = len(LEAGUES)
    completed = 0
    # Leagues in flight at once. Each ESPN league also fans out over date
    # chunks (ESPN_CHUNK_WORKERS), so 4×3 = 12 concurrent ESPN requests —
    # 8×6 = 48 got the deploy host rate-limited.
    max_workers = max(1, int(os.getenv("REFRESH_MAX_WORKERS", "4")))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(refresh_league, league.id, days_ahead, days_behind): league
            for league in LEAGUES
        }
        for fut in as_completed(futures):
            league = futures[fut]
            try:
                n, msg = fut.result()
            except Exception as exc:  # refresh_league already traps most errors
                n, msg = 0, f"error: {exc}"
            results[league.id] = {"count": n, "message": msg}
            total += n
            completed += 1
            if progress is not None:
                try:
                    progress(completed, total_leagues, league.id, n)
                except Exception:
                    pass  # never let a progress sink break the refresh

    # Clean up events that ended before the same look-back horizon the
    # fetchers use, so this doesn't undercut what refresh_league just kept.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_behind)).isoformat()
    purged = purge_old(cutoff)
    return {"total": total, "purged": purged, "leagues": results}


if __name__ == "__main__":
    import json
    print(json.dumps(refresh_all(), indent=2))
