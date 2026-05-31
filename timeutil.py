"""Single source of truth for datetime handling.

EVERY timestamp produced or accepted by this project must pass through
`to_utc_iso()`. That guarantees a uniform canonical form so SQL string
comparisons, FullCalendar's strict ISO parser, and downstream timezone
conversions all behave the same way.

Canonical form:
    YYYY-MM-DDTHH:MM:SS+00:00
    - Always UTC (offset always +00:00)
    - Always seconds present
    - Always explicit offset present

Reasoning: every recurring bug we've hit in this project (events not
appearing, times in the wrong row, filter excluding overnight games)
traced back to inconsistent timestamp formats sneaking into the system
from a different code path. One normalizer eliminates that whole class.
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc_iso(value) -> str | None:
    """Coerce any reasonable datetime input into canonical UTC ISO 8601.

    Accepts:
      - None / "" → None
      - ISO strings: with/without 'Z', with/without offset, with/without seconds
      - datetime objects: naive ones are assumed UTC; aware ones are converted

    Returns:
      Canonical UTC ISO string, or None if the input can't be parsed.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            s = str(value).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_canonical(s: str | None) -> bool:
    """Quick check: is this string already in canonical UTC ISO form?
    Used by the startup integrity check."""
    if not s:
        return True
    # Exactly: YYYY-MM-DDTHH:MM:SS+00:00 → length 25
    return (
        len(s) == 25
        and s[4] == "-" and s[7] == "-" and s[10] == "T"
        and s[13] == ":" and s[16] == ":"
        and s.endswith("+00:00")
    )
