"""Source dispatcher.

Maps a source name (used in leagues.py) to the function that fetches and
parses events for it. Each fetcher returns a list of db.Event objects.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from db import Event

from .espn import fetch_espn, fetch_espn_f1, fetch_espn_mma, fetch_espn_multi
from .valorant import fetch_valorant
from .manual import fetch_manual


# A fetcher takes: (source_args dict, days_ahead int) -> list[Event]
Fetcher = Callable[[dict, int], list[Event]]


FETCHERS: dict[str, Fetcher] = {
    "espn": fetch_espn,
    "espn_f1": fetch_espn_f1,
    "espn_multi": fetch_espn_multi,
    "espn_mma": fetch_espn_mma,
    "valorant": fetch_valorant,
    "manual": fetch_manual,
}


def get_fetcher(source: str) -> Fetcher:
    if source not in FETCHERS:
        raise ValueError(f"Unknown source: {source}")
    return FETCHERS[source]
