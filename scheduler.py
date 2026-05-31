"""APScheduler setup: periodic refresh + daily digest."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    tz = os.getenv("TZ", "America/New_York")
    refresh_min = int(os.getenv("REFRESH_INTERVAL_MIN", "30"))
    digest_time = os.getenv("DIGEST_TIME", "07:00")

    sched = BackgroundScheduler(timezone=tz)

    # Periodic data refresh
    from refresh import refresh_all
    sched.add_job(
        refresh_all,
        trigger=IntervalTrigger(minutes=refresh_min),
        id="refresh_all",
        replace_existing=True,
        next_run_time=None,  # don't immediately re-fetch; the API/manual call handles startup
    )

    # Daily digest at the configured local time
    try:
        hh, mm = digest_time.split(":")
        from digest import send_digest
        sched.add_job(
            send_digest,
            trigger=CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
            id="daily_digest",
            replace_existing=True,
        )
    except Exception as exc:
        logger.warning("Could not schedule digest: %s", exc)

    sched.start()
    _scheduler = sched
    return sched
