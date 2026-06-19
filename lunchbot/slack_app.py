"""Slack Bolt app (Socket Mode): registers handlers and slash commands.

Golden rule (spec section 10): ack() within 3 seconds, then do slow work. Bolt
acks automatically when the handler returns, so handlers must stay quick -- the
heavy lifting (DB writes, chat_update, Claude) happens inside, but it's all fast
for a single-team workload.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from slack_bolt import App

from .blocks import discover_message

from .config import Config
from .poll import PollService

log = logging.getLogger(__name__)


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

    @app.action("reroll")
    def on_reroll(ack, body):
        ack()
        poll_id = _poll_id_from_message(db, body)
        if poll_id is not None:
            poll.reroll(poll_id)

    @app.action("discover_add")
    def on_discover_add(ack, action, respond):
        ack()
        try:
            item = json.loads(action["value"])
        except (ValueError, KeyError):
            return
        name = _clean_name(item.get("name", ""))
        if not name:
            return
        pid = db.upsert_place(name, cuisine=item.get("cuisine"), source="discover")
        db.enrich_place(pid, item.get("cuisine"), item.get("price_band"))
        respond(
            text=f"Added *{name}*{_meta_suffix(db.get_place(pid))}.",
            replace_original=False,
            response_type="ephemeral",
        )

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

        if sub == "" or sub in poll.config.meal_names:
            poll.post_picks(sub or poll.config.default_meal)
        elif sub == "add" and arg:
            name = _clean_name(arg)
            if not name:
                respond("Usage: `/lunch add <name>`")
                return
            place_id = db.upsert_place(name, source="manual")
            # Best-effort auto-fill cuisine/price (fills only empty fields).
            enriched = poll.claude.enrich(name)
            if enriched:
                db.enrich_place(place_id, enriched.get("cuisine"), enriched.get("price_band"))
            row = db.get_place(place_id)
            detail = _meta_suffix(row)
            respond(
                f"Added *{name}* (place #{place_id}){detail}. "
                f"Set a score with `/lunch score {name} <0-10>`."
            )
        elif sub == "remove" and arg:
            _deactivate_by_name(db, _clean_name(arg), respond)
        elif sub == "list":
            respond(_place_list(db))
        elif sub == "score":
            _set_score(db, arg, respond, poll.config.meal_names | {"both"})
        elif sub == "cuisine":
            _set_cuisine(db, arg, respond)
        elif sub == "stats":
            respond(_stats_text(db))
        elif sub == "discover":
            _discover(poll, db, arg, respond)
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


def _meta_suffix(row) -> str:
    """' — mexican · $$' style suffix from whatever metadata a place has."""
    bits = []
    if row["cuisine"]:
        bits.append(row["cuisine"])
    if row["price_band"]:
        bits.append("$" * int(row["price_band"]))
    return f" — {' · '.join(bits)}" if bits else ""


def _set_cuisine(db, arg, respond):
    """`/lunch cuisine <name> <cuisine>` — manual override for a place's cuisine."""
    tokens = arg.split()
    if len(tokens) < 2:
        respond("Usage: `/lunch cuisine <name> <cuisine>` (e.g. `/lunch cuisine Thai Basil thai`)")
        return
    cuisine = tokens[-1].lower()
    name = _clean_name(" ".join(tokens[:-1]))
    row = db.find_place(name)
    if row is None:
        respond(f"No place matching *{name}*. See `/lunch list`.")
        return
    db.set_place_cuisine(int(row["id"]), cuisine)
    respond(f"Set *{row['name']}* cuisine to *{cuisine}*.")


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
    lines = ["*Places*  _(score /10 · cuisine · price · meal)_"]
    for r in rows:
        score = _score_out_of_10(r)
        score_str = f"{score:.1f}" if score is not None else "—"
        cuisine = r["cuisine"] or "—"
        price = "$" * int(r["price_band"]) if r["price_band"] else "—"
        meal = r["meal"] or "both"
        lines.append(f"• *{r['name']}* — {score_str}/10 · {cuisine} · {price} · {meal}")
    return "\n".join(lines)


def _set_score(db, arg, respond, valid_meals):
    """`/lunch score <name> <0-10> [<meal>|both]`."""
    tokens = arg.split()
    if len(tokens) < 2:
        respond("Usage: `/lunch score <name> <0-10> [<meal>|both]`")
        return

    # An optional trailing meal keyword; everything before the number is the name.
    meal = None
    if tokens[-1].lower() in valid_meals:
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


def _stats_text(db) -> str:
    """Make the learning visible: polls run, most-picked, best-rated."""
    closed = db.conn.execute("SELECT COUNT(*) AS c FROM polls WHERE closed = 1").fetchone()["c"]
    if not closed:
        return "No polls have closed yet — run `/lunch` to start one."

    lines = [f"*Lunch Bot stats* — {closed} poll{'s' if closed != 1 else ''} closed"]

    most_picked = db.conn.execute(
        "SELECT name, times_picked, sum_ratings, num_ratings FROM places "
        "WHERE times_picked > 0 ORDER BY times_picked DESC, name COLLATE NOCASE LIMIT 5"
    ).fetchall()
    if most_picked:
        lines.append("\n🏆 *Most picked*")
        for r in most_picked:
            score = (
                f"{r['sum_ratings'] / r['num_ratings'] * 10:.1f}/10"
                if r["num_ratings"] else "unrated"
            )
            lines.append(f"• *{r['name']}* — won {r['times_picked']}× · {score}")

    best = db.conn.execute(
        "SELECT name, sum_ratings, num_ratings FROM places WHERE num_ratings > 0 "
        "ORDER BY (sum_ratings / num_ratings) DESC, num_ratings DESC LIMIT 5"
    ).fetchall()
    if best:
        lines.append("\n❤️ *Best rated*")
        for r in best:
            avg = r["sum_ratings"] / r["num_ratings"] * 10
            n = r["num_ratings"]
            lines.append(f"• *{r['name']}* — {avg:.1f}/10 ({n} rating{'s' if n != 1 else ''})")

    return "\n".join(lines)


def _discover(poll, db, query, respond):
    """`/lunch discover <cuisine / price / area>` — ask Claude for new spots."""
    query = query.strip()
    if not query:
        respond("Usage: `/lunch discover <what you want>` — e.g. `/lunch discover cheap thai near downtown`")
        return
    if not poll.claude.enabled:
        respond("Discovery needs an Anthropic key set in the bot's config.")
        return
    found = poll.claude.discover(query)
    if not found:
        respond(f"Couldn't find new spots for *{query}*. Try different wording?")
        return
    # Propose only -- nothing is saved until the user taps a place's ➕ Add button.
    respond(text=f"Ideas for {query}", blocks=discover_message(query, found))


def _help_text() -> str:
    return (
        "*Lunch Bot commands*\n"
        "• `/lunch` or `/lunch dinner` — start a poll now\n"
        "• `/lunch stats` — most-picked & best-rated places\n"
        "• `/lunch list` — show all places, score, cuisine, price, meal\n"
        "• `/lunch add <name>` — add a place (auto-detects cuisine & price)\n"
        "• `/lunch discover <cuisine|price|area>` — find new spots to try\n"
        "• `/lunch score <name> <0-10> [lunch|dinner|both]` — set a place's score & meal\n"
        "• `/lunch cuisine <name> <cuisine>` — fix a place's cuisine\n"
        "• `/lunch remove <name>` — stop suggesting a place\n"
        "_Polls also have a 🔄 Reroll button for fresh options._"
    )
