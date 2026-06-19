"""APScheduler wiring: two daily jobs + per-poll one-shot closes.

Kept separate from poll logic so the lifecycle (poll.py) stays testable without
a live scheduler. The PollService calls back into `schedule_close` to register
the 10-minute close, and a rating prompt is chained after each close.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config

log = logging.getLogger(__name__)

# How long after a close to ask "how was it?" -- enough time to actually eat.
# Per-meal overrides; anything else uses DEFAULT_RATING_DELAY.
RATING_DELAY_MINUTES = {"lunch": 150, "dinner": 720}  # 2.5h later / next morning-ish
DEFAULT_RATING_DELAY = 150


class Scheduler:
    def __init__(self, config: Config):
        self.config = config
        self.sched = BackgroundScheduler(timezone=config.tz)
        self._poll = None  # set in attach()

    def attach(self, poll_service) -> None:
        self._poll = poll_service

    def start(self) -> None:
        cfg = self.config
        for meal in cfg.meals:
            self.sched.add_job(
                self._post, CronTrigger(day_of_week="mon-fri", hour=meal.hour, minute=meal.minute),
                args=[meal.name], id=f"post_{meal.name}", replace_existing=True,
            )
            log.info("Scheduled %s poll at %02d:%02d %s", meal.name, meal.hour, meal.minute, cfg.tz)
        # Nightly DB snapshot at 03:00 (data/backups/, 14 kept).
        self.sched.add_job(
            self._backup, CronTrigger(hour=3, minute=0),
            id="nightly_backup", replace_existing=True,
        )
        log.info("Scheduled nightly DB backup at 03:00 %s", cfg.tz)
        self.sched.start()

    # -- callbacks wired into PollService ------------------------------------

    def _post(self, slot: str) -> None:
        self._poll.post_picks(slot)

    def schedule_close(self, poll_id: int, close_at: datetime) -> None:
        self.sched.add_job(
            self._close, "date", run_date=close_at, args=[poll_id],
            id=f"close_{poll_id}", replace_existing=True,
        )

    def _close(self, poll_id: int) -> None:
        poll = self._poll.db.get_poll(poll_id)
        self._poll.close_poll(poll_id)
        poll = self._poll.db.get_poll(poll_id)
        if poll and poll["winner_id"] is not None:
            delay = RATING_DELAY_MINUTES.get(poll["slot"], DEFAULT_RATING_DELAY)
            self.sched.add_job(
                self._rate, "date", run_date=datetime.now() + timedelta(minutes=delay),
                args=[poll_id], id=f"rate_{poll_id}", replace_existing=True,
            )

    def _rate(self, poll_id: int) -> None:
        self._poll.post_rating_prompt(poll_id)

    def _backup(self) -> None:
        try:
            path = self._poll.db.backup()
            log.info("DB backup written to %s", path)
        except Exception:
            log.exception("Nightly DB backup failed")

    def shutdown(self) -> None:
        self.sched.shutdown(wait=False)
