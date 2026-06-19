"""Block Kit message rendering.

Pure functions: data in, Slack block list out. No network here so the rendered
output can be asserted in tests (spec.md section 16). The action ids and value
encodings here are the contract the Slack handlers in slack_app.py depend on.
"""

from __future__ import annotations

import json
from typing import Optional


def candidate_card(place: dict, pitch: str, votes: int, *, closed: bool, is_winner: bool) -> list[dict]:
    """One restaurant card: name (+ cuisine) and a Vote button showing the count."""
    cuisine = place["cuisine"]
    name = place["name"]
    title = f"*{name}*" + (f"  ·  {cuisine}" if cuisine else "")
    if is_winner:
        title = f"🏆  {title}"

    section: dict = {"type": "section", "text": {"type": "mrkdwn", "text": title}}
    if closed:
        section["text"]["text"] += f"\n{votes} vote" + ("" if votes == 1 else "s")
    else:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": f"Vote · {votes}"},
            "action_id": "vote",
            "value": str(place["id"]),
        }
    return [section]


def poll_message(
    slot: str,
    cards: list[tuple[dict, str, int]],
    *,
    closed: bool = False,
    winner_id: Optional[int] = None,
) -> list[dict]:
    """Full poll message. `cards` is a list of (place, pitch, votes)."""
    if closed and winner_id is not None:
        header = f"{slot.title()} — winner"
    elif closed:
        header = f"{slot.title()} — closed"
    else:
        header = f"Where should we go for {slot}?"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
    ]
    for place, pitch, votes in cards:
        blocks += candidate_card(
            place, pitch, votes, closed=closed, is_winner=(place["id"] == winner_id)
        )
    if not closed:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔄 Reroll"},
                        "action_id": "reroll",
                    }
                ],
            }
        )
    return blocks


def discover_message(query: str, suggestions: list[dict]) -> list[dict]:
    """Proposed spots from /lunch discover. Each has an Add button -- nothing is
    saved until the user explicitly taps it."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Ideas for “{query}”* — tap ➕ to add one:"},
        }
    ]
    for s in suggestions:
        bits = []
        if s.get("cuisine"):
            bits.append(s["cuisine"])
        if s.get("price_band"):
            bits.append("$" * int(s["price_band"]))
        meta = f"  ·  {' · '.join(bits)}" if bits else ""
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{s['name']}*{meta}"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "➕ Add"},
                    "action_id": "discover_add",
                    "value": json.dumps(s)[:1900],
                },
            }
        )
    return blocks


def poll_fallback_text(slot: str, closed: bool = False) -> str:
    return f"{slot.title()} poll {'results' if closed else 'is open'}"


def rating_message(place_name: str, poll_id: int, place_id: int) -> list[dict]:
    """Post-meal 👍/👎 prompt for the winning place."""
    value = f"{poll_id}:{place_id}"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"How was *{place_name}*?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👍"},
                    "style": "primary",
                    "action_id": "rate_up",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👎"},
                    "action_id": "rate_down",
                    "value": value,
                },
            ],
        },
    ]


def rating_done_message(place_name: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"Thanks — noted how *{place_name}* went."},
        }
    ]
