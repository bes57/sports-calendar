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
    group: str = "Other"                   # sidebar group, e.g. "Soccer", "Basketball"
    fetch_days_ahead: int | None = None    # override FETCH_DAYS_AHEAD for this league
                                           # (use for sports whose season is months away)


LEAGUES: list[League] = [
    League(
        id="mlb", name="MLB", full_name="Major League Baseball",
        color="#1E40AF",  # deep blue
        source="espn", source_args={"sport": "baseball", "league": "mlb"},
        duration_hours=3.0,
        group="Baseball",
    ),
    League(
        id="mlb_allstar", name="MLB All-Star", full_name="MLB All-Star Week",
        color="#FBBF24",  # gold/yellow — All-Star colors, distinct from MLB navy
        source="manual", source_args={"event_key": "mlb_allstar_2026"},
        duration_hours=3.0,
        group="Baseball",
    ),
    League(
        id="cws", name="CWS", full_name="NCAA Men's College World Series",
        color="#92400E",  # amber-800, distinct from MLB deep blue
        source="espn", source_args={
            "sport": "baseball",
            "league": "college-baseball",
            # The college-baseball feed includes regular season + regionals
            # + super regionals + CWS. CWS games are tagged with this note;
            # filter so only CWS games hit the calendar.
            "note_contains": "Men's College World Series",
        },
        duration_hours=3.0,
        group="Baseball",
        fetch_days_ahead=45,  # cover the gap until ESPN publishes the bracket (~June 8)
    ),
    League(
        id="nba", name="NBA", full_name="National Basketball Association",
        color="#DC2626",  # red
        source="espn", source_args={"sport": "basketball", "league": "nba"},
        duration_hours=3.0,
        group="Basketball",
    ),
    League(
        id="wnba", name="WNBA", full_name="Women's National Basketball Association",
        color="#F97316",  # orange
        source="espn", source_args={"sport": "basketball", "league": "wnba"},
        duration_hours=2.5,
        group="Basketball",
    ),
    League(
        id="ncaam", name="NCAAM", full_name="NCAA Men's Basketball",
        color="#4338CA",  # indigo-700
        source="espn", source_args={"sport": "basketball", "league": "mens-college-basketball"},
        duration_hours=2.5,
        group="Basketball",
    ),
    League(
        id="ncaaw", name="NCAAW", full_name="NCAA Women's Basketball",
        color="#0891B2",  # cyan-600
        source="espn", source_args={"sport": "basketball", "league": "womens-college-basketball"},
        duration_hours=2.5,
        group="Basketball",
    ),
    League(
        id="nba_draft", name="NBA Draft", full_name="NBA Draft",
        color="#A21CAF",  # bright purple — distinct from NBA red
        source="manual", source_args={"event_key": "nba_draft"},
        duration_hours=4.0,
        group="Basketball",
    ),
    League(
        id="nba_summerleague", name="NBA Summer League", full_name="NBA Summer League (Las Vegas)",
        color="#F43F5E",  # rose — distinct from NBA red, WNBA orange, Draft purple
        source="espn", source_args={"sport": "basketball", "league": "nba-summer-las-vegas"},
        duration_hours=2.0,
        group="Basketball",
    ),
    League(
        id="nhl", name="NHL", full_name="National Hockey League",
        color="#0EA5E9",  # sky blue
        source="espn", source_args={"sport": "hockey", "league": "nhl"},
        duration_hours=2.5,
        group="Hockey",
    ),
    League(
        id="ufc", name="UFC", full_name="Ultimate Fighting Championship",
        color="#7F1D1D",  # maroon — a dark red, far enough from NBA's bright red
        source="espn_mma", source_args={"sport": "mma", "league": "ufc"},
        duration_hours=3.0,  # applies to the last segment of the night; the
                             # earlier ones end when the next segment starts
        group="Combat",
    ),
    League(
        id="mma", name="MMA (PFL)", full_name="Professional Fighters League",
        color="#A16207",  # dark gold
        source="espn_mma", source_args={"sport": "mma", "league": "pfl"},
        duration_hours=3.0,
        group="Combat",
    ),
    League(
        id="f1", name="F1", full_name="Formula 1",
        color="#DB2777",  # magenta
        source="espn_f1", source_args={"sport": "racing", "league": "f1"},
        group="Racing",
    ),
    League(
        id="nfl", name="NFL", full_name="National Football League",
        color="#1E293B",  # slate-800 — distinct from MLB blue & NCAAF green
        source="espn", source_args={"sport": "football", "league": "nfl"},
        duration_hours=3.5,
        group="Football",
    ),
    League(
        id="ncaaf", name="NCAAF", full_name="NCAA Football",
        color="#4D7C0F",  # dark green (lime-700). Red-800 was near-identical
                          # to UFC maroon in a Saturday column; this is the
                          # green farthest from IPL/ODI/golf/MLS.
        source="espn", source_args={
            "sport": "football",
            "league": "college-football",
            # ESPN's scoreboard defaults to groups=80 (FBS only), which
            # silently drops every FCS game — Idaho, Weber State and the rest
            # of the Big Sky included. 90 is all of Division I (FBS + FCS).
            "groups": "90",
        },
        duration_hours=3.5,
        group="Football",
    ),
    League(
        id="ipl", name="IPL", full_name="Indian Premier League (Cricket)",
        color="#16A34A",  # green
        source="espn", source_args={"sport": "cricket", "league": "8048"},
        duration_hours=4.0,
        group="Cricket",
    ),
    League(
        id="odi", name="ODI", full_name="Men's International ODI Cricket",
        color="#047857",  # emerald-700 — distinct from IPL #16A34A
        source="espn_multi", source_args={
            "sport": "cricket",
            # ESPN has no single "ODI" league; every bilateral tour gets its
            # own league id, so we curate the set of senior-men's ODI series.
            # Update this list when new tours appear (find via the cricbuzz
            # international schedule, then probe ESPN ids for matching name).
            "leagues": [
                "23275",  # South Africa in India ODI Series 2025/26
                "23693",  # New Zealand in India ODI Series 2025/26
                "23725",  # West Indies in New Zealand ODI Series 2025/26
                "23728",  # England in New Zealand ODI Series 2025/26
                "23800",  # Sri Lanka in England ODI Series 2026
                "23807",  # India in England ODI Series 2026
                "23910",  # Sri Lanka in Pakistan ODI Series 2025/26
                "23956",  # West Indies in Bangladesh ODI Series 2025/26
                "23987",  # England in Sri Lanka ODI Series 2025/26
                "24195",  # England in South Africa ODI Series 2026/27
                "24199",  # Bangladesh in South Africa ODI Series 2026/27
                "24202",  # Australia in South Africa ODI Series 2026/27
                "24204",  # Afghanistan v Sri Lanka ODI Series 2025/26
                "24208",  # Pakistan in Bangladesh ODI Series 2025/26
                "24225",  # Afghanistan in India ODI Series 2026
                "24262",  # Afghanistan in Ireland ODI Series 2026
                "24272",  # England in Australia ODI Series 2026/27
                "24282",  # Zimbabwe in India ODI Series 2026/27
                "24285",  # Sri Lanka in India ODI Series 2026/27
                "24288",  # West Indies in India ODI Series 2026/27
                "24302",  # Australia in Zimbabwe ODI Series 2026
                "24323",  # Australia in Bangladesh ODI Series 2026
                "24378",  # Australia in Pakistan ODI Series 2026
                "24418",  # Bangladesh in Zimbabwe ODI Series 2026
                "24422",  # Sri Lanka in West Indies ODI Series 2026
                "24437",  # New Zealand in West Indies ODI Series 2026
            ],
        },
        duration_hours=8.0,  # ODIs are 50 overs/side — ~8h with breaks
        group="Cricket",
        fetch_days_ahead=365,  # tours scheduled a year+ in advance
    ),
    League(
        id="t20i", name="T20I", full_name="Men's International T20 Cricket",
        color="#84CC16",  # lime-500 — distinct from IPL green & ODI emerald
        source="espn_multi", source_args={
            "sport": "cricket",
            # Same per-tour ID architecture as ODI. Filtered to ICC Full Members
            # only (no associate-nation T20I tri-series etc.).
            "leagues": [
                "23219",  # West Indies in South Africa T20I Series 2025/26
                "23692",  # New Zealand in India T20I Series 2025/26
                "23719",  # South Africa in New Zealand T20I Series 2025/26
                "23801",  # Sri Lanka in England T20I Series 2026
                "23809",  # India in England T20I Series 2026
                "23986",  # England in Sri Lanka T20I Series 2025/26
                "24070",  # Pakistan in Sri Lanka T20I Series 2025/26
                "24085",  # Afghanistan v West Indies T20I Series 2025/26
                "24120",  # Australia in Pakistan T20I Series 2025/26
                "24197",  # Bangladesh in South Africa T20I Series 2026/27
                "24242",  # New Zealand in Bangladesh T20I Series 2026
                "24257",  # India in Ireland T20I Series 2026
                "24271",  # England in Australia T20I Series 2026/27
                "24284",  # Sri Lanka in India T20I Series 2026/27
                "24287",  # West Indies in India T20I Series 2026/27
                "24300",  # India in Zimbabwe T20I Series 2026
                "24322",  # Australia in Bangladesh T20I Series 2026
                "24417",  # Bangladesh in Zimbabwe T20I Series 2026
                "24421",  # Sri Lanka in West Indies T20I Series 2026
            ],
        },
        duration_hours=3.5,  # T20s are 20 overs/side — ~3.5h with breaks
        group="Cricket",
        fetch_days_ahead=365,
    ),
    League(
        id="valorant", name="Valorant", full_name="Valorant Champions Tour",
        color="#7C3AED",  # purple
        source="valorant", source_args={},
        duration_hours=2.0,
        group="Esports",
    ),
    League(
        id="golf", name="Golf (PGA)", full_name="PGA Tour",
        color="#065F46",  # deep green (golf fairway)
        source="espn", source_args={"sport": "golf", "league": "pga"},
        multi_day=True,
        group="Golf",
    ),
    League(
        id="mls", name="MLS", full_name="Major League Soccer",
        color="#14B8A6",  # teal
        source="espn", source_args={"sport": "soccer", "league": "usa.1"},
        duration_hours=2.0,
        group="Soccer",
    ),
    League(
        id="wc", name="World Cup", full_name="FIFA World Cup",
        color="#CA8A04",  # gold — trophy theme; distinct from MMA #A16207
        source="espn", source_args={"sport": "soccer", "league": "fifa.world"},
        duration_hours=2.0,
        group="Soccer",
    ),
]


def by_id(league_id: str) -> League | None:
    return next((l for l in LEAGUES if l.id == league_id), None)


def all_ids() -> list[str]:
    return [l.id for l in LEAGUES]


def grouped() -> list[tuple[str, list[League]]]:
    """Return [(group_name, leagues_in_group), ...] in the registry's natural
    order. Used by the sidebar to render collapsible group sections."""
    order: list[str] = []
    bucket: dict[str, list[League]] = {}
    for lg in LEAGUES:
        if lg.group not in bucket:
            bucket[lg.group] = []
            order.append(lg.group)
        bucket[lg.group].append(lg)
    return [(g, bucket[g]) for g in order]
