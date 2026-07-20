"""SQLite storage for events.

Single table, unified across leagues. Each event has a composite primary key of
(league, source_id) so re-fetching is idempotent — upserts replace stale data
without creating duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from timeutil import to_utc_iso, is_canonical, utc_now_iso


DB_PATH = Path(__file__).parent / "data" / "events.db"


@dataclass
class Event:
    league: str          # league id (e.g. "mlb")
    source_id: str       # id within the source system
    title: str           # e.g. "Yankees @ Red Sox"
    subtitle: str        # context, e.g. "Game 3, ALCS" or "FP1, Monaco GP"
    start_utc: str       # ISO 8601 UTC, e.g. "2026-05-30T19:30:00+00:00"
    end_utc: str | None  # ISO 8601 UTC or None
    venue: str | None
    broadcast: str | None
    url: str | None
    status: str          # "scheduled", "in_progress", "final", "postponed"
    extra: dict          # sport-specific metadata
    all_day: bool = False  # render as multi-day banner in the calendar


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    league       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    subtitle     TEXT NOT NULL DEFAULT '',
    start_utc    TEXT NOT NULL,
    end_utc      TEXT,
    venue        TEXT,
    broadcast    TEXT,
    url          TEXT,
    status       TEXT NOT NULL DEFAULT 'scheduled',
    extra        TEXT NOT NULL DEFAULT '{}',
    all_day      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (league, source_id)
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_utc);
CREATE INDEX IF NOT EXISTS idx_events_league ON events(league);

CREATE TABLE IF NOT EXISTS refresh_log (
    league       TEXT PRIMARY KEY,
    last_run     TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    message      TEXT
);
"""


def init_db() -> None:
    """Create schema if missing, then normalize any pre-existing rows so the
    canonical-format invariant holds across older DBs."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
    migrate_existing_timestamps_to_canonical()


def migrate_existing_timestamps_to_canonical() -> int:
    """One-shot pass that re-writes any non-canonical start_utc/end_utc rows.
    Cheap to run on every startup — only updates rows that need it."""
    fixed = 0
    with connect() as conn:
        rows = conn.execute("SELECT rowid, start_utc, end_utc FROM events").fetchall()
        for r in rows:
            s_new = to_utc_iso(r["start_utc"])
            e_new = to_utc_iso(r["end_utc"]) if r["end_utc"] else None
            if s_new != r["start_utc"] or (r["end_utc"] and e_new != r["end_utc"]):
                conn.execute(
                    "UPDATE events SET start_utc = ?, end_utc = ? WHERE rowid = ?",
                    (s_new, e_new, r["rowid"]),
                )
                fixed += 1
    return fixed


@contextmanager
def connect():
    # WAL + a generous busy_timeout let the parallel per-league refreshes
    # (refresh_all runs leagues on a thread pool) write concurrently without
    # tripping "database is locked": WAL allows readers alongside one writer,
    # and busy_timeout makes a contending writer wait its turn instead of
    # erroring out immediately.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_events(events: Iterable[Event]) -> int:
    now = utc_now_iso()
    # Defense in depth: force every timestamp through to_utc_iso() at the
    # write boundary. Any caller that forgot to normalize gets caught here.
    rows = [
        (
            e.league, e.source_id, e.title, e.subtitle,
            to_utc_iso(e.start_utc), to_utc_iso(e.end_utc),
            e.venue, e.broadcast, e.url, e.status, json.dumps(e.extra),
            1 if e.all_day else 0, now,
        )
        for e in events
    ]
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO events
                (league, source_id, title, subtitle, start_utc, end_utc,
                 venue, broadcast, url, status, extra, all_day, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league, source_id) DO UPDATE SET
                title = excluded.title,
                subtitle = excluded.subtitle,
                start_utc = excluded.start_utc,
                end_utc = excluded.end_utc,
                venue = excluded.venue,
                broadcast = excluded.broadcast,
                url = excluded.url,
                status = excluded.status,
                extra = excluded.extra,
                all_day = excluded.all_day,
                updated_at = excluded.updated_at
            """,
            rows,
        )
    return len(rows)


def get_events(
    start_iso: str | None = None,
    end_iso: str | None = None,
    leagues: list[str] | None = None,
) -> list[dict]:
    """Return events whose [start, end] interval overlaps [start_iso, end_iso].

    Bounds are normalized via timeutil.to_utc_iso() so callers can pass any
    reasonable ISO form (with/without seconds, with/without Z, with any
    offset) and SQL string comparison still works.
    """
    start_iso = to_utc_iso(start_iso)
    end_iso = to_utc_iso(end_iso)
    q = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if end_iso:
        q += " AND start_utc < ?"
        params.append(end_iso)
    if start_iso:
        q += " AND COALESCE(end_utc, start_utc) >= ?"
        params.append(start_iso)
    if leagues:
        placeholders = ",".join("?" * len(leagues))
        q += f" AND league IN ({placeholders})"
        params.extend(leagues)
    q += " ORDER BY start_utc ASC"
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = json.loads(d.get("extra") or "{}")
        out.append(d)
    return out


def get_teams() -> list[dict]:
    """Distinct (league, abbr, name) triples derived from every stored event's
    `extra.competitors`. There's no separate team-roster table — team lists
    come straight from whatever ESPN has already put in front of us, so this
    stays correct without a second data source to maintain. Leagues whose
    fetcher doesn't populate competitors (F1, Valorant, manual events) just
    contribute nothing here."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    with connect() as conn:
        rows = conn.execute("SELECT league, extra FROM events").fetchall()
    for r in rows:
        try:
            extra = json.loads(r["extra"] or "{}")
        except (TypeError, ValueError):
            continue
        for c in extra.get("competitors") or []:
            abbr = c.get("abbr")
            name = c.get("name")
            if not abbr or not name:
                continue
            key = (r["league"], abbr)
            if key in seen:
                continue
            seen.add(key)
            out.append({"league": r["league"], "abbr": abbr, "name": name})
    out.sort(key=lambda t: (t["league"], t["name"]))
    return out


def record_refresh(league: str, ok: bool, message: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO refresh_log (league, last_run, ok, message)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(league) DO UPDATE SET
                last_run = excluded.last_run,
                ok = excluded.ok,
                message = excluded.message
            """,
            (league, now, 1 if ok else 0, message),
        )


def get_refresh_status() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM refresh_log").fetchall()
    return [dict(r) for r in rows]


def purge_old(before_iso: str) -> int:
    """Delete events that ended before the given ISO timestamp."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM events WHERE COALESCE(end_utc, start_utc) < ?",
            (before_iso,),
        )
        return cur.rowcount


def prune_league_to(league_id: str, keep_source_ids: set[str]) -> int:
    """Delete rows for `league_id` whose source_id is not in `keep_source_ids`.
    Used after a refresh to drop events the upstream source no longer returns
    (e.g. when a stricter filter has been applied, or a game was cancelled)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT source_id FROM events WHERE league = ?",
            (league_id,),
        ).fetchall()
        to_delete = [r["source_id"] for r in rows if r["source_id"] not in keep_source_ids]
        if not to_delete:
            return 0
        placeholders = ",".join("?" * len(to_delete))
        cur = conn.execute(
            f"DELETE FROM events WHERE league = ? AND source_id IN ({placeholders})",
            (league_id, *to_delete),
        )
        return cur.rowcount
