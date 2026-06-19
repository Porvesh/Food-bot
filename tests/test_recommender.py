"""Unit tests for the scoring math and selection logic (spec.md section 16)."""

from datetime import date

import pytest

from lunchbot.recommender import (
    Place,
    Tunables,
    bayesian_quality,
    explore_bonus,
    recency_factor,
    recommend,
    score_place,
    user_pref,
    violates_hard_constraint,
)

TODAY = date(2026, 6, 18)
TUN = Tunables(prior_m=8, explore_k=0.3, recency_halflife_days=7)


# -- Bayesian shrinkage -------------------------------------------------------

def test_unrated_place_sits_at_global_mean():
    assert bayesian_quality(0.0, 0, prior_m=8, global_c=0.6) == 0.6


def test_few_ratings_stay_close_to_prior():
    # 2 perfect ratings should NOT beat the prior by much.
    q = bayesian_quality(2.0, 2, prior_m=8, global_c=0.6)
    assert 0.6 < q < 0.72


def test_many_ratings_approach_observed():
    q = bayesian_quality(40 * 0.9, 40, prior_m=8, global_c=0.6)
    assert q == pytest.approx(0.85, abs=0.01)


def test_worked_example_from_spec():
    # Spec section 7: a taco spot over three visits, C=0.60, m=8.
    assert bayesian_quality(6 * 0.67, 6, 8, 0.60) == pytest.approx(0.63, abs=0.01)
    assert bayesian_quality(12 * 0.75, 12, 8, 0.60) == pytest.approx(0.69, abs=0.01)
    assert bayesian_quality(18 * 0.83, 18, 8, 0.60) == pytest.approx(0.76, abs=0.01)


# -- Exploration --------------------------------------------------------------

def test_explore_decreases_with_times_picked():
    bonuses = [explore_bonus(t, 0.3) for t in range(0, 10)]
    assert all(b1 > b2 for b1, b2 in zip(bonuses, bonuses[1:]))


def test_explore_untried_is_largest():
    assert explore_bonus(0, 0.3) == pytest.approx(0.3)


# -- Recency ------------------------------------------------------------------

def test_recency_never_visited_is_one():
    assert recency_factor(None, TODAY, 7) == 1.0


def test_recency_zero_right_after_visit():
    assert recency_factor(TODAY.isoformat(), TODAY, 7) == pytest.approx(0.0)


def test_recency_rises_over_a_week():
    week_ago = date(2026, 6, 11).isoformat()
    assert recency_factor(week_ago, TODAY, 7) == pytest.approx(1 - pow(2.718281828, -1), abs=0.02)


# -- Per-user cuisine preference ----------------------------------------------

def test_user_pref_neutral_without_history():
    assert user_pref({}, {}, "U1", "thai", 8) == 0.5


def test_user_pref_uses_personal_mean_for_new_cuisine():
    means = {"U1": 0.8}
    stats = {"U1": {"ramen": (3.0, 3)}}
    # No thai history -> falls back to personal mean.
    assert user_pref(stats, means, "U1", "thai", 8) == 0.8


# -- Hard constraints ---------------------------------------------------------

def test_constraint_filters_place():
    p = Place(id=1, name="Steakhouse", cuisine="steak", cannot_accommodate=frozenset({"vegetarian"}))
    assert violates_hard_constraint(p, {"vegetarian"})
    assert not violates_hard_constraint(p, {"halal"})


def test_recommend_excludes_constrained_place():
    places = [
        Place(id=1, name="Steak", cuisine="steak", cannot_accommodate=frozenset({"vegetarian"})),
        Place(id=2, name="Salad", cuisine="salad"),
        Place(id=3, name="Thai", cuisine="thai"),
        Place(id=4, name="Ramen", cuisine="ramen"),
    ]
    picks = recommend(
        places, ["U1"], today=TODAY, global_c=0.6, tunables=TUN,
        active_constraints={"vegetarian"},
    )
    assert all(s.place.id != 1 for s in picks)


def test_inactive_place_never_suggested():
    places = [
        Place(id=1, name="Closed", cuisine="thai", is_active=False),
        Place(id=2, name="Open A", cuisine="salad"),
        Place(id=3, name="Open B", cuisine="ramen"),
        Place(id=4, name="Open C", cuisine="pizza"),
    ]
    picks = recommend(places, ["U1"], today=TODAY, global_c=0.6, tunables=TUN)
    assert all(s.place.id != 1 for s in picks)


# -- Selection ----------------------------------------------------------------

def test_recommend_returns_three_distinct():
    places = [Place(id=i, name=f"P{i}", cuisine="thai") for i in range(1, 8)]
    picks = recommend(places, ["U1"], today=TODAY, global_c=0.6, tunables=TUN)
    assert len(picks) == 3
    assert len({s.place.id for s in picks}) == 3


def test_recommend_includes_an_explorer():
    # A well-rated, frequently-picked favorite plus a pile of untried places.
    favorite = Place(id=1, name="Fav", cuisine="thai", sum_ratings=40, num_ratings=40, times_picked=30)
    untried = [Place(id=i, name=f"New{i}", cuisine="thai", times_picked=0) for i in range(2, 6)]
    picks = recommend([favorite] + untried, ["U1"], today=TODAY, global_c=0.6, tunables=TUN)
    # At least one untried place should be surfaced by the exploration term.
    assert any(s.place.times_picked == 0 for s in picks)


# -- Property: consistent 👍 must monotonically raise quality -----------------

def test_consistent_thumbs_up_monotonically_rises():
    qualities = []
    s, n = 0.0, 0
    for _ in range(10):
        s += 1.0
        n += 1
        qualities.append(bayesian_quality(s, n, prior_m=8, global_c=0.6))
    assert all(q1 < q2 for q1, q2 in zip(qualities, qualities[1:]))


def test_score_components_are_exposed():
    p = Place(id=1, name="P", cuisine="thai", sum_ratings=8, num_ratings=10)
    scored = score_place(
        p, ["U1"], today=TODAY, global_c=0.6, tunables=TUN,
        user_cuisine_stats={}, user_means={},
    )
    assert scored.score == pytest.approx(
        scored.rating_quality * scored.group_fit * scored.recency + scored.explore
    )
