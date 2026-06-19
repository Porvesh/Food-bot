"""Unit tests for the post-MVP features: manual scores, meal filtering,
cuisine/price enrichment, the nightly backup, and the slash-command helpers."""

import json
from datetime import date

from lunchbot import slack_app as sa
from lunchbot.claude_client import ClaudeClient
from lunchbot.config import Config
from lunchbot.db import Database
from lunchbot.poll import PollService
from lunchbot.recommender import Place, Tunables, score_place

TODAY = date(2026, 6, 18)
TUN = Tunables(prior_m=8, explore_k=0.3, recency_halflife_days=7)


# -- fakes -------------------------------------------------------------------

class FakeSlack:
    def __init__(self):
        self._ts = 1000
        self.posted = []

    def chat_postMessage(self, **kw):
        self._ts += 1
        ts = f"{self._ts}.0001"
        self.posted.append({**kw, "ts": ts})
        return {"ts": ts}

    def chat_update(self, **kw):
        return {"ts": kw.get("ts")}


class FakeClaude:
    enabled = False

    def pitch(self, name, cuisine):
        return f"Try {name}"

    def enrich(self, name):
        return {}


def _service(places):
    db = Database(":memory:")
    for name, meal in places:
        pid = db.upsert_place(name, source="manual")
        db.set_place_meal(pid, meal)
    sched = []
    return PollService(db, Config(), FakeSlack(), FakeClaude(),
                       lambda pid, at: sched.append((pid, at)))


def _capture():
    out = []

    def respond(*args, **kwargs):
        out.append(kwargs if kwargs else (args[0] if args else None))

    return out, respond


# -- recommender: manual score overrides learned quality ---------------------

def test_manual_score_overrides_rating_quality():
    p = Place(id=1, name="P", cuisine="thai", manual_score=9.0)  # no ratings
    scored = score_place(p, ["U1"], today=TODAY, global_c=0.6, tunables=TUN,
                         user_cuisine_stats={}, user_means={})
    assert scored.rating_quality == 0.9  # 9/10, not the 0.6 global prior


def test_no_manual_score_uses_bayesian():
    p = Place(id=1, name="P", cuisine="thai")  # unrated, no manual score
    scored = score_place(p, ["U1"], today=TODAY, global_c=0.6, tunables=TUN,
                         user_cuisine_stats={}, user_means={})
    assert scored.rating_quality == 0.6


# -- DB: migration, enrich, meal, find ---------------------------------------

def test_migration_adds_columns():
    db = Database(":memory:")
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(places)")}
    assert {"manual_score", "meal"} <= cols


def test_enrich_place_fills_only_empty_fields():
    db = Database(":memory:")
    pid = db.upsert_place("Chipotle")
    db.set_place_cuisine(pid, "mexican")          # human set this
    db.enrich_place(pid, cuisine="italian", price_band=2)  # auto tries to change
    row = db.get_place(pid)
    assert row["cuisine"] == "mexican"            # not clobbered
    assert row["price_band"] == 2                 # empty field filled


def test_find_place_is_canonical():
    db = Database(":memory:")
    db.upsert_place("Thai Basil")
    assert db.find_place("thai basil!")["name"] == "Thai Basil"
    assert db.find_place("nope") is None


def test_meal_filtering_excludes_other_slot():
    svc = _service([("Lunchery", "lunch"), ("Dinner Club", "dinner"),
                    ("Anytime A", "both"), ("Anytime B", "both")])
    lunch = {s.place.name for s in svc.select_candidates("lunch", TODAY)}
    assert "Dinner Club" not in lunch
    assert "Lunchery" in lunch or "Anytime A" in lunch


# -- backup ------------------------------------------------------------------

def test_backup_writes_a_valid_snapshot(tmp_path):
    dbfile = tmp_path / "lunchbot.db"
    db = Database(str(dbfile))
    db.upsert_place("Chipotle")
    path = db.backup()
    assert path and path.endswith(".bak")
    import sqlite3
    snap = sqlite3.connect(path)
    assert snap.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 1
    snap.close()


# -- Claude enrichment parsing -----------------------------------------------

def _stub_claude(text):
    c = ClaudeClient.__new__(ClaudeClient)
    c.model = "x"
    block = type("B", (), {"type": "text", "text": text})()
    msgs = type("M", (), {"create": lambda self, **kw: type("R", (), {"content": [block]})()})()
    c._client = type("C", (), {"messages": msgs})()
    return c


