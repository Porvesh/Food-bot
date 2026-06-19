"""Configuration loaded from environment (and an optional .env file).

Keeping all env access in one place means the rest of the code reads from a
typed object instead of sprinkling os.getenv everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Meal:
    """A configured poll slot: a name and the time it posts (24h)."""

    name: str
    hour: int
    minute: int


# Built-in meals, in daily order, with default times and default on/off state.
# Each is toggled by <NAME>_ENABLED and timed by <NAME>_TIME in the env.
DEFAULT_MEALS = [
    ("breakfast", "08:30", "0"),
    ("coffee", "10:30", "0"),
    ("lunch", "11:30", "1"),
    ("snack", "15:30", "0"),
    ("dinner", "18:00", "1"),
]


def _parse_meals(raw: str) -> tuple[Meal, ...]:
    """Parse a `MEALS` override like 'breakfast@08:30,lunch@11:00,coffee@15:00'."""
    meals = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, t = chunk.partition("@")
        hh, _, mm = t.partition(":")
        try:
            meals.append(Meal(name.strip().lower(), int(hh), int(mm or 0)))
        except ValueError:
            continue
    return tuple(meals)


def _load_meals() -> tuple[Meal, ...]:
    """Build the day's meals. A `MEALS` string overrides everything; otherwise
    each built-in meal is on/off via <NAME>_ENABLED and timed via <NAME>_TIME."""
    raw = os.getenv("MEALS")
    if raw:
        return _parse_meals(raw) or _parse_meals("lunch@11:30,dinner@18:00")
    meals = []
    for name, default_time, default_on in DEFAULT_MEALS:
        if _flag(f"{name.upper()}_ENABLED", default=default_on):
            hh, _, mm = os.getenv(f"{name.upper()}_TIME", default_time).partition(":")
            try:
                meals.append(Meal(name, int(hh), int(mm or 0)))
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
    def meal_names(self) -> set[str]:
        return {m.name for m in self.meals}

    @property
    def default_meal(self) -> str:
        return self.meals[0].name if self.meals else "lunch"


def load_config() -> Config:
    return Config()
