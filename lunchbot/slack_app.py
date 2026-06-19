"""Slack Bolt app (Socket Mode): registers handlers and slash commands.

Golden rule (spec section 10): ack() within 3 seconds, then do slow work. Bolt
acks automatically when the handler returns, so handlers must stay quick -- the
heavy lifting (DB writes, chat_update, Claude) happens inside, but it's all fast
for a single-team workload.
"""

from __future__ import annotations

import logging

from slack_bolt import App

from .config import Config
from .poll import PollService

log = logging.getLogger(__name__)

PREFS = ["vegetarian", "vegan", "halal", "gluten_free"]


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
        user_id = body["user_id"]

        if sub in ("", "lunch", "dinner"):
            slot = sub or "lunch"
            poll.post_picks(slot)
        elif sub == "add" and arg:
            place_id = db.upsert_place(arg, source="manual")
            respond(f"Added *{arg}* (place #{place_id}). It'll show up via exploration.")
        elif sub == "remove" and arg:
            _deactivate_by_name(db, arg, respond)
        elif sub == "skip":
            from datetime import date
            db.ensure_user(user_id, body.get("user_name"))
            for slot in ("lunch", "dinner"):
                db.set_attendance(user_id, date.today().isoformat(), slot, present=False)
            respond("Got it — you're out for today's polls. 🙅")
        elif sub == "in":
            from datetime import date
            db.ensure_user(user_id, body.get("user_name"))
            for slot in ("lunch", "dinner"):
                db.set_attendance(user_id, date.today().isoformat(), slot, present=True)
            respond("You're in for today. 🙌")
        elif sub == "prefs":
            _toggle_pref(db, user_id, body.get("user_name"), arg, respond)
        else:
            respond(_help_text())

    return app


# -- helpers -----------------------------------------------------------------

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


def _toggle_pref(db, user_id, user_name, arg, respond):
    db.ensure_user(user_id, user_name)
    arg = arg.strip().lower()
    if not arg:
        current = db.constraints_for([user_id])
        active = ", ".join(sorted(current)) or "none"
        respond(
            f"Your dietary constraints: *{active}*.\n"
            f"Toggle one with `/lunch prefs <{' | '.join(PREFS)}>`."
        )
        return
    if arg not in PREFS:
        respond(f"Unknown constraint. Options: {', '.join(PREFS)}")
        return
    currently = arg in db.constraints_for([user_id])
    db.set_constraint(user_id, arg, enabled=not currently)
    state = "removed" if currently else "added"
    respond(f"{state.title()} dietary constraint *{arg}*.")


def _help_text() -> str:
    return (
        "*Lunch Bot commands*\n"
        "• `/lunch` or `/lunch dinner` — start a poll now\n"
        "• `/lunch add <name>` — add a place\n"
        "• `/lunch remove <name>` — stop suggesting a place\n"
        "• `/lunch prefs [vegetarian|vegan|halal|gluten_free]` — view/toggle constraints\n"
        "• `/lunch skip` / `/lunch in` — opt out / in for today"
    )