def test_enrich_parses_and_normalizes():
    c = _stub_claude('{"cuisine":"MEXICAN","price_band":2}')
    assert c.enrich("Chipotle") == {"cuisine": "mexican", "price_band": 2}


def test_enrich_disabled_returns_empty():
    assert ClaudeClient("", "x").enrich("Chipotle") == {}


def test_loads_json_strips_markdown_fence():
    from lunchbot.claude_client import _loads_json
    assert _loads_json('```json\n[{"name":"X"}]\n```') == [{"name": "X"}]
    assert _loads_json('```\n{"a": 1}\n```') == {"a": 1}
    assert _loads_json('{"a": 1}') == {"a": 1}


def test_enrich_handles_fenced_response():
    assert _stub_claude('```json\n{"cuisine":"thai","price_band":1}\n```').enrich("X") == {
        "cuisine": "thai", "price_band": 1
    }


def test_discover_parses_fenced_list():
    c = _stub_claude('```json\n[{"name":"Pho 1","cuisine":"vietnamese","price_band":2}]\n```')
    assert c.discover("pho") == [{"name": "Pho 1", "cuisine": "vietnamese", "price_band": 2}]


def test_enrich_bad_json_returns_empty():
    assert _stub_claude("sorry, no idea").enrich("X") == {}


def test_enrich_drops_out_of_range_price():
    c = _stub_claude('{"cuisine":"thai","price_band":9}')
    assert c.enrich("X") == {"cuisine": "thai"}  # bad band dropped, cuisine kept


# -- slash-command helpers ---------------------------------------------------

def test_clean_name_takes_first_line():
    assert sa._clean_name("Thai Basil\n/lunch add Sweetgreen") == "Thai Basil"
    assert sa._clean_name("  spaced   out  ") == "spaced out"


def test_score_out_of_10_prefers_manual_then_ratings():
    db = Database(":memory:")
    pid = db.upsert_place("P")
    assert sa._score_out_of_10(db.get_place(pid)) is None      # unrated
    db.record_rating(0, "U1", pid, 1.0)                        # one 👍
    assert sa._score_out_of_10(db.get_place(pid)) == 10.0
    db.set_manual_score(pid, 7.0)
    assert sa._score_out_of_10(db.get_place(pid)) == 7.0       # manual wins


MEALS = {"lunch", "dinner", "both"}


def test_set_score_rejects_unknown_place():
    db = Database(":memory:")
    out, respond = _capture()
    sa._set_score(db, "Ghost Diner 8", respond, MEALS)
    assert "No place matching" in out[-1]


def test_set_score_sets_value_and_meal():
    db = Database(":memory:")
    db.upsert_place("Pizza Place")
    out, respond = _capture()
    sa._set_score(db, "Pizza Place 9 dinner", respond, MEALS)
    row = db.find_place("Pizza Place")
    assert row["manual_score"] == 9.0
    assert row["meal"] == "dinner"


def test_set_score_rejects_out_of_range():
    db = Database(":memory:")
    db.upsert_place("P")
    out, respond = _capture()
    sa._set_score(db, "P 50", respond, MEALS)
    assert "between 0 and 10" in out[-1]


def test_set_cuisine_overrides():
    db = Database(":memory:")
    db.upsert_place("Thai Basil")
    out, respond = _capture()
    sa._set_cuisine(db, "Thai Basil thai", respond)
    assert db.find_place("Thai Basil")["cuisine"] == "thai"


def test_meta_suffix_formats_cuisine_and_price():
    db = Database(":memory:")
    pid = db.upsert_place("P")
    db.enrich_place(pid, cuisine="ramen", price_band=3)
    assert sa._meta_suffix(db.get_place(pid)) == " — ramen · $$$"


# -- configurable meals ------------------------------------------------------

def test_parse_meals():
    from lunchbot.config import _parse_meals
    meals = _parse_meals("breakfast@08:30, coffee@10:00 , dinner@18:00")
    assert [(m.name, m.hour, m.minute) for m in meals] == [
        ("breakfast", 8, 30), ("coffee", 10, 0), ("dinner", 18, 0)
    ]


