"""FastAPI app: serves the calendar page, settings, and JSON APIs."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import init_db, get_events, get_refresh_status
from leagues import LEAGUES, by_id, grouped as grouped_leagues
from refresh import refresh_all, refresh_league
from digest import build_digest_text, build_digest_html, build_digest_sms, send_digest
from scheduler import start_scheduler


from timeutil import to_utc_iso as _to_utc_iso

# Backwards-compat alias: every ISO field on the wire is canonical UTC,
# produced via the single normalizer in timeutil.
_normalize_iso = _to_utc_iso

load_dotenv()
BASE_DIR = Path(__file__).parent

app = FastAPI(title="K-Cal")


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles wrapper that forbids browser caching. Without this, a soft
    reload can serve cached HTML referencing a stale asset_version, which then
    pulls a stale JS from cache and edits look like no-ops in the browser."""

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Mirror the static no-cache policy for HTML responses so cached HTML
    can't pin the page to an obsolete asset_version."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    # Kick scheduler — runs periodic refresh and the morning digest
    start_scheduler()
    # On a fresh container (e.g. Railway redeploy) the SQLite DB is empty,
    # which would leave the calendar blank until the next periodic refresh.
    # Fire one refresh in the background so data shows up immediately.
    # The DB only has 1 row → skip the cold refresh (likely a hot reload).
    import asyncio
    from refresh import refresh_all
    try:
        from db import connect
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) as n FROM events").fetchone()
            existing = row["n"] if row else 0
    except Exception:
        existing = 0
    if existing < 5:
        # Run in a thread so we don't block startup (uvicorn waits for the
        # startup handler before serving). 30+s ESPN fetches would otherwise
        # delay first request well past Railway's healthcheck timeout.
        asyncio.get_event_loop().run_in_executor(None, refresh_all)


# --- Pages ---

def _asset_version() -> str:
    """Stamp loaded onto /static asset URLs so edits to JS/CSS bust browser cache."""
    try:
        css = (BASE_DIR / "static" / "app.css").stat().st_mtime
        js = (BASE_DIR / "static" / "app.js").stat().st_mtime
        return str(int(max(css, js)))
    except OSError:
        return "0"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "leagues": LEAGUES,
            "grouped_leagues": grouped_leagues(),
            "leagues_json": [
                {"id": l.id, "name": l.name, "color": l.color} for l in LEAGUES
            ],
            "tz": os.getenv("TZ", "America/New_York"),
            "asset_version": _asset_version(),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    status = get_refresh_status()
    status_by_league = {s["league"]: s for s in status}
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "leagues": LEAGUES,
            "status": status_by_league,
            "tz": os.getenv("TZ", "America/New_York"),
            "digest_time": os.getenv("DIGEST_TIME", "07:00"),
            "digest_email": os.getenv("DIGEST_EMAIL_TO", ""),
            "asset_version": _asset_version(),
        },
    )


# --- JSON APIs ---

@app.get("/api/events")
async def api_events(
    start: str | None = Query(None, description="ISO datetime; defaults to now"),
    end: str | None = Query(None, description="ISO datetime; defaults to start + 60d"),
    leagues: str | None = Query(None, description="Comma-separated league ids"),
):
    """Return events in FullCalendar's expected shape."""
    league_list = [s.strip() for s in leagues.split(",")] if leagues else None
    # Normalize client bounds (may carry a non-UTC offset like -04:00) into UTC
    # ISO so SQL string-compare against `start_utc` (+00:00) is correct.
    rows = get_events(
        start_iso=_to_utc_iso(start),
        end_iso=_to_utc_iso(end),
        leagues=league_list,
    )
    out = []
    for r in rows:
        lg = by_id(r["league"])
        color = lg.color if lg else "#6B7280"
        full_title = r["title"]
        extra = r.get("extra") or {}
        short_title = extra.get("short_name") or full_title
        out.append({
            "id": f"{r['league']}:{r['source_id']}",
            "title": short_title,
            "start": _normalize_iso(r["start_utc"]),
            "end": _normalize_iso(r.get("end_utc")),
            "allDay": bool(r.get("all_day")),
            "backgroundColor": color,
            "borderColor": color,
            "extendedProps": {
                "league": r["league"],
                "leagueName": lg.name if lg else r["league"],
                "fullTitle": full_title,
                "subtitle": r["subtitle"],
                "venue": r["venue"],
                "broadcast": r["broadcast"],
                "url": r["url"],
                "status": r["status"],
                "note": _league_note(r["league"]),
            },
        })
    return JSONResponse(out)


