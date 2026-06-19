# 🍽️ Lunch Bot

A self-learning Slack bot that suggests where your team eats. Twice a day it posts
three candidate spots, runs a 10-minute poll, announces the winner, and collects a
one-tap 👍/👎 rating. Over time it learns the team's taste from those taps and gets
better at picking — **with no model training and no ML pipeline to babysit.**

The "memory" is just a SQLite file. "Learning" is a transparent, debuggable formula
ranking restaurants from accumulated votes and ratings — see [How it learns](#how-it-learns).

```
┌────────────────────────────────────────────────────────┐
│                   lunch-bot process                     │
│  Slack (Bolt, Socket Mode) ⇄ Slack API                  │
│  Scheduler (APScheduler) → post at 11:00 & 18:00        │
│  Recommender ⇄ SQLite (the "memory")                    │
│  Claude API (pitch blurbs, history extraction)          │
└────────────────────────────────────────────────────────┘
```

One always-on process. No queue, no broker, no public HTTP endpoint (Socket Mode
dials out, so there are no inbound ports to expose).

---

## Quick start

```bash
git clone https://github.com/Porvesh/Food-bot.git
cd Food-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then fill in your tokens (see below)
python -m lunchbot.main
```

### Create the Slack app (one-time)

1. Create an app at <https://api.slack.com/apps> → **From scratch**.
2. **Socket Mode** → enable it. This generates an app-level token `xapp-…`
   (give it the `connections:write` scope). → `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → add bot scopes:
   `chat:write`, `commands`, `channels:history`, `users:read`
   (add `groups:history` for private channels).
4. **Slash Commands** → create `/lunch`.
5. **Install to Workspace** → copy the Bot User OAuth Token `xoxb-…` → `SLACK_BOT_TOKEN`.
6. Invite the bot: `/invite @your-bot` in the target channel, then grab the channel
   id (right-click channel → *View channel details* → bottom) → `LUNCH_CHANNEL_ID`.

`ANTHROPIC_API_KEY` is optional — without it the bot still runs and falls back to
plain pitch text.

### Seed it from history (optional but recommended)

```bash
python -m lunchbot.backfill --channel C0XXXXXXX
```

This pulls channel history, extracts restaurant mentions with Claude, and seeds the
database with priors so the bot's first suggestion already reflects months of team
behavior. Without an API key, seed manually with `/lunch add <name>`.

---

## Usage

| Command | What it does |
|---|---|
| `/lunch` / `/lunch dinner` | Start a poll right now |
| `/lunch add <name>` | Add a restaurant |
| `/lunch remove <name>` | Stop suggesting a restaurant |
| `/lunch prefs [vegetarian\|vegan\|halal\|gluten_free]` | View / toggle your dietary constraints |
| `/lunch skip` / `/lunch in` | Opt out / in for today's polls |

Voting is one tap; tapping a different place moves your vote. After the meal the bot
asks 👍/👎 — that single tap is what makes it learn.

---

## How it learns

For each candidate place `p` and today's present roster `R`:

```
score(p) = rating_quality(p) · group_fit(p, R) · recency(p) + explore(p)
```

- **rating_quality** — a Bayesian-shrunk like-rate, so a 5★ place with 2 votes can't
  beat a 4.5★ with 40. Unrated places sit at the global mean and move only as real
  ratings arrive.
- **group_fit** — each present person's predicted liking of the cuisine, averaged.
  Same week, different roster → different rankings.
- **recency** — decays repeats; a place just visited is downweighted, recovering over ~a week.
- **explore** — a bonus for untried places that decays as they're sampled, so the bot
  keeps discovering instead of converging on the same three spots.

Hard dietary constraints (allergies, vegetarian) are **filters, never weights** — the
bot can never "learn its way past" an allergy.

The most useful signal it collects is the gap between **votes** (what *sounds* good)
and **ratings** (what *was* good): a place that keeps winning votes but earning 👎
quietly drops out of suggestions.

Full design rationale lives in [spec.md](spec.md).

---

## Development

```bash
pip install -r requirements-dev.txt
pytest            # 26 tests: scoring math, learning properties, poll lifecycle
ruff check .
```

The recommendation engine ([lunchbot/recommender.py](lunchbot/recommender.py)) is a
**pure function** with no Slack or DB imports, so the scoring math is unit-tested in
isolation. The poll lifecycle is tested end-to-end against an in-memory DB and a fake
Slack client.

### Layout

| File | Responsibility |
|---|---|
| `recommender.py` | Pure scoring + selection (the heart) |
| `db.py` / `schema.sql` | SQLite data-access layer |
| `poll.py` | Poll lifecycle: post → vote → close → rate |
| `slack_app.py` | Bolt handlers + slash commands |
| `scheduler.py` | APScheduler: daily posts + per-poll closes |
| `blocks.py` | Block Kit message rendering (pure) |
| `claude_client.py` | Pitch blurbs + history extraction (graceful fallback) |
| `backfill.py` | One-shot cold-start seeding |
| `main.py` | Wires it all together, runs Socket Mode |

---

## Deploy

Any always-on box works (Fly.io, Railway, a Raspberry Pi, a small EC2). Socket Mode
dials out, so no inbound ports. Set the env vars from `.env.example`, mount a volume
for `DB_PATH`, and run `python -m lunchbot.main`. On restart, the bot recovers any
open poll — re-scheduling its close or closing it immediately if the window passed.

## Contributing

Issues and PRs welcome. See the roadmap in [spec.md](spec.md#13-feature-roadmap)
(regret tracking, weekly Claude digest, dietary badges, NL requests). Keep the
recommender a transparent formula — Claude's job here is copy and extraction, not
the ranking core.

## License

[MIT](LICENSE)
