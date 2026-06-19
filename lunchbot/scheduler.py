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
RATING_DELAY_MINUTES = {"lunch": 150, "dinner": 720}  # 2.5h later / next morning-ish


class Scheduler:
    def __init__(self, config: Config):
        self.config = config
        self.sched = BackgroundScheduler(timezone=config.tz)
        self._poll = None  # set in attach()

    def attach(self, poll_service) -> None:
        self._poll = poll_service

    def start(self) -> None:
        cfg = self.config
        if cfg.lunch_enabled:
            h, m = cfg.parse_time(cfg.lunch_time)
            self.sched.add_job(
                self._post, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
                args=["lunch"], id="post_lunch", replace_existing=True,
            )
            log.info("Scheduled lunch poll at %02d:%02d %s", h, m, cfg.tz)
        if cfg.dinner_enabled:
            h, m = cfg.parse_time(cfg.dinner_time)
            self.sched.add_job(
                self._post, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
                args=["dinner"], id="post_dinner", replace_existing=True,
            )
            log.info("Scheduled dinner poll at %02d:%02d %s", h, m, cfg.tz)
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
            delay = RATING_DELAY_MINUTES.get(poll["slot"], 150)
            self.sched.add_job(
                self._rate, "date", run_date=datetime.now() + timedelta(minutes=delay),
                args=[poll_id], id=f"rate_{poll_id}", replace_existing=True,
            )

    def _rate(self, poll_id: int) -> None:
        self._poll.post_rating_prompt(poll_id)

    def shutdown(self) -> None:
        self.sched.shutdown(wait=False)
