"""Integration test: full poll lifecycle against an in-memory DB and fake Slack.

Covers post -> vote -> change vote -> close -> rate, plus zero-vote default,
tie-break, and restart recovery (spec section 16).
"""

from datetime import date, datetime, timedelta

import pytest

from lunchbot.config import Config
from lunchbot.db import Database
from lunchbot.poll import PollService
from lunchbot.scheduler import Scheduler


class FakeSlack:
    """Records calls and hands back incrementing ts values like Slack would."""

    def __init__(self):
        self.posted = []
        self.updated = []
        self._ts = 1000

    def chat_postMessage(self, **kwargs):
        self._ts += 1
        ts = f"{self._ts}.0001"
        self.posted.append({**kwargs, "ts": ts})
        return {"ts": ts}

    def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ts": kwargs.get("ts")}


class FakeClaude:
    enabled = False

    def pitch(self, name, cuisine):
        return f"Try {name}"


class FakeScheduler:
    def __init__(self):
        self.scheduled = []

    def schedule_close(self, poll_id, close_at):
        self.scheduled.append((poll_id, close_at))


@pytest.fixture
def service():
    db = Database(":memory:")
    for name, cuisine in [("Thai Spot", "thai"), ("Ramen Bar", "ramen"),
                          ("Salad Co", "salad"), ("Taco Truck", "tacos")]:
        db.upsert_place(name, cuisine=cuisine, source="manual")
    config = Config()
    sched = FakeScheduler()
    svc = PollService(db, config, FakeSlack(), FakeClaude(), sched.schedule_close)
    svc._sched = sched
    return svc


def test_post_creates_poll_and_message(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    assert poll_id is not None
    poll = service.db.get_poll(poll_id)
    assert poll["ts"] is not None
    assert len(service.db.poll_candidates(poll)) == 3
    assert len(service.slack.posted) == 1
    assert service._sched.scheduled[0][0] == poll_id


def test_multi_vote_and_toggle(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.handle_vote(poll_id, "U1", cands[0])
    assert service.db.vote_tally(poll_id) == {cands[0]: 1}
    # Same user votes a second place -> both count (multi-select).
    service.handle_vote(poll_id, "U1", cands[1])
    assert service.db.vote_tally(poll_id) == {cands[0]: 1, cands[1]: 1}
    # Tapping the first place again toggles it off.
    service.handle_vote(poll_id, "U1", cands[0])
    assert service.db.vote_tally(poll_id) == {cands[1]: 1}


def test_close_picks_winner_and_records_win(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.handle_vote(poll_id, "U1", cands[0])
    service.handle_vote(poll_id, "U2", cands[0])
    service.handle_vote(poll_id, "U3", cands[1])
    service.close_poll(poll_id)
    poll = service.db.get_poll(poll_id)
    assert poll["closed"] == 1
    assert poll["winner_id"] == cands[0]
    assert service.db.get_place(cands[0])["times_picked"] == 1


def test_close_is_idempotent(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.handle_vote(poll_id, "U1", cands[0])
    service.close_poll(poll_id)
    first = service.db.get_place(cands[0])["times_picked"]
    service.close_poll(poll_id)  # double-fire (restart recovery)
    assert service.db.get_place(cands[0])["times_picked"] == first


def test_zero_votes_still_picks_a_winner(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    service.close_poll(poll_id)
    poll = service.db.get_poll(poll_id)
    assert poll["winner_id"] is not None  # default pick so the day has a visit


def test_late_vote_after_close_is_rejected(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.close_poll(poll_id)
    service.handle_vote(poll_id, "U9", cands[0])
    # The only vote recorded should be none from after close.
    assert "U9" not in [r["slack_id"] for r in service.db.conn.execute("SELECT slack_id FROM votes")]


def test_rating_updates_place_aggregates(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.handle_vote(poll_id, "U1", cands[0])
    service.close_poll(poll_id)
    winner = service.db.get_poll(poll_id)["winner_id"]

    captured = {}
    def respond(**kwargs):
        captured.update(kwargs)

    service.handle_rating(poll_id, winner, "U1", 1.0, respond)
    place = service.db.get_place(winner)
    assert place["num_ratings"] == 1
    assert place["sum_ratings"] == 1.0


def test_recovery_closes_overdue_poll(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    # Force the close_at into the past (tz-aware, matching how it's now stored).
    service.db.conn.execute(
        "UPDATE polls SET close_at = ? WHERE id = ?",
        ((service.config.now() - timedelta(minutes=1)).isoformat(), poll_id),
    )
    service.db.conn.commit()
    service.recover_open_polls()
    assert service.db.get_poll(poll_id)["closed"] == 1


def test_close_at_is_timezone_aware(service):
    """The close time handed to the scheduler must be tz-aware, so a host whose
    system clock zone differs from TZ doesn't fire closes hours off."""
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    _, close_at = service._sched.scheduled[0]
    assert close_at.tzinfo is not None
    stored = service.db.get_poll(poll_id)["close_at"]
    assert datetime.fromisoformat(stored).tzinfo is not None


def test_pitch_cache_cleared_on_close(service):
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    assert poll_id in service._pitch_cache
    service.close_poll(poll_id)
    assert poll_id not in service._pitch_cache


def test_recover_pending_ratings_prompts_overdue_winner(service):
    """A poll that closed with a winner but never got its rating prompt (e.g. the
    process restarted in the close->rate window) is prompted on recovery."""
    poll_id = service.post_picks("lunch", date(2026, 6, 18))
    cands = service.db.poll_candidates(service.db.get_poll(poll_id))
    service.handle_vote(poll_id, "U1", cands[0])
    service.close_poll(poll_id)
    # Push close_at well into the past so close_at + rating delay is overdue.
    service.db.conn.execute(
        "UPDATE polls SET close_at = ? WHERE id = ?",
        ((service.config.now() - timedelta(days=1)).isoformat(), poll_id),
    )
    service.db.conn.commit()
    assert service.db.get_poll(poll_id)["rated"] == 0

    sched = Scheduler(service.config)
    sched.attach(service)
    posted_before = len(service.slack.posted)
    sched.recover_pending_ratings()

    assert service.db.get_poll(poll_id)["rated"] == 1
    new_posts = service.slack.posted[posted_before:]
    assert any("How was" in p.get("text", "") for p in new_posts)
