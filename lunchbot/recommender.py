"""The recommendation engine -- a pure function of the data.

This module has NO knowledge of SQLite or Slack. It takes plain dataclasses in
and returns picks out, so the scoring math (the heart of the bot) can be unit
tested in isolation. See spec.md sections 6 and 18.

    score(p) = rating_quality(p) * group_fit(p, R) * recency(p) + explore(p)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

NEUTRAL = 0.5  # default predicted liking when a user has no history with a cuisine


@dataclass
class Place:
    id: int
    name: str
    cuisine: Optional[str] = None
    is_active: bool = True
    times_picked: int = 0
    last_visit: Optional[str] = None  # ISO date
    sum_ratings: float = 0.0
    num_ratings: int = 0
    # Cuisines this place can NOT accommodate (drives hard-constraint filtering).
    # e.g. {"vegetarian"} means "no vegetarian option here".
    cannot_accommodate: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Tunables:
    prior_m: float = 8.0
    explore_k: float = 0.3
    recency_halflife_days: float = 7.0


@dataclass
class Scored:
    place: Place
    score: float
    rating_quality: float
    group_fit: float
    recency: float
    explore: float


def bayesian_quality(sum_ratings: float, num_ratings: int, prior_m: float, global_c: float) -> float:
    """Shrink an observed like-rate toward the global mean until trust is earned.

    With num_ratings == 0 this is exactly global_c, so an unrated place sits at
    the global mean and moves only as real ratings arrive.
    """
    v = num_ratings
    if v <= 0:
        return global_c
    r_obs = sum_ratings / v
    return (v / (v + prior_m)) * r_obs + (prior_m / (v + prior_m)) * global_c


def recency_factor(last_visit: Optional[str], today: date, halflife_days: float) -> float:
    """~0 right after a visit, approaching 1 as time passes. 1.0 if never visited."""
    if not last_visit:
        return 1.0
    try:
        days = (today - date.fromisoformat(last_visit)).days
    except ValueError:
        return 1.0
    days = max(days, 0)
    return 1.0 - math.exp(-days / halflife_days)


def explore_bonus(times_picked: int, explore_k: float) -> float:
    """A poor-man's UCB: large for untried places, decaying as they're sampled."""
    return explore_k / math.sqrt(times_picked + 1)


def user_pref(
    user_cuisine_stats: dict[str, dict[str, tuple[float, int]]],
    user_means: dict[str, float],
    slack_id: str,
    cuisine: Optional[str],
    prior_m: float,
) -> float:
    """Predicted liking of a cuisine by one user, Bayesian-shrunk toward their
    own personal mean. Neutral when the user has no history at all."""
    if cuisine is None:
        return NEUTRAL
    personal_mean = user_means.get(slack_id, NEUTRAL)
    stats = user_cuisine_stats.get(slack_id, {})
    if cuisine not in stats:
        return personal_mean
    s, n = stats[cuisine]
    if n <= 0:
        return personal_mean
    obs = s / n
    return (n / (n + prior_m)) * obs + (prior_m / (n + prior_m)) * personal_mean


def group_fit(
    place: Place,
    roster: list[str],
    user_cuisine_stats: dict[str, dict[str, tuple[float, int]]],
    user_means: dict[str, float],
    prior_m: float,
) -> float:
    """Mean predicted liking of this place's cuisine across the present roster."""
    if not roster:
        return NEUTRAL
    return sum(
        user_pref(user_cuisine_stats, user_means, u, place.cuisine, prior_m) for u in roster
    ) / len(roster)


def violates_hard_constraint(place: Place, active_constraints: set[str]) -> bool:
    """True if the place cannot accommodate any active dietary constraint.

    Hard constraints are filters, never penalties -- the bot must never learn
    its way past an allergy.
    """
    return bool(place.cannot_accommodate & active_constraints)


def score_place(
    place: Place,
    roster: list[str],
    *,
    today: date,
    global_c: float,
    tunables: Tunables,
    user_cuisine_stats: dict[str, dict[str, tuple[float, int]]],
    user_means: dict[str, float],
) -> Scored:
    rq = bayesian_quality(place.sum_ratings, place.num_ratings, tunables.prior_m, global_c)
    gf = group_fit(place, roster, user_cuisine_stats, user_means, tunables.prior_m)
    rec = recency_factor(place.last_visit, today, tunables.recency_halflife_days)
    exp = explore_bonus(place.times_picked, tunables.explore_k)
    return Scored(place, rq * gf * rec + exp, rq, gf, rec, exp)


def recommend(
    places: list[Place],
    roster: list[str],
    *,
    today: date,
    global_c: float,
    tunables: Tunables,
    active_constraints: set[str] | None = None,
    user_cuisine_stats: dict[str, dict[str, tuple[float, int]]] | None = None,
    user_means: dict[str, float] | None = None,
    n: int = 3,
) -> list[Scored]:
    """Return n picks: the top scorers plus one exploratory pick from the tail.

    This is the selection logic from spec.md section 6.3: 2 proven picks + 1
    chance to learn something new, de-duplicated.
    """
    active_constraints = active_constraints or set()
    user_cuisine_stats = user_cuisine_stats or {}
    user_means = user_means or {}

    eligible = [
        p
        for p in places
        if p.is_active and not violates_hard_constraint(p, active_constraints)
    ]
    scored = [
        score_place(
            p,
            roster,
            today=today,
            global_c=global_c,
            tunables=tunables,
            user_cuisine_stats=user_cuisine_stats,
            user_means=user_means,
        )
        for p in eligible
    ]
    scored.sort(key=lambda s: s.score, reverse=True)

    if len(scored) <= n:
        return scored

    n_top = max(n - 1, 0)
    picks = scored[:n_top]
    chosen_ids = {s.place.id for s in picks}

    # One exploratory pick: highest explore term among places not already chosen.
    tail = [s for s in scored if s.place.id not in chosen_ids]
    if tail:
        explorer = max(tail, key=lambda s: s.explore)
        picks.append(explorer)

    return picks[:n]