def test_meals_default_lunch_and_dinner(monkeypatch):
    from lunchbot import config
    for v in ["MEALS", "BREAKFAST_ENABLED", "COFFEE_ENABLED", "SNACK_ENABLED",
              "LUNCH_ENABLED", "DINNER_ENABLED"]:
        monkeypatch.delenv(v, raising=False)
    names = [m.name for m in config._load_meals()]
    assert names == ["lunch", "dinner"]


def test_meals_toggle_via_env(monkeypatch):
    from lunchbot import config
    monkeypatch.delenv("MEALS", raising=False)
    monkeypatch.setenv("BREAKFAST_ENABLED", "1")
    monkeypatch.setenv("COFFEE_ENABLED", "1")
    monkeypatch.setenv("SNACK_ENABLED", "0")
    names = [m.name for m in config._load_meals()]
    assert names == ["breakfast", "coffee", "lunch", "dinner"]  # daily order


def test_meals_string_overrides(monkeypatch):
    from lunchbot import config
    monkeypatch.setenv("MEALS", "brunch@10:00,supper@19:30")
    names = [m.name for m in config._load_meals()]
    assert names == ["brunch", "supper"]


# -- multi-vote --------------------------------------------------------------

def test_cast_vote_toggles_and_allows_multiple():
    db = Database(":memory:")
    a, b = db.upsert_place("A"), db.upsert_place("B")
    assert db.cast_vote(1, "U1", a) is True   # vote A
    assert db.cast_vote(1, "U1", b) is True   # also vote B (multi-select)
    assert db.vote_tally(1) == {a: 1, b: 1}
    assert db.cast_vote(1, "U1", a) is False  # toggle A off
    assert db.vote_tally(1) == {b: 1}


# -- recency cooldown --------------------------------------------------------

def test_within_cooldown():
    from lunchbot.recommender import within_cooldown
    assert within_cooldown(TODAY.isoformat(), TODAY, 1) is True       # visited today
    assert within_cooldown(date(2026, 6, 17).isoformat(), TODAY, 1) is False  # 2 days ago
    assert within_cooldown(None, TODAY, 1) is False                   # never visited
    assert within_cooldown(TODAY.isoformat(), TODAY, 0) is False      # cooldown off


def test_cooldown_excludes_recent_when_alternatives_exist():
    from lunchbot.recommender import recommend
    recent = Place(id=1, name="Yesterday", cuisine="thai", last_visit=TODAY.isoformat())
    others = [Place(id=i, name=f"P{i}", cuisine="thai") for i in range(2, 6)]
    picks = recommend([recent] + others, ["U1"], today=TODAY, global_c=0.6, tunables=TUN)
    assert all(s.place.id != 1 for s in picks)


def test_cooldown_relaxes_when_too_few_places():
    from lunchbot.recommender import recommend
    # All three visited today; cooldown must relax or there'd be nothing to poll.
    places = [Place(id=i, name=f"P{i}", cuisine="thai", last_visit=TODAY.isoformat())
              for i in range(1, 4)]
    picks = recommend(places, ["U1"], today=TODAY, global_c=0.6, tunables=TUN)
    assert len(picks) == 3


# -- reroll ------------------------------------------------------------------

def test_reroll_gives_fresh_candidates_and_clears_votes():
    svc = _service([(f"P{i}", "both") for i in range(1, 7)])  # 6 places
    poll_id = svc.post_picks("lunch", TODAY)
    original = set(svc.db.poll_candidates(svc.db.get_poll(poll_id)))
    svc.handle_vote(poll_id, "U1", next(iter(original)))
    assert svc.db.vote_tally(poll_id)  # a vote exists

    svc.reroll(poll_id)
    new = set(svc.db.poll_candidates(svc.db.get_poll(poll_id)))
    assert new.isdisjoint(original)        # entirely fresh options
    assert svc.db.vote_tally(poll_id) == {}  # votes reset


# -- stats -------------------------------------------------------------------

def test_stats_empty_before_any_poll():
    assert "No polls" in sa._stats_text(Database(":memory:"))


def test_stats_reports_winner_after_close():
    svc = _service([(f"P{i}", "both") for i in range(1, 5)])
    poll_id = svc.post_picks("lunch", TODAY)
    cands = svc.db.poll_candidates(svc.db.get_poll(poll_id))
    svc.handle_vote(poll_id, "U1", cands[0])
    svc.close_poll(poll_id)
    text = sa._stats_text(svc.db)
    assert "1 poll closed" in text
    assert "Most picked" in text


