"""Configuration loaded from environment (and an optional .env file).

Keeping all env access in one place means the rest of the code reads from a
typed object instead of sprinkling os.getenv everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Default days a meal runs on. Same syntax as APScheduler's day_of_week:
# "mon-fri", "mon,wed,fri", "sun-thu", "0-4", etc. (case-insensitive).
DEFAULT_DAYS = "mon-fri"


@dataclass(frozen=True)
class Meal:
    """A configured poll slot: a name, the time it posts (24h), and the days it
    runs on (`days` is an APScheduler day_of_week expression, e.g. 'mon-fri')."""

    name: str
    hour: int
    minute: int
    days: str = DEFAULT_DAYS


# Built-in meals, in daily order, with default times and default on/off state.
# Each is toggled by <NAME>_ENABLED, timed by <NAME>_TIME, and scoped to days by
# <NAME>_DAYS in the env.
DEFAULT_MEALS = [
    ("breakfast", "08:30", "0"),
    ("coffee", "10:30", "0"),
    ("lunch", "11:30", "1"),
    ("snack", "15:30", "0"),
    ("dinner", "18:00", "1"),
]


def _parse_meals(raw: str) -> tuple[Meal, ...]:
    """Parse a `MEALS` override. Each entry is `name@HH:MM` (runs Mon-Fri) or
    `name@HH:MM@days` to set which days, e.g.
    'lunch@11:00@mon-fri,dinner@18:00@sun-thu,brunch@10:00@sat+sun'.
    Days use APScheduler day_of_week syntax (ranges/abbrevs); since commas already
    split meals here, write day *lists* with `+` instead (sat+sun)."""
    meals = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, rest = chunk.partition("@")
        t, _, days = rest.partition("@")
        hh, _, mm = t.partition(":")
        try:
            meals.append(
                Meal(name.strip().lower(), int(hh), int(mm or 0), _clean_days(days))
            )
        except ValueError:
            continue
    return tuple(meals)


# mon..sun, matching APScheduler's day_of_week numbering (mon=0 ... sun=6).
_DAY_ABBRS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_INDEX = {abbr: i for i, abbr in enumerate(_DAY_ABBRS)}
_DAY_INDEX.update({str(i): i for i in range(7)})  # also accept 0-6


def _clean_days(raw: str) -> str:
    """Normalize a day_of_week expression into an explicit, APScheduler-valid
    day list. Blank falls back to the default.

    Inside `MEALS` the comma already separates meals, so day *lists* are written
    with `+` (or `|`/space) instead -- e.g. `sat+sun`. We translate those to
    commas and expand any `a-b` ranges into their constituent days. Crucially
    this also handles wrap-around weeks like `sun-thu` (a Sunday-off schedule),
    which APScheduler's bare range syntax rejects because sun(6) > thu(3).

    Anything we can't parse is passed through unchanged so APScheduler can
    surface a clear error rather than us silently swallowing a typo."""
    days = raw.strip().lower()
    for sep in ("+", "|", " "):
        days = days.replace(sep, ",")
    if not days:
        return DEFAULT_DAYS

    indices: list[int] = []
    for token in days.split(","):
        token = token.strip()
        if not token:
            continue
        start, sep, end = token.partition("-")
        if sep:  # a range, possibly wrapping (e.g. sun-thu)
            a, b = _DAY_INDEX.get(start.strip()), _DAY_INDEX.get(end.strip())
            if a is None or b is None:
                return days  # unrecognized -- let APScheduler complain
            # Walk forward from a to b cyclically, so sun-thu = sun,mon,tue,wed,thu.
            i = a
            while True:
                indices.append(i)
                if i == b:
                    break
                i = (i + 1) % 7
        else:
            idx = _DAY_INDEX.get(token)
            if idx is None:
                return days
            indices.append(idx)

    # De-dupe, order mon..sun, emit canonical abbreviations.
    seen = sorted(set(indices))
    return ",".join(_DAY_ABBRS[i] for i in seen) or DEFAULT_DAYS


def _load_meals() -> tuple[Meal, ...]:
    """Build the day's meals. A `MEALS` string overrides everything; otherwise
    each built-in meal is on/off via <NAME>_ENABLED, timed via <NAME>_TIME, and
    scoped to days via <NAME>_DAYS."""
    raw = os.getenv("MEALS")
    if raw:
        return _parse_meals(raw) or _parse_meals("lunch@11:30,dinner@18:00")
    meals = []
    for name, default_time, default_on in DEFAULT_MEALS:
        if _flag(f"{name.upper()}_ENABLED", default=default_on):
            hh, _, mm = os.getenv(f"{name.upper()}_TIME", default_time).partition(":")
            days = _clean_days(os.getenv(f"{name.upper()}_DAYS", ""))
            try:
                meals.append(Meal(name, int(hh), int(mm or 0), days))
            except ValueError:
                continue
    return tuple(meals) or _parse_meals("lunch@11:30,dinner@18:00")


@dataclass(frozen=True)
class Tunables:
    """Recommender knobs. Defaults match the spec; override via env."""

    prior_m: float = field(default_factory=lambda: float(os.getenv("RECO_PRIOR_M", "8")))
    explore_k: float = field(default_factory=lambda: float(os.getenv("RECO_EXPLORE_K", "0.3")))
    recency_halflife_days: float = field(
        default_factory=lambda: float(os.getenv("RECO_RECENCY_HALFLIFE_DAYS", "7"))
    )
    # Hard floor: never re-suggest a place visited within this many days (as long
    # as enough other places remain to fill the poll).
    cooldown_days: int = field(default_factory=lambda: int(os.getenv("RECO_COOLDOWN_DAYS", "1")))


@dataclass(frozen=True)
class Config:
    slack_bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=lambda: os.getenv("SLACK_APP_TOKEN", ""))
    channel_id: str = field(default_factory=lambda: os.getenv("LUNCH_CHANNEL_ID", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    )

    tz: str = field(default_factory=lambda: os.getenv("TZ", "America/Los_Angeles"))
    meals: tuple[Meal, ...] = field(default_factory=_load_meals)
    poll_minutes: int = field(default_factory=lambda: int(os.getenv("POLL_MINUTES", "10")))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/lunchbot.db"))

    tunables: Tunables = field(default_factory=Tunables)

    def require(self) -> None:
        """Fail fast with a clear message if required secrets are missing."""
        missing = [
            name
            for name, value in {
                "SLACK_BOT_TOKEN": self.slack_bot_token,
                "SLACK_APP_TOKEN": self.slack_app_token,
                "LUNCH_CHANNEL_ID": self.channel_id,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(missing)
                + "\nCopy .env.example to .env and fill them in."
            )

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def now(self) -> datetime:
        """Current time in the configured timezone, timezone-aware.

        Using this instead of a naive ``datetime.now()`` keeps poll close/rating
        times consistent with the scheduler (created with ``timezone=tz``): a
        naive datetime handed to APScheduler is read in the scheduler's zone, so
        a host running in a different zone than ``TZ`` would otherwise fire jobs
        hours off.
        """
        return datetime.now(self.tzinfo)

    @property
    def meal_names(self) -> set[str]:
        return {m.name for m in self.meals}

    @property
    def default_meal(self) -> str:
        return self.meals[0].name if self.meals else "lunch"


def load_config() -> Config:
    return Config()
