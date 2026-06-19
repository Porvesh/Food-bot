"""Poll lifecycle: post -> vote -> close -> rate.

This ties the pure engine (recommender.py) to the stateful world (db.py + Slack).
It is deliberately the only place that knows how to translate DB rows into engine
dataclasses, post/update Slack messages, and decide winners.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import recommender as rec
from .blocks import (
    poll_fallback_text,
    poll_message,
    rating_done_message,
    rating_message,
)
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

# A function the scheduler injects so close_poll can be registered as a one-shot.
ScheduleClose = Callable[[int, datetime], None]


class PollService:
    def __init__(self, db: Database, config: Config, slack_client, claude, schedule_close: ScheduleClose):
        self.db = db
        self.config = config
        self.slack = slack_client
        self.claude = claude
        self.schedule_close = schedule_close
        # poll_id -> {place_id: pitch}, so live re-renders reuse the Claude copy
        # instead of paying for a generation on every button click.
        self._pitch_cache: dict[int, dict[int, str]] = {}

    # -- engine wiring --------------------------------------------------------

    def _to_engine_place(self, row) -> rec.Place:
        return rec.Place(
            id=row["id"],
            name=row["name"],
            cuisine=row["cuisine"],
            is_active=bool(row["is_active"]),
            times_picked=row["times_picked"],
            last_visit=row["last_visit"],
            sum_ratings=row["sum_ratings"],
            num_ratings=row["num_ratings"],
        )

    def _tunables(self) -> rec.Tunables:
        t = self.config.tunables
        return rec.Tunables(
            prior_m=t.prior_m,
            explore_k=t.explore_k,
            recency_halflife_days=t.recency_halflife_days,
        )

    def select_candidates(self, slot: str, on_date: date) -> list[rec.Scored]:
        places = [self._to_engine_place(r) for r in self.db.active_places()]
        roster = self.db.present_roster(on_date.isoformat(), slot)
        return rec.recommend(
            places,
            roster,
            today=on_date,
            global_c=self.db.global_like_rate(),
            tunables=self._tunables(),
            active_constraints=self.db.constraints_for(roster),
            user_cuisine_stats=self.db.cuisine_ratings_by_user(),
            user_means=self.db.user_mean_rating(),
        )

    # -- post ----------------------------------------------------------------

    def post_picks(self, slot: str, on_date: Optional[date] = None) -> Optional[int]:
        on_date = on_date or date.today()
        picks = self.select_candidates(slot, on_date)
        if len(picks) < 1:
            log.warning("No candidate places for %s; add some with /lunch add.", slot)
            self.slack.chat_postMessage(
                channel=self.config.channel_id,
                text=f"I have no places to suggest for {slot} yet. Add some with `/lunch add <name>`.",
            )
            return None

        candidate_ids = [s.place.id for s in picks]
        close_at = datetime.now() + timedelta(minutes=self.config.poll_minutes)
        poll_id = self.db.create_poll(slot, on_date.isoformat(), candidate_ids, close_at.isoformat())

        cards = []
        pitches: dict[int, str] = {}
        for s in picks:
            row = self.db.get_place(s.place.id)
            pitch = self.claude.pitch(row["name"], row["cuisine"])
            pitches[row["id"]] = pitch
            cards.append((row, pitch, 0))
        self._pitch_cache[poll_id] = pitches

        resp = self.slack.chat_postMessage(
            channel=self.config.channel_id,
            text=poll_fallback_text(slot),
            blocks=poll_message(slot, cards),
        )
        self.db.set_poll_ts(poll_id, resp["ts"])
        self.schedule_close(poll_id, close_at)
        log.info("Posted %s poll #%d, closes at %s", slot, poll_id, close_at.isoformat())
        return poll_id

    # -- vote ----------------------------------------------------------------

    def handle_vote(self, poll_id: int, slack_id: str, place_id: int, display: Optional[str] = None) -> None:
        poll = self.db.get_poll(poll_id)
        if poll is None or poll["closed"]:
            return  # late vote: rejected by the closed flag (spec section 4)
        self.db.ensure_user(slack_id, display)
        self.db.cast_vote(poll_id, slack_id, place_id)
        self._rerender_poll(poll_id)

    def _rerender_poll(self, poll_id: int, *, closed: bool = False) -> None:
        poll = self.db.get_poll(poll_id)
        if poll is None or not poll["ts"]:
            return
        tally = self.db.vote_tally(poll_id)
        pitches = self._pitch_cache.get(poll_id, {})
        cards = []
        for pid in self.db.poll_candidates(poll):
            row = self.db.get_place(pid)
            if row is None:
                continue
            pitch = pitches.get(pid) or _cached_pitch(row)
            cards.append((row, pitch, tally.get(pid, 0)))
        self.slack.chat_update(
            channel=self.config.channel_id,
            ts=poll["ts"],
            text=poll_fallback_text(poll["slot"], closed=closed),
            blocks=poll_message(
                poll["slot"], cards, closed=closed, winner_id=poll["winner_id"]
            ),
        )

    # -- close ---------------------------------------------------------------

    def close_poll(self, poll_id: int) -> None:
        """Idempotent: safe to call twice (restart recovery may double-fire)."""
        poll = self.db.get_poll(poll_id)
        if poll is None or poll["closed"]:
            return

        tally = self.db.vote_tally(poll_id)
        candidates = self.db.poll_candidates(poll)
        winner_id = self._decide_winner(poll, tally, candidates)

        self.db.mark_poll_closed(poll_id, winner_id)
        if winner_id is not None:
            self.db.record_win(winner_id, poll["date"])

        self._rerender_poll(poll_id, closed=True)

        if winner_id is not None:
            row = self.db.get_place(winner_id)
            zero_votes = sum(tally.values()) == 0
            note = " (default pick — nobody voted)" if zero_votes else ""
            self.slack.chat_postMessage(
                channel=self.config.channel_id,
                text=f"Winner: {row['name']}{note}",
            )
        log.info("Closed poll #%d, winner=%s", poll_id, winner_id)

    def _decide_winner(self, poll, tally: dict[int, int], candidates: list[int]) -> Optional[int]:
        if not candidates:
            return None
        if not tally:
            # Zero votes: auto-pick the #1-scored candidate so the day still
            # produces a visit (spec section 12).
            picks = self.select_candidates(poll["slot"], date.fromisoformat(poll["date"]))
            ranked = [s.place.id for s in picks if s.place.id in candidates]
            return ranked[0] if ranked else candidates[0]

        max_votes = max(tally.values())
        tied = [pid for pid, n in tally.items() if n == max_votes]
        if len(tied) == 1:
            return tied[0]
        # Tie-break by the recommender's own score (deterministic, spec section 12).
        scored = self.select_candidates(poll["slot"], date.fromisoformat(poll["date"]))
        order = {s.place.id: i for i, s in enumerate(scored)}
        return min(tied, key=lambda pid: order.get(pid, 1_000_000))

    # -- rate ----------------------------------------------------------------

    def post_rating_prompt(self, poll_id: int) -> None:
        poll = self.db.get_poll(poll_id)
        if poll is None or poll["rated"] or poll["winner_id"] is None:
            return
        row = self.db.get_place(poll["winner_id"])
        self.slack.chat_postMessage(
            channel=self.config.channel_id,
            text=f"How was {row['name']}?",
            blocks=rating_message(row["name"], poll_id, poll["winner_id"]),
        )
        self.db.mark_poll_rated(poll_id)

    def handle_rating(
        self,
        poll_id: int,
        place_id: int,
        slack_id: str,
        value: float,
        respond,
        display: Optional[str] = None,
    ) -> None:
        self.db.ensure_user(slack_id, display)
        self.db.record_rating(poll_id, slack_id, place_id, value)
        row = self.db.get_place(place_id)
        # Replace the prompt with a thank-you for this user (ephemeral-friendly).
        respond(blocks=rating_done_message(row["name"]), replace_original=False, response_type="ephemeral")

    # -- restart recovery -----------------------------------------------------

    def recover_open_polls(self) -> None:
        """On boot, close any polls whose window passed; re-schedule the rest."""
        now = datetime.now()
        for poll in self.db.open_polls():
            close_at = datetime.fromisoformat(poll["close_at"]) if poll["close_at"] else now
            if close_at <= now:
                log.info("Recovery: closing overdue poll #%d", poll["id"])
                self.close_poll(poll["id"])
            else:
                log.info("Recovery: re-scheduling close for poll #%d at %s", poll["id"], close_at)
                self.schedule_close(poll["id"], close_at)


def _cached_pitch(row) -> str:
    """Cheap re-render pitch during live voting (avoid a Claude call per click)."""
    return f"{row['cuisine'].title()} at {row['name']}" if row["cuisine"] else row["name"]
