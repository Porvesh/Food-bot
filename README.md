# 🍽️ Lunch Bot

A self-learning Slack bot that decides where your team eats. It posts a poll, runs
a 10-minute vote, announces the winner, and asks for a one-tap 👍/👎 afterward — and
gets better at picking over time. No ML pipeline; the "memory" is a SQLite file and
the "learning" is a transparent ranking formula.

Runs as one process over a Slack **Socket Mode** connection — no public URL, no
inbound ports.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the tokens below
python -m lunchbot.main
```

### Slack app (one-time)

Create an app at <https://api.slack.com/apps>, enable **Socket Mode**, add bot scopes
`chat:write`, `commands`, `channels:history`, `users:read`, create a `/lunch` slash
command, then collect:

- `SLACK_APP_TOKEN` (`xapp-…`) — Basic Information → App-Level Tokens (`connections:write`)
- `SLACK_BOT_TOKEN` (`xoxb-…`) — Install App → Bot User OAuth Token
- `LUNCH_CHANNEL_ID` (`C0…`) — the channel, then `/invite @Lunch Bot` into it

`ANTHROPIC_API_KEY` is optional — without it the bot still runs with plain-text pitches.

## Commands

| Command | What it does |
|---|---|
| `/lunch` · `/lunch dinner` | Start a poll now |
| `/lunch stats` | Most-picked and best-rated places |
| `/lunch list` | All places — score /10 · cuisine · price · meal |
| `/lunch add <name>` | Add a place (auto-detects cuisine & price) |
| `/lunch discover <cuisine\|price\|area>` | Propose new spots to add (opt-in) |
| `/lunch score <name> <0-10> [lunch\|dinner\|both]` | Set a place's score & meal |
| `/lunch cuisine <name> <cuisine>` | Fix a place's cuisine |
| `/lunch remove <name>` | Stop suggesting a place |

Polls carry **🗳️ Vote** and **🔄 Reroll** buttons; the post-meal prompt is one-tap **👍/👎**.

## How it ranks

For each candidate place `p` and today's roster `R`:

```
score(p) = rating_quality(p) · group_fit(p, R) · recency(p) + explore(p)
```

- **rating_quality** — Bayesian-shrunk 👍 rate (a manual `/lunch score` overrides it)
- **group_fit** — predicted liking of the cuisine across who's present
- **recency** — decays repeats; a **hard 1-day cooldown** stops yesterday's pick reappearing
- **explore** — a bonus for untried places that fades as they're sampled

Dietary constraints are hard **filters**, never weights. The vote-vs-rating gap is the
key signal: a place that wins votes but earns 👎 quietly drops out.

## Scheduling

Daily polls at 11:00 and 18:00 (Mon–Fri, configurable `TZ`/times), and a nightly
SQLite backup at 03:00 → `data/backups/` (14 kept). On restart, open polls are
recovered. To run always-on, host the process under launchd / a small box (Socket
Mode dials out, so no ports to open).

## Cold start

Seed from an existing channel's history (pull → extract with Claude → dedup → seed):

```bash
python -m lunchbot.backfill --channel C0XXXXXXX
```

## Development

```bash
pip install -r requirements-dev.txt
pytest        # 56 tests
ruff check .
```

| File | Responsibility |
|---|---|
| `recommender.py` | Pure scoring + selection (no Slack/DB) |
| `db.py` / `schema.sql` | SQLite data layer |
| `poll.py` | Poll lifecycle: post → vote → close → rate → reroll |
| `slack_app.py` | Bolt handlers + slash commands |
| `scheduler.py` | Daily polls, per-poll closes, nightly backup |
| `blocks.py` | Block Kit rendering (pure) |
| `claude_client.py` | Pitches, cuisine/price enrichment, discovery, extraction |
| `backfill.py` | One-shot cold-start seeding |
| `main.py` | Wires it together, runs Socket Mode |

## License

[MIT](LICENSE)
