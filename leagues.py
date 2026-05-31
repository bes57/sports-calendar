"""Central league registry.

Each league has a stable id (used in DB), a display name, a color (used on the
calendar), and a 'source' tuple that tells the dispatcher which fetcher and
which parameters to use.

To add a new league later:
  1. Add an entry below.
  2. If it uses a brand-new source, add the fetcher in sources/ and register it
     in sources/__init__.py.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    id: str                                # short stable id, e.g. "mlb"
    name: str                              # display name, e.g. "MLB"
    full_name: str                         # long display
    color: str                             # hex color used on the calendar
    source: str                            # which fetcher to use
    source_args: dict                      # args passed to the fetcher
    duration_hours: float | None = None    # event length; None = sport default
    multi_day: bool = False                # if True, render events as all-day spans


LEAGUES: list[League] = [
    League(
        id="mlb", name="MLB", full_name="Major League Baseball",
        color="#1E40AF",  # deep blue
        source="espn", source_args={"sport": "baseball", "league": "mlb"},
        duration_hours=3.0,
    ),
    League(
        id="nba", name="NBA", full_name="National Basketball Association",
        color="#DC2626",  # red
        source="espn", source_args={"sport": "basketball", "league": "nba"},
        duration_hours=3.0,
    ),
    League(
        id="wnba", name="WNBA", full_name="Women's National Basketball Association",
        color="#F97316",  # orange
        source="espn", source_args={"sport": "basketball", "league": "wnba"},
        duration_hours=2.5,
    ),
    League(
        id="nhl", name="NHL", full_name="National Hockey League",
        color="#0EA5E9",  # sky blue
        source="espn", source_args={"sport": "hockey", "league": "nhl"},
        duration_hours=2.5,
    ),
    League(
        id="ufc", name="UFC", full_name="Ultimate Fighting Championship",
        color="#7F1D1D",  # maroon
        source="espn", source_args={"sport": "mma", "league": "ufc"},
        duration_hours=3.0,  # main card only; prelims start ~2h earlier
    ),
    League(
        id="mma", name="MMA (PFL)", full_name="Professional Fighters League",
        color="#A16207",  # dark gold
        source="espn", source_args={"sport": "mma", "league": "pfl"},
        duration_hours=3.0,
    ),
    League(
        id="f1", name="F1", full_name="Formula 1",
        color="#DB2777",  # magenta
        source="espn_f1", source_args={"sport": "racing", "league": "f1"},
    ),
    League(
        id="ipl", name="IPL", full_name="Indian Premier League (Cricket)",
        color="#16A34A",  # green
        source="espn", source_args={"sport": "cricket", "league": "8048"},
        duration_hours=4.0,
    ),
    League(
        id="valorant", name="Valorant", full_name="Valorant Champions Tour",
        color="#7C3AED",  # purple
        source="valorant", source_args={},
        duration_hours=2.0,
    ),
    League(
        id="golf", name="Golf (PGA)", full_name="PGA Tour",
        color="#065F46",  # deep green (golf fairway)
        source="espn", source_args={"sport": "golf", "league": "pga"},
        multi_day=True,
    ),
    League(
        id="mls", name="MLS", full_name="Major League Soccer",
        color="#14B8A6",  # teal
        source="espn", source_args={"sport": "soccer", "league": "usa.1"},
        duration_hours=2.0,
    ),
    League(
        id="nba_draft", name="NBA Draft", full_name="NBA Draft",
        color="#A21CAF",  # bright purple — distinct from NBA red
        source="manual", source_args={"event_key": "nba_draft"},
        duration_hours=4.0,
    ),
]


def by_id(league_id: str) -> League | None:
    return next((l for l in LEAGUES if l.id == league_id), None)


def all_ids() -> list[str]:
    return [l.id for l in LEAGUES]