# Per-league context shown in the event popover. Kept here (vs the DB) so we
# can edit the wording without re-fetching anything.
_LEAGUE_NOTES = {
    "ufc": (
        "Time shown is the main card start. "
        "Prelims typically start ~2 hours earlier; early prelims another ~2 hours before that."
    ),
    "mma": (
        "Time shown is the main card start. "
        "Prelims typically start ~2 hours earlier."
    ),
}


def _league_note(league_id: str) -> str | None:
    """League-level note shown on the popover. Per-event golf tee times are
    omitted because ESPN doesn't publish them in the scoreboard feed —
    they're tournament-specific and only get posted the week of the event
    on pgatour.com."""
    return _LEAGUE_NOTES.get(league_id)


@app.get("/api/leagues")
async def api_leagues():
    return [
        {"id": l.id, "name": l.name, "full_name": l.full_name, "color": l.color}
        for l in LEAGUES
    ]


@app.post("/api/refresh")
async def api_refresh(background_tasks: BackgroundTasks, league: str | None = None):
    """Trigger a refresh. Returns immediately; refresh runs in background."""
    if league:
        background_tasks.add_task(
            refresh_league,
            league,
            int(os.getenv("FETCH_DAYS_AHEAD", "180")),
        )
        return {"ok": True, "queued": league}
    background_tasks.add_task(refresh_all)
    return {"ok": True, "queued": "all"}


@app.post("/api/refresh/sync")
async def api_refresh_sync(league: str | None = None):
    """Refresh and wait — useful for manual testing."""
    if league:
        n, msg = refresh_league(league, int(os.getenv("FETCH_DAYS_AHEAD", "180")))
        return {"league": league, "count": n, "message": msg}
    return refresh_all()


@app.get("/api/refresh/stream")
async def api_refresh_stream(league: str | None = None):
    """Run a refresh and stream progress as Server-Sent Events so the calendar
    page can show a live progress bar. Emits one `progress` payload per league
    as it completes, then a final `done` (or `error`) payload.

    The refresh itself is blocking/threaded, so we run it in an executor and
    bridge progress back onto the event loop through an asyncio.Queue.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue()

    def emit(payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, payload)

    def work() -> None:
        try:
            if league:
                n, _msg = refresh_league(
                    league, int(os.getenv("FETCH_DAYS_AHEAD", "180"))
                )
                emit({"type": "progress", "done": 1, "total": 1,
                      "league": league, "count": n})
                emit({"type": "done", "total_events": n})
            else:
                result = refresh_all(progress=lambda done, total, lg, n: emit(
                    {"type": "progress", "done": done, "total": total,
                     "league": lg, "count": n}
                ))
                emit({"type": "done", "total_events": result.get("total", 0)})
        except Exception as exc:  # surface failures to the client
            emit({"type": "error", "message": str(exc)[:300]})

    loop.run_in_executor(None, work)

    async def streamer():
        # Tell the client how many leagues to expect so the bar starts
        # determinate at 0% rather than guessing.
        total = 1 if league else len(LEAGUES)
        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"
        while True:
            payload = await queue.get()
            yield f"data: {json.dumps(payload)}\n\n"
            if payload.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        streamer(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx/Railway)
        },
    )


@app.get("/api/refresh/status")
async def api_refresh_status():
    return get_refresh_status()


@app.get("/api/digest/preview", response_class=PlainTextResponse)
async def api_digest_preview(format: str = "text"):
    if format == "html":
        return HTMLResponse(build_digest_html())
    if format == "sms":
        return PlainTextResponse(build_digest_sms())
    return PlainTextResponse(build_digest_text())


@app.post("/api/digest/send")
async def api_digest_send():
    result = send_digest()
    return result
