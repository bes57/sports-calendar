"""Refresh runner: iterates leagues, calls the right fetcher, upserts to DB."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from db import init_db, upsert_events, record_refresh, purge_old, prune_league_to
from leagues import LEAGUES, by_id
from sources import get_fetcher


def refresh_league(league_id: str, days_ahead: int) -> tuple[int, str]:
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
        # Per-league lookahead override — used for leagues whose season is
        # months away (NFL, NBA, etc.) so the schedule still surfaces during
        # the off-season window.
        effective_days = league.fetch_days_ahead or days_ahead
        events = fetcher(args, effective_days)
        n = upsert_events(events)
        # Drop DB rows for this league whose source_id is no longer in the
        # fetcher's output. Without this, source-side changes (e.g. a stricter
        # filter, a cancelled/relocated game) leave stale rows on the calendar.
        purged = prune_league_to(league.id, {e.source_id for e in events})
        msg = f"{n} events"
        if purged:
            msg += f" ({purged} pruned)"
        record_refresh(league.id, True, msg)
        return n, "ok"
    except Exception as exc:
        record_refresh(league.id, False, str(exc)[:300])
        return 0, f"error: {exc}"


def refresh_all(days_ahead: int | None = None) -> dict:
    init_db()
    if days_ahead is None:
        days_ahead = int(os.getenv("FETCH_DAYS_AHEAD", "30"))
    results = {}
    total = 0
    for league in LEAGUES:
        n, msg = refresh_league(league.id, days_ahead)
        results[league.id] = {"count": n, "message": msg}
        total += n
    # Clean up events that ended more than 2 days ago
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    purged = purge_old(cutoff)
    return {"total": total, "purged": purged, "leagues": results}


if __name__ == "__main__":
    import json
    print(json.dumps(refresh_all(), indent=2))
