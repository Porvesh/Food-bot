"""Claude is used for copy and extraction -- never for the recommender core.

Pitch blurbs make the poll feel human; the ranking stays a transparent formula.
Every call degrades gracefully: if there's no API key or the call fails, we fall
back to a plain description so the bot keeps working.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

PITCH_SYSTEM = (
    "You write one-line lunch pitches for a team poll. Given a restaurant name "
    "and cuisine, return a single playful sentence (max 12 words) that makes "
    "people want to vote for it. No emoji, no quotes, no trailing period needed."
)


class ClaudeClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = None
        if api_key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=api_key)
            except Exception as exc:  # pragma: no cover - import/credential issues
                log.warning("Claude disabled (%s); using fallback pitches.", exc)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def pitch(self, name: str, cuisine: Optional[str]) -> str:
        """One-line pitch for a single place, with a safe fallback."""
        fallback = f"{cuisine.title()} at {name}" if cuisine else name
        if not self._client:
            return fallback
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=40,
                system=PITCH_SYSTEM,
                messages=[
                    {"role": "user", "content": f"Name: {name}\nCuisine: {cuisine or 'unknown'}"}
                ],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return text.strip() or fallback
        except Exception as exc:  # pragma: no cover - network/credential issues
            log.warning("Pitch generation failed (%s); using fallback.", exc)
            return fallback

    def extract_places(self, messages: list[dict]) -> list[dict]:
        """Backfill helper: extract restaurant mentions from channel history.

        Returns a list of {index, name, cuisine, signal} dicts. Used by the
        one-shot backfill script (spec.md section 9). Empty list on failure.
        """
        if not self._client:
            return []
        system = (
            "Extract restaurant mentions from Slack messages. Return JSON only: "
            'a list of {"index": int, "name": str, "cuisine": str, "signal": '
            '"went"|"suggested"|"positive"|"negative"}. Skip non-food messages.'
        )
        payload = json.dumps(
            [{"index": i, "text": m.get("text", "")} for i, m in enumerate(messages)]
        )
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": payload}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return json.loads(text)
        except Exception as exc:  # pragma: no cover
            log.warning("History extraction failed: %s", exc)
            return []
