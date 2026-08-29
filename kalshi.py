"""Kalshi market links.

Kalshi (kalshi.com) runs a prediction market on the winner of most games we
track. Each refresh asks Kalshi's public API for the open events in a
league's series, matches them to our events, and stores the market page URL
in `extra["kalshi_url"]`; the popover shows it next to the source link.
Leagues with a series but no per-game match fall back to the series page.

Matching is best-effort and never breaks a refresh: any failure (Kalshi
down, rate limit, unparseable ticker) just leaves the link off — or keeps the
URL a previous refresh already found for that event.

How Kalshi names things (verified 2026-08-28 against the live API):
  KXNCAAFGAME-26AUG28IDHOCP        "Idaho vs Cal Poly"          IDHO vs CP
  KXMLBGAME-26AUG291305BOSNYYG1    "Boston vs New York Y: Game 1"
  KXUFCFIGHT-26AUG29MENNEL         "Fight Night: Meng vs Nelson"
  KXF1RACE-ITAGP26                 "Italian Grand Prix Winner"
  KXVALORANTGAME-26AUG290400PRVAR  "Paper Rex vs. VARREL"
Team tickers are {SERIES}-{YY}{MON}{DD}[{HHMM Eastern}]{ABBR_A}{ABBR_B}[G#]
with (mostly) ESPN's own team abbreviations; the date is the Eastern date of
the game. Web pages live at kalshi.com/markets/{series}/{slug}/{event}.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

from db import Event, get_events

API = "https://api.elections.kalshi.com/trade-api/v2"
WEB = "https://kalshi.com/markets"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_ET = ZoneInfo("America/New_York")

# league id -> (series ticker, series slug, matcher). The slug is Kalshi's
# slugified series title; their router keys on the tickers, so a stale slug
# still resolves, but keeping it accurate makes the links look like theirs.
SERIES: dict[str, tuple[str, str, str]] = {
    "mlb":              ("KXMLBGAME",          "professional-baseball-game",        "teams"),
    "nba":              ("KXNBAGAME",          "pro-basketball-game",               "teams"),
    "wnba":             ("KXWNBAGAME",         "womens-pro-basketball-game",        "teams"),
    "ncaam":            ("KXNCAAMBGAME",       "mens-college-basketball-mens-game", "teams"),
    "ncaaw":            ("KXNCAAWBGAME",       "college-basketball-womens-game",    "teams"),
    "nba_summerleague": ("KXNBASUMMERGAME",    "pro-basketball-summer-league-game", "teams"),
    "nhl":              ("KXNHLGAME",          "nhl-game",                          "teams"),
    "nfl":              ("KXNFLGAME",          "professional-football-game",        "teams"),
    "ncaaf":            ("KXNCAAFGAME",        "college-football-game",             "teams"),
    "mls":              ("KXMLSGAME",          "major-league-soccer-game",          "teams"),
    "wc":               ("KXWCGAME",           "world-cup-game",                    "teams"),
    "ipl":              ("KXIPLGAME",          "indian-premier-league-cricket-game", "teams"),
    # KXODIMATCH / KXT20MATCH are the domestic (county, state) series;
    # the CRICKET* ones are the internationals we track.
    "odi":              ("KXCRICKETODIMATCH",  "cricket-odi-match",                 "teams"),
    "t20i":             ("KXCRICKETT20IMATCH", "cricket-t20i-match",                "teams"),
    "ufc":              ("KXUFCFIGHT",         "ufc-fight",                         "fight"),
    "mma":              ("KXMMAFIGHT",         "mma-fight",                         "fight"),
    "f1":               ("KXF1RACE",           "f1-race",                           "f1"),
    "valorant":         ("KXVALORANTGAME",     "valorant-game-winner",              "names"),
    # No entry: golf (Kalshi has no per-tournament PGA series), CWS, and the
    # manual one-offs (drafts, All-Star week).
}

# ESPN abbreviation -> the ones Kalshi uses instead. Anything not listed is
# also tried as a prefix match (SA/SAS, NY/NYK, UTAH/UTA …), so this only
# needs the cases where neither is a prefix of the other.
_ALIASES: dict[str, tuple[str, ...]] = {
    "ARI": ("AZ",), "CHW": ("CWS",),                     # MLB
    "JAX": ("JAC",), "WSH": ("WAS",),                    # NFL / any Washington
    "NO": ("NOP",), "GS": ("GSW",),                      # NBA
    "DC": ("DCU",), "LA": ("LAG", "LAK"), "RBNY": ("NYRB",),  # MLS / NHL
    "NJ": ("NJD",), "SJ": ("SJS",), "TB": ("TBL",), "MTL": ("MON",),
    "VGK": ("VEG",), "PHX": ("PHO",),
    "SL": ("SRI",),                                      # cricket
    "NCSU": ("NCST",),                                   # NC State
}

# Kalshi dates a ticker by the game's Eastern date, and so do we — so a team
# sport only ever matches the same-day market. (A ±1 day tolerance quietly
# paired the next game of an MLB series with yesterday's market once that
# was the only one open.) Overseas fixtures are the exception: Kalshi may
# date them locally, so these leagues fall back to the neighbouring days
# when nothing matches on the day itself.
_LOOSE_DATE_LEAGUES = {"ipl", "odi", "t20i", "wc"}

_TICKER_RE = re.compile(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{4})?(.*)$")
_GAME_SUFFIX_RE = re.compile(r"^(.*?)(G\d)$")
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_VS_RE = re.compile(r"\s+vs\.?\s+", re.I)


def enabled() -> bool:
    return os.getenv("KALSHI_LINKS", "true").lower() not in ("false", "0", "no")


def series_url(league_id: str) -> str | None:
    """The league's market listing on Kalshi — the popover's fallback when
    the event itself has no matched market."""
    spec = SERIES.get(league_id)
    if not spec:
        return None
    ticker, slug, _ = spec
    return f"{WEB}/{ticker.lower()}/{slug}"


def event_url(league_id: str, event_ticker: str) -> str:
    ticker, slug, _ = SERIES[league_id]
    return f"{WEB}/{ticker.lower()}/{slug}/{event_ticker.lower()}"


def annotate(league_id: str, events: list[Event]) -> int:
    """Set extra["kalshi_url"] on every event with a matching Kalshi market.
    Returns how many events carry a link afterwards (matched or preserved).
    """
    spec = SERIES.get(league_id)
    if not spec or not enabled() or not events:
        return 0
    ticker, _slug, kind = spec

    # A market that settled has left the "open" list, but its page is still
    # there — keep whatever URL an earlier refresh found so links don't
    # disappear from past games.
    prev: dict[str, str] = {}
    try:
        for row in get_events(leagues=[league_id]):
            url = (row.get("extra") or {}).get("kalshi_url")
            if url:
                prev[row["source_id"]] = url
    except Exception:
        pass

    try:
        raw = _fetch_open_events(ticker)
    except Exception:
        raw = []
    cands = [c for c in (_parse_event(e) for e in raw) if c]

    matcher = {"teams": _match_teams, "fight": _match_fight,
               "f1": _match_f1, "names": _match_names}[kind]
    loose_date = league_id in _LOOSE_DATE_LEAGUES
    linked = 0
    for ev in events:
        cand = None
        try:
            cand = matcher(ev, cands, loose_date)
        except Exception:
            cand = None
        url = event_url(league_id, cand["ticker"]) if cand else prev.get(ev.source_id)
        if url:
            ev.extra["kalshi_url"] = url
            linked += 1
    return linked


# --- Kalshi API ---

def _fetch_open_events(series_ticker: str) -> list[dict]:
    out: list[dict] = []
    cursor = None
    with httpx.Client(timeout=TIMEOUT) as client:
        for _ in range(10):  # 200/page; the biggest series (NCAAF) is ~2 pages
            params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{API}/events", params=params)
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("events") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
    return out


def _parse_event(e: dict) -> dict | None:
    ticker = e.get("event_ticker") or ""
    if not ticker:
        return None
    out = {
        "ticker": ticker,
        "title": e.get("title") or "",
        "sub_title": e.get("sub_title") or "",
        "date": None,      # Eastern date encoded in the ticker
        "hhmm": None,      # Eastern HH:MM as an int, when the ticker has one
        "tail": "",        # the part after the date/time: team abbrs + G#
    }
    m = _TICKER_RE.match(ticker)
    if m and m.group(2) in _MONTHS:
        yy, mon, dd, hhmm, tail = m.groups()
        try:
            out["date"] = datetime(2000 + int(yy), _MONTHS[mon], int(dd)).date()
        except ValueError:
            return out
        if hhmm:
            out["hhmm"] = int(hhmm)
        out["tail"] = tail
    return out


# --- matchers ---
# Each takes (our Event, parsed Kalshi candidates, loose_date) and returns
# the matching candidate or None. Team sports match on the same Eastern date
# (see _LOOSE_DATE_LEAGUES); fights and esports allow a day either side —
# a fighter pair or a VCT matchup doesn't repeat on consecutive days.

def _start_et(ev: Event) -> datetime | None:
    try:
        return datetime.fromisoformat(ev.start_utc.replace("Z", "+00:00")).astimezone(_ET)
    except (TypeError, ValueError):
        return None


def _near(cands: list[dict], when: datetime, days_either_side: int = 1) -> list[dict]:
    days = {when.date() + timedelta(days=d)
            for d in range(-days_either_side, days_either_side + 1)}
    return [c for c in cands if c["date"] in days]


def _abbr_variants(abbr: str) -> set[str]:
    a = abbr.upper()
    return {a, *_ALIASES.get(a, ())}


def _abbr_like(kalshi: str, espn: str) -> bool:
    """Exact, aliased, or one is a prefix of the other (SA/SAS, NY/NYK)."""
    if kalshi in _abbr_variants(espn):
        return True
    return len(kalshi) >= 2 and len(espn) >= 2 and (
        kalshi.startswith(espn) or espn.startswith(kalshi))


def _match_teams(ev: Event, cands: list[dict], loose_date: bool = False) -> dict | None:
    comps = (ev.extra or {}).get("competitors") or []
    abbrs = [str(c.get("abbr") or "").upper() for c in comps]
    abbrs = [a for a in abbrs if a]
    names = [str(c.get("name") or "") for c in comps]
    when = _start_et(ev)
    if len(abbrs) != 2 or when is None:
        return None
    same_day = _near(cands, when, 0)
    found = _match_teams_on(abbrs, names, same_day, when)
    if found is None and loose_date:
        found = _match_teams_on(abbrs, names, _near(cands, when, 1), when)
    return found


def _match_teams_on(abbrs: list[str], names: list[str],
                    cands: list[dict], when: datetime) -> dict | None:
    x, y = abbrs
    exact: list[dict] = []
    loose: list[dict] = []
    by_name: list[dict] = []
    for c in cands:
        tail = c["tail"]
        gm = _GAME_SUFFIX_RE.match(tail)
        if gm:
            tail = gm.group(1)
        if not tail:
            continue
        if any(a + b == tail for a in _abbr_variants(x) for b in _abbr_variants(y)) or \
           any(b + a == tail for a in _abbr_variants(x) for b in _abbr_variants(y)):
            exact.append(c)
            continue
        # Unknown abbreviation scheme: try every split of the tail and accept
        # a prefix-compatible pair, but only if it's the sole candidate.
        for i in range(2, len(tail) - 1):
            ka, kb = tail[:i], tail[i:]
            if (_abbr_like(ka, x) and _abbr_like(kb, y)) or \
               (_abbr_like(ka, y) and _abbr_like(kb, x)):
                loose.append(c)
                break
        # Kalshi's college abbreviations are its own (NCST for ESPN's NCSU,
        # OKLA for OU, BSU for BOIS …) but its titles are team names, so
        # compare those too — again only accepted when unambiguous.
        sides = _sides(c["title"])
        if sides and len(names) == 2 and all(names):
            ka, kb = sides
            if (_team_name_like(ka, names[0]) and _team_name_like(kb, names[1])) or \
               (_team_name_like(ka, names[1]) and _team_name_like(kb, names[0])):
                by_name.append(c)
    pool = exact or (loose if len(loose) == 1 else []) or \
        (by_name if len(by_name) == 1 else [])
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    # Doubleheader (…G1/…G2) or a same-day rematch: take the start time
    # closest to ours, same-day first.
    et_minutes = when.hour * 60 + when.minute

    def distance(c: dict) -> tuple[int, int]:
        day_gap = abs((c["date"] - when.date()).days)
        if c["hhmm"] is None:
            return (day_gap, 24 * 60)
        return (day_gap, abs((c["hhmm"] // 100) * 60 + c["hhmm"] % 100 - et_minutes))
    return min(pool, key=distance)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _name_tokens(name: str) -> list[str]:
    """'Boise St.' -> ['boise', 'state']; 'Miami (OH) RedHawks' -> ['miami', 'oh', 'redhawks']."""
    out = []
    for tok in (name or "").replace("-", " ").split():
        t = _norm(tok)
        if t == "st":
            t = "state"
        if t:
            out.append(t)
    return out


def _team_name_like(kalshi_side: str, espn_name: str) -> bool:
    """Kalshi names a team the way a box score does ('Boise St.', 'Miami
    (OH)', 'Los Angeles A'); ESPN's displayName is location + nickname
    ('Boise State Broncos'). Match when the leading token agrees and, if both
    names have a second token, that one agrees as well — enough to keep
    'Tennessee St.' away from 'Tennessee Tech'."""
    k, e = _name_tokens(kalshi_side), _name_tokens(espn_name)
    if not k or not e or k[0] != e[0]:
        return False
    if len(k) >= 2 and len(e) >= 2 and k[1] != e[1]:
        return False
    return True


def _surname(name: str) -> str:
    parts = (name or "").split()
    return _norm(parts[-1]) if parts else ""


def _sides(title: str) -> tuple[str, str] | None:
    """'Fight Night: Meng vs Nelson' / 'Paper Rex vs. VARREL' -> the two names."""
    body = title.rsplit(": ", 1)[-1] if ": " in title else title
    parts = _VS_RE.split(body, maxsplit=1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def _fighter_like(kalshi_side: str, espn_full: str) -> bool:
    """Kalshi bills a fight by surname ('Meng vs Nelson'); accept the surname
    wherever it sits in ESPN's full name — 'Song Yadong' is family-name-first
    — or a multi-word tail like 'de Sousa Santiago'."""
    k, e = _norm(kalshi_side), _norm(espn_full)
    if not k or not e:
        return False
    return e.endswith(k) or k in _name_tokens(espn_full)


def _match_fight(ev: Event, cands: list[dict], loose_date: bool = False) -> dict | None:
    """A card segment links to the market on its headline bout (last in the
    ESPN running order — the main event for the main card)."""
    bouts = (ev.extra or {}).get("bouts") or []
    when = _start_et(ev)
    if not bouts or when is None:
        return None
    head = bouts[-1]
    a, b = head.get("a") or "", head.get("b") or ""
    if not a or not b or "TBA" in a or "TBA" in b:
        return None
    for c in _near(cands, when):
        sides = _sides(c["title"])
        if not sides:
            continue
        ka, kb = sides
        if (_fighter_like(ka, a) and _fighter_like(kb, b)) or \
           (_fighter_like(ka, b) and _fighter_like(kb, a)):
            return c
    return None


_F1_YEAR_RE = re.compile(r"\s+(\d{4})\s*$")


def _match_f1(ev: Event, cands: list[dict], loose_date: bool = False) -> dict | None:
    """Every session of a Grand Prix weekend links to the race-winner market.
    Kalshi: 'Italian Grand Prix 2026'; ESPN: 'Pirelli Italian Grand Prix'."""
    gp = _norm((ev.extra or {}).get("gp") or ev.title.split(" — ")[0])
    when = _start_et(ev)
    if not gp or when is None:
        return None
    for c in cands:
        label = c["sub_title"] or c["title"].replace(" Winner", "")
        ym = _F1_YEAR_RE.search(label)
        year = int(ym.group(1)) if ym else None
        name = _norm(_F1_YEAR_RE.sub("", label))
        if name and name in gp and (year is None or year == when.year):
            return c
    return None


def _name_like(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _match_names(ev: Event, cands: list[dict], loose_date: bool = False) -> dict | None:
    """Esports: no abbreviations, so compare team names ('Gen.G' vs
    Kalshi's 'Gen.G Esports')."""
    extra = ev.extra or {}
    a, b = extra.get("team_a"), extra.get("team_b")
    if not (a and b):
        sides = _sides(ev.title)
        if not sides:
            return None
        a, b = sides
    when = _start_et(ev)
    if when is None:
        return None
    for c in _near(cands, when):
        sides = _sides(c["title"])
        if not sides:
            continue
        ka, kb = sides
        if (_name_like(ka, a) and _name_like(kb, b)) or \
           (_name_like(ka, b) and _name_like(kb, a)):
            return c
    return None
