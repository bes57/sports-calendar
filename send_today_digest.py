"""Standalone runner for the daily digest.

Used by the GitHub Actions cron workflow so the push goes out even when the
local server isn't running. Refreshes the event feeds, builds today's
agenda, and calls send_digest() once.

Env vars (set in the GitHub Actions workflow via repo secrets):
    NTFY_TOPIC          required for the ntfy push
    NTFY_SERVER         optional, defaults to https://ntfy.sh
    TZ                  e.g. America/New_York
    FETCH_DAYS_AHEAD    optional, defaults to 30
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo modules import cleanly regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import init_db  # noqa: E402
from digest import send_digest  # noqa: E402
from refresh import refresh_all  # noqa: E402


def main() -> int:
    init_db()
    print("Refreshing event feeds...")
    summary = refresh_all()
    print(f"Total events fetched: {summary['total']}")

    print("Sending digest...")
    result = send_digest()
    print(f"Result: {result}")

    ntfy = result.get("ntfy") or {}
    if not ntfy.get("sent"):
        print(f"ntfy push failed: {ntfy}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
