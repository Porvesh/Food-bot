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

ENRICH_SYSTEM = (
    "You label restaurants for a lunch bot. Given a restaurant name, return JSON "
    "only (no prose): "
    '{"cuisine": <one lowercase word like "thai", "ramen", "mexican", "salad", '
    'or null if you truly cannot tell>, "price_band": <1-4 where 1=$ cheap and '
    '4=$$$$ fancy, or null if unsure>}. '
    "Use your general knowledge of well-known places; guess null rather than invent."
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

    def enrich(self, name: str) -> dict:
        """Best-effort {cuisine, price_band} for a restaurant name.

        Returns {} when Claude is disabled or the call fails, so callers can
        treat enrichment as purely additive (the place still gets added).
        """
        if not self._client:
            return {}
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=60,
                system=ENRICH_SYSTEM,
                messages=[{"role": "user", "content": f"Name: {name}"}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(text)
        except Exception as exc:  # pragma: no cover - network/credential/parse issues
            log.warning("Enrichment failed for %r (%s); leaving fields blank.", name, exc)
            return {}

        out: dict = {}
        cuisine = data.get("cuisine")
        if isinstance(cuisine, str) and cuisine.strip():
            out["cuisine"] = cuisine.strip().lower()
        band = data.get("price_band")
        if isinstance(band, int) and 1 <= band <= 4:
            out["price_band"] = band
        return out

    def discover(self, query: str, limit: int = 8) -> list[dict]:
        """Propose real restaurants matching a free-text request (cuisine, price,
        area). Returns a list of {name, cuisine, price_band}; [] on failure."""
        if not self._client:
            return []
        system = (
            "You suggest real, well-known restaurants for a team lunch bot. Given a "
            f"request, return JSON only: a list of up to {limit} objects "
            '{"name": str, "cuisine": <one lowercase word>, "price_band": <1-4>}. '
            "Only real places that plausibly match. No prose, no duplicates."
        )
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": query}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(text)
        except Exception as exc:  # pragma: no cover - network/credential/parse issues
            log.warning("Discovery failed for %r (%s).", query, exc)
            return []
        out = []
        for item in data if isinstance(data, list) else []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            entry = {"name": name}
            cuisine = item.get("cuisine")
            if isinstance(cuisine, str) and cuisine.strip():
                entry["cuisine"] = cuisine.strip().lower()
            band = item.get("price_band")
            if isinstance(band, int) and 1 <= band <= 4:
                entry["price_band"] = band
            out.append(entry)
        return out

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
