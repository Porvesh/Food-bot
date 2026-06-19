"""Slack Bolt app (Socket Mode): registers handlers and slash commands.

Golden rule (spec section 10): ack() within 3 seconds, then do slow work. Bolt
acks automatically when the handler returns, so handlers must stay quick -- the
heavy lifting (DB writes, chat_update, Claude) happens inside, but it's all fast
for a single-team workload.
"""

from __future__ import annotations

import logging
from typing import Optional

from slack_bolt import App

from .config import Config
from .poll import PollService

log = logging.getLogger(__name__)

MEALS = ("lunch", "dinner", "both")


def create_app(config: Config) -> App:
    """Create the Bolt app so its `client` is available before PollService."""
    return App(token=config.slack_bot_token)


def register_handlers(app: App, poll: PollService) -> App:
    db = poll.db

    # -- voting --------------------------------------------------------------

    @app.action("vote")
    def on_vote(ack, body, action):
        ack()
        poll_id = _poll_id_from_message(db, body)
        if poll_id is None:
            return
        user = body["user"]
        poll.handle_vote(poll_id, user["id"], int(action["value"]), user.get("username"))

    # -- rating --------------------------------------------------------------

    @app.action("rate_up")
    def on_rate_up(ack, body, action, respond):
        ack()
        _do_rate(poll, body, action, respond, 1.0)

    @app.action("rate_down")
    def on_rate_down(ack, body, action, respond):
        ack()
        _do_rate(poll, body, action, respond, 0.0)

    # -- slash command -------------------------------------------------------

    @app.command("/lunch")
    def on_lunch(ack, body, respond):
        ack()
        text = (body.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if sub in ("", "lunch", "dinner"):
            slot = sub or "lunch"
            poll.post_picks(slot)
        elif sub == "add" and arg:
            name = _clean_name(arg)
            if not name:
                respond("Usage: `/lunch add <name>`")
                return
            place_id = db.upsert_place(name, source="manual")
            respond(f"Added *{name}* (place #{place_id}). Set a score with `/lunch score {name} <0-10>`.")
        elif sub == "remove" and arg:
            _deactivate_by_name(db, _clean_name(arg), respond)
        elif sub == "list":
            respond(_place_list(db))
        elif sub == "score":
            _set_score(db, arg, respond)
        else:
            respond(_help_text())

    return app


# -- helpers -----------------------------------------------------------------

def _clean_name(raw: str) -> str:
    """A restaurant name is a single line. Guard against pasted multi-line text
    (e.g. a name with the next slash command glued on) by keeping the first line
    and collapsing internal whitespace."""
    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    return " ".join(first_line.split())


def _poll_id_from_message(db, body) -> int | None:
    """Map the clicked message back to its poll via the stored Slack ts."""
    ts = body.get("message", {}).get("ts") or body.get("container", {}).get("message_ts")
    if not ts:
        return None
    row = db.conn.execute("SELECT id FROM polls WHERE ts = ?", (ts,)).fetchone()
    return int(row["id"]) if row else None


def _do_rate(poll: PollService, body, action, respond, value: float):
    poll_id_str, _, place_id_str = action["value"].partition(":")
    user = body["user"]
    poll.handle_rating(
        int(poll_id_str), int(place_id_str), user["id"], value, respond, user.get("username")
    )


def _deactivate_by_name(db, name, respond):
    from .db import canonical_key

    row = db.conn.execute(
        "SELECT id, name FROM places WHERE canonical_key = ?", (canonical_key(name),)
    ).fetchone()
    if row:
        db.set_place_active(int(row["id"]), False)
        respond(f"Removed *{row['name']}* — it won't be suggested again.")
    else:
        respond(f"Couldn't find a place matching *{name}*.")


def _score_out_of_10(row) -> Optional[float]:
    """The place's score on a 0..10 scale: the team-set score if present, else
    the rolled-up rating average, else None (unrated)."""
    if row["manual_score"] is not None:
        return float(row["manual_score"])
    if row["num_ratings"]:
        return float(row["sum_ratings"]) / float(row["num_ratings"]) * 10.0
    return None


def _place_list(db) -> str:
    rows = db.conn.execute(
        "SELECT * FROM places WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    if not rows:
        return "No places yet. Add one with `/lunch add <name>`."
    lines = ["*Places*  _(score out of 10 · meal)_"]
    for r in rows:
        score = _score_out_of_10(r)
        score_str = f"{score:.1f}" if score is not None else "—"
        meal = r["meal"] or "both"
        lines.append(f"• *{r['name']}* — {score_str}/10 · {meal}")
    return "\n".join(lines)


def _set_score(db, arg, respond):
    """`/lunch score <name> <0-10> [lunch|dinner|both]`."""
    tokens = arg.split()
    if len(tokens) < 2:
        respond("Usage: `/lunch score <name> <0-10> [lunch|dinner|both]`")
        return

    # An optional trailing meal keyword; everything before the number is the name.
    meal = None
    if tokens[-1].lower() in MEALS:
        meal = tokens[-1].lower()
        tokens = tokens[:-1]

    try:
        value = float(tokens[-1])
    except ValueError:
        respond("Score must be a number 0–10, e.g. `/lunch score Chipotle 8`.")
        return
    if not 0 <= value <= 10:
        respond("Score must be between 0 and 10.")
        return

    name = _clean_name(" ".join(tokens[:-1]))
    row = db.find_place(name)
    if row is None:
        respond(f"No place matching *{name}*. See `/lunch list` or add it with `/lunch add {name}`.")
        return

    db.set_manual_score(int(row["id"]), value)
    msg = f"Scored *{row['name']}* at *{value:.1f}/10*."
    if meal:
        db.set_place_meal(int(row["id"]), meal)
        msg += f" Shows up for *{meal}*."
    respond(msg)


def _help_text() -> str:
    return (
        "*Lunch Bot commands*\n"
        "• `/lunch` or `/lunch dinner` — start a poll now\n"
        "• `/lunch list` — show all places, their score, and meal\n"
        "• `/lunch add <name>` — add a place\n"
        "• `/lunch score <name> <0-10> [lunch|dinner|both]` — set a place's score & meal\n"
        "• `/lunch remove <name>` — stop suggesting a place"
    )
