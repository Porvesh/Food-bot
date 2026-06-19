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
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    )

    tz: str = field(default_factory=lambda: os.getenv("TZ", "America/Los_Angeles"))
    lunch_time: str = field(default_factory=lambda: os.getenv("LUNCH_TIME", "11:00"))
    dinner_time: str = field(default_factory=lambda: os.getenv("DINNER_TIME", "18:00"))
    lunch_enabled: bool = field(default_factory=lambda: _flag("LUNCH_ENABLED"))
    dinner_enabled: bool = field(default_factory=lambda: _flag("DINNER_ENABLED"))
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

    def parse_time(self, value: str) -> tuple[int, int]:
        hour, _, minute = value.partition(":")
        return int(hour), int(minute or 0)


def load_config() -> Config:
    return Config()
