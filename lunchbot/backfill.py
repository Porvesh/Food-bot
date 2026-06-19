"""One-shot cold-start backfill: pull -> extract -> dedup -> seed (spec section 9).

Run once after install to seed the bot from your channel's history so it's useful
on day one:

    python -m lunchbot.backfill --channel C0XXXXXXX --days 180

Without an Anthropic key this still runs but extracts nothing; you can instead
seed manually with `/lunch add <name>` in Slack.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from slack_bolt import App

from .claude_client import ClaudeClient
from .config import load_config
from .db import Database

log = logging.getLogger("lunchbot.backfill")

BATCH = 80


def pull_messages(app: App, channel: str, limit: int) -> list[dict]:
    """Paginate conversations_history; include thread replies."""
    messages: list[dict] = []
    cursor = None
    while len(messages) < limit:
        resp = app.client.conversations_history(channel=channel, limit=200, cursor=cursor)
        batch = resp.get("messages", [])
        messages.extend(batch)
        for m in batch:
            if m.get("reply_count"):
                replies = app.client.conversations_replies(channel=channel, ts=m["ts"])
                messages.extend(replies.get("messages", [])[1:])  # skip the parent
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages[:limit]


def seed(db: Database, extracted: list[dict], messages: list[dict]) -> int:
    """Roll extracted items up per place and insert with priors."""
    by_place: dict[str, dict] = defaultdict(lambda: {"went": 0, "pos": 0, "neg": 0,
                                                     "cuisine": None, "name": None})
    for item in extracted:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        agg = by_place[name.lower()]
        agg["name"] = name
        agg["cuisine"] = agg["cuisine"] or item.get("cuisine")
        signal = item.get("signal")
        if signal == "went":
            agg["went"] += 1
        elif signal == "positive":
            agg["pos"] += 1
        elif signal == "negative":
            agg["neg"] += 1

    created = 0
    for agg in by_place.values():
        place_id = db.upsert_place(agg["name"], cuisine=agg["cuisine"], source="history")
        # times_picked from 'went'; sentiment seeds ratings only when present.
        db.conn.execute(
            "UPDATE places SET times_picked = times_picked + ? WHERE id = ?",
            (agg["went"], place_id),
        )
        total_sent = agg["pos"] + agg["neg"]
        if total_sent:
            db.conn.execute(
                "UPDATE places SET sum_ratings = sum_ratings + ?, num_ratings = num_ratings + ? "
                "WHERE id = ?",
                (float(agg["pos"]), total_sent, place_id),
            )
        created += 1
    db.conn.commit()
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Seed Lunch Bot from channel history.")
    parser.add_argument("--channel", help="channel id (defaults to LUNCH_CHANNEL_ID)")
    parser.add_argument("--days", type=int, default=180, help="(informational) how far back")
    parser.add_argument("--limit", type=int, default=2000, help="max messages to pull")
    args = parser.parse_args()

    config = load_config()
    config.require()
    channel = args.channel or config.channel_id

    app = App(token=config.slack_bot_token)
    db = Database(config.db_path)
    claude = ClaudeClient(config.anthropic_api_key, config.anthropic_model)

    log.info("Pulling up to %d messages from %s...", args.limit, channel)
    messages = pull_messages(app, channel, args.limit)
    log.info("Pulled %d messages.", len(messages))

    if not claude.enabled:
        log.warning("No Anthropic key — skipping extraction. Use /lunch add to seed manually.")
        return

    extracted: list[dict] = []
    for i in range(0, len(messages), BATCH):
        extracted.extend(claude.extract_places(messages[i : i + BATCH]))
    log.info("Extracted %d restaurant mentions.", len(extracted))

    created = seed(db, extracted, messages)
    log.info("Seeded %d places. Bot is ready.", created)


if __name__ == "__main__":
    main()
