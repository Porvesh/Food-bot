# Contributing to Lunch Bot

Thanks for helping out! 🍽️ This guide gets you from clone to green checkmark.

## You don't need a Slack workspace

The whole test suite runs **offline** — no tokens, no network, no Slack app. The
recommendation engine is a pure function, and the poll lifecycle is tested against
an in-memory database and a fake Slack client. So for most changes, this is all you need:

```bash
git clone git@github.com:Porvesh/Food-bot.git
cd Food-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest            # 26 tests
ruff check .      # lint
```

CI runs exactly these two commands on Python 3.10–3.12 for every push and PR, so if
they pass locally they'll pass in CI.

## Live Slack testing (only when changing the Slack layer)

If your change touches `slack_app.py`, `blocks.py`, or the message flow, test it against
a real workspace. You'll need your own Slack app and tokens — see the
[README setup section](README.md#create-the-slack-app-one-time). Tips:

- Set `POLL_MINUTES=1` in `.env` so you don't wait 10 minutes per poll.
- Use `/lunch` to fire a poll immediately instead of waiting for the schedule.

## Project layout

| File | Responsibility |
|---|---|
| `recommender.py` | Pure scoring + selection — **the heart**. No Slack/DB imports. |
| `db.py` / `schema.sql` | SQLite data-access layer |
| `poll.py` | Poll lifecycle: post → vote → close → rate |
| `slack_app.py` | Bolt handlers + slash commands |
| `scheduler.py` | APScheduler: daily posts + per-poll closes |
| `blocks.py` | Block Kit rendering (pure) |
| `claude_client.py` | Pitch blurbs + history extraction (graceful fallback) |
| `backfill.py` | One-shot cold-start seeding |

## Guidelines

- **Keep the recommender a transparent formula.** Claude's job is copy and extraction,
  never the ranking. Anything that ranks restaurants should be debuggable math you can
  unit-test, not a model call.
- **Hard dietary constraints are filters, never weights.** The bot must never be able to
  "learn its way past" an allergy.
- **Add a test for new behavior.** Scoring changes go in `tests/test_recommender.py`;
  flow changes go in `tests/test_poll_lifecycle.py`.
- Run `ruff check .` before pushing (CI will fail otherwise).

## Good first issues

See the [roadmap in spec.md](spec.md#13-feature-roadmap). High-leverage v2 items:
regret tracking, the weekly Claude digest, dietary badges on cards, and a
Google Places top-up for cold-start discovery.

## Submitting

1. Fork and branch from `main`.
2. Make your change with a test.
3. Ensure `pytest` and `ruff check .` pass.
4. Open a PR describing what and why. CI must be green before merge.