def test_voter_participation_counts_distinct_closed_polls():
    db = Database(":memory:")
    a, b = db.upsert_place("A"), db.upsert_place("B")
    # Poll 1 (will close): U1 votes for both places — still counts as one poll.
    db.create_poll("lunch", TODAY.isoformat(), [a, b], TODAY.isoformat())
    db.ensure_user("U1", "Alice")
    db.ensure_user("U2", "Bob")
    db.cast_vote(1, "U1", a)
    db.cast_vote(1, "U1", b)
    db.mark_poll_closed(1, a)
    # Poll 2 stays OPEN: a vote here must NOT count toward turnout.
    db.create_poll("lunch", TODAY.isoformat(), [a], TODAY.isoformat())
    db.cast_vote(2, "U1", a)

    part = {r["display"]: r["polls_voted"] for r in db.voter_participation()}
    assert part["Alice"] == 1   # two places + an open poll still = 1 closed poll
    assert part["Bob"] == 0     # known (rated/seen) but never voted


def test_stats_roasts_the_least_active_voter():
    svc = _service([(f"P{i}", "both") for i in range(1, 5)])
    # Alice votes in the poll; Bob is known but sits it out.
    poll_id = svc.post_picks("lunch", TODAY)
    cands = svc.db.poll_candidates(svc.db.get_poll(poll_id))
    svc.handle_vote(poll_id, "U1", cands[0], "Alice")
    svc.db.ensure_user("U2", "Bob")
    svc.close_poll(poll_id)

    text = sa._stats_text(svc.db)
    assert "Least likely to vote" in text
    assert "<@U2>" in text   # the slacker is @-mentioned by Slack id
    assert "haha" in text


def test_stats_skips_roast_when_nobody_voted():
    svc = _service([(f"P{i}", "both") for i in range(1, 5)])
    poll_id = svc.post_picks("lunch", TODAY)
    svc.close_poll(poll_id)  # closed with zero votes
    text = sa._stats_text(svc.db)
    assert "Least likely to vote" not in text


def test_stats_no_roast_when_everyone_voted_equally():
    svc = _service([(f"P{i}", "both") for i in range(1, 5)])
    poll_id = svc.post_picks("lunch", TODAY)
    cands = svc.db.poll_candidates(svc.db.get_poll(poll_id))
    svc.handle_vote(poll_id, "U1", cands[0], "Alice")
    svc.handle_vote(poll_id, "U2", cands[0], "Bob")
    svc.close_poll(poll_id)
    text = sa._stats_text(svc.db)
    assert "Least likely to vote" not in text  # no roast — it's a tie


# -- discover ----------------------------------------------------------------

def test_discover_proposes_without_inserting():
    class DiscoverClaude(FakeClaude):
        enabled = True
        def discover(self, query, limit=8):
            return [{"name": "Pho King", "cuisine": "vietnamese", "price_band": 2},
                    {"name": "Burrito Bros", "cuisine": "mexican"}]
    db = Database(":memory:")
    poll = PollService(db, Config(), FakeSlack(), DiscoverClaude(), lambda *a: None)
    out, respond = _capture()
    sa._discover(poll, db, "cheap asian", respond)
    # Nothing saved -- discover only proposes.
    assert db.find_place("Pho King") is None
    assert db.find_place("Burrito Bros") is None
    # Response carries Add buttons for each suggestion.
    blocks = out[-1]["blocks"]
    actions = [b for b in blocks if b.get("accessory", {}).get("action_id") == "discover_add"]
    assert len(actions) == 2


def test_discover_message_has_add_buttons():
    from lunchbot.blocks import discover_message
    blocks = discover_message("thai", [{"name": "Pok Pok", "cuisine": "thai", "price_band": 2}])
    btn = blocks[1]["accessory"]
    assert btn["action_id"] == "discover_add"
    assert json.loads(btn["value"])["name"] == "Pok Pok"


def test_discover_needs_query():
    db = Database(":memory:")
    poll = PollService(db, Config(), FakeSlack(), FakeClaude(), lambda *a: None)
    out, respond = _capture()
    sa._discover(poll, db, "  ", respond)
    assert "Usage" in out[-1]
