# Lunch Bot — Engineering Specification

A self-learning Slack bot that suggests where the team eats. Twice a day it posts
three candidate spots, runs a 10-minute poll, announces the winner, and collects a
one-tap rating. Over time it learns the team's taste from those taps and gets better
at picking — with no model training and no ML pipeline to babysit.

- **Status:** draft v1
- **Owner:** _TBD_
- **Surface:** single Slack channel, one team
- **Billing context:** company pays for meals, so the bot suggests only — it never
  places orders, consolidates carts, or splits bills.

---

## 1. Goals and non-goals

### Goals
- Post three eat-out suggestions at 11:00 (lunch) and 18:00 (dinner) on workdays.
- Let the team vote with one tap; one vote per person, changeable until the poll closes.
- Close each poll after exactly 10 minutes, announce a single winner, log the visit.
- Collect a one-tap 👍/👎 rating on the winner after the meal.
- Improve suggestion quality over time from accumulated votes, ratings, and attendance.
- Bootstrap from existing channel history so the bot is useful on day one.
- Stay operationally trivial: one always-on process, one embedded database.



## 2. Scope and constraints

- One Slack channel (`#lunch` or similar), one geographic location (the office).
- Workdays only (Mon–Fri by default; holiday calendar optional in v2).
- Two slots per day: `lunch` (11:00) and `dinner` (18:00). Either can be disabled.
- Poll window is fixed at 10 minutes. Winner is decided at the cutoff, not before.
- One winning spot per poll (so up to two winners per day across both slots).
- Timezone is a single configured value (e.g. `America/Los_Angeles`).

---

## 3. Architecture

A single long-running process holds everything: the Slack connection, the scheduler,
the database handle, and the scoring logic. There is no queue, no broker, no public
HTTP endpoint.

```
+------------------------------------------------------------+
|                    lunch-bot process                       |
|                                                            |
|  Slack (Bolt, Socket Mode) <----websocket----> Slack API   |
|     |  posts polls, receives votes/ratings                 |
|     v                                                      |
|  Scheduler (APScheduler / cron)                            |
|     |  fires post_picks at 11:00 & 18:00                   |
|     |  fires close_poll 10 min after each post             |
|     v                                                      |
|  Recommender  ---reads/writes--->  SQLite (the "memory")   |
|     |                                                      |
|     v                                                      |
|  Claude API  (extraction, weekly digest, pitch blurbs)     |
+------------------------------------------------------------+
```

### Component choices

| Concern        | Choice                          | Rationale                                              |
|----------------|---------------------------------|-------------------------------------------------------|
| Slack I/O      | Bolt SDK in **Socket Mode**     | No public URL / ngrok / webhook endpoint needed.      |
| Scheduling     | APScheduler (in-process)        | Two daily jobs + per-poll one-shots. No Celery/Redis. |
| Storage        | **SQLite** (file)               | One team, a few hundred rows/year. Trivially backed up.|
| LLM            | Claude (Sonnet) via API/Bedrock | Extraction, weekly pattern digest, pitch blurbs.      |
| Discovery      | Google Places (optional)        | Inject untried nearby spots for exploration.          |
| Hosting        | Any always-on box (Fly/Railway/EC2/Pi) | Socket Mode dials out, so no inbound ports.    |

**Why Socket Mode:** the bot opens an outbound websocket to Slack. Slack pushes
button clicks and slash commands down that socket; the bot calls the Web API to post.
This removes the entire "expose a public endpoint and verify request signatures"
problem, which is the usual first wall people hit.

---

## 4. Daily flow and UX

### Lifecycle of one poll

1. **Trigger** (11:00 or 18:00): scheduler calls `post_picks(slot)`.
2. **Suggest:** recommender selects 3 candidates and Claude writes a one-line pitch each.
3. **Post:** a Block Kit message with three "Vote" buttons. Save the message `ts`.
4. **Schedule close:** a one-shot job is registered for `now + 10 minutes`.
5. **Voting window (10 min):** members tap a place. One vote per person; tapping a
   different place moves their vote. The message updates in place with live tallies.
6. **Close:** at the cutoff, `close_poll` runs: tally, break ties, mark closed,
   re-render the message **without buttons**, announce the winner, log the visit.
7. **Rate:** the bot posts a 👍/👎 prompt for the winner (or schedules it for after
   the meal, e.g. lunch rating prompt at 13:30).
8. **Learn:** the rating writes a row; tomorrow's scoring reads it.

### Message states

- **Open poll:** header + 3 cards, each with name · cuisine · ⭐avg · pitch · Vote button + running count.
- **Closed poll:** same cards, buttons removed, winner marked 🏆 with final vote count.
- **Rating prompt:** "How was \<winner\>?" + 👍 / 👎 buttons.

### "Closed" enforcement

Slack has no native poll lock. A poll is "closed" by (a) a `poll.closed` flag in the
DB checked idempotently in `close_poll`, and (b) re-rendering the message without
buttons so late clicks have nothing to click. Any action handler also rejects votes
against a poll whose `closed` flag is set.

---

## 5. Data model (SQLite)

```sql
-- Restaurants and their rolled-up stats (the core "what we know" table)
CREATE TABLE places (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,           -- canonical display name
    canonical_key TEXT NOT NULL UNIQUE,    -- lowercased/deduped match key
    cuisine       TEXT,                    -- one word: thai, ramen, salad, ...
    price_band    INTEGER,                 -- 1..4 ($..$$$$), nullable
    is_active     INTEGER DEFAULT 1,       -- 0 = closed/removed, never suggest
    times_picked  INTEGER DEFAULT 0,       -- # of times it WON a poll
    last_visit    TEXT,                    -- ISO date of last win, nullable
    sum_ratings   REAL DEFAULT 0,          -- sum of rating values (0..1 each)
    num_ratings   INTEGER DEFAULT 0,       -- # of ratings collected
    source        TEXT DEFAULT 'history',  -- history | places_api | manual
    created_at    TEXT DEFAULT (datetime('now'))
);

-- People (lazily created on first interaction)
CREATE TABLE users (
    slack_id   TEXT PRIMARY KEY,
    display    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Hard dietary constraints — a FILTER, never a learned weight
CREATE TABLE constraints (
    slack_id TEXT,
    kind     TEXT,    -- vegetarian | vegan | halal | gluten_free | allergy:peanut | ...
    PRIMARY KEY (slack_id, kind)
);

-- One row per poll
CREATE TABLE polls (
    id         INTEGER PRIMARY KEY,
    slot       TEXT,            -- lunch | dinner
    date       TEXT,            -- ISO date
    ts         TEXT,            -- Slack message timestamp (also the update handle)
    candidates TEXT,            -- JSON array of place ids offered
    winner_id  INTEGER,         -- set at close
    closed     INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- One row per vote (latest wins per user per poll via upsert)
CREATE TABLE votes (
    poll_id  INTEGER,
    slack_id TEXT,
    place_id INTEGER,
    voted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (poll_id, slack_id)
);

-- One row per rating (post-meal thumbs)
CREATE TABLE ratings (
    poll_id  INTEGER,
    slack_id TEXT,
    place_id INTEGER,
    value    REAL,             -- 1.0 = up, 0.0 = down (room for 1..5 later)
    rated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (poll_id, slack_id)
);

-- Per-user RSVP / attendance for a given slot+date (who to optimize for)
CREATE TABLE attendance (
    slack_id TEXT,
    date     TEXT,
    slot     TEXT,
    present  INTEGER,           -- 1 = in, 0 = out
    PRIMARY KEY (slack_id, date, slot)
);
```

Derived facts (per-user cuisine preference, global like-rate, etc.) are computed on
the fly from `ratings` joined to `places.cuisine`; they are not separate tables. The
DB is the entire memory of the system — there are no model weights anywhere.

---

## 6. The recommendation engine

The engine is a pure function of the tables above. Given a slot and the present
roster, it filters out disqualified places, scores the rest, and returns three.

### 6.1 Candidate filtering (hard constraints first)

Before any scoring, drop any place that:
- is inactive (`is_active = 0`),
- violates a **hard** dietary constraint of anyone present (e.g. a place with no
  vegetarian option when a vegetarian is present),
- was the winner of the previous slot/day within the recency floor (optional).

Hard constraints are filters, never penalties. The bot must never "learn its way
past" an allergy.

### 6.2 Score

For each surviving candidate place `p`, given today's present roster `R`:

```
score(p) = rating_quality(p) * group_fit(p, R) * recency(p) + explore(p)
```

**rating_quality — Bayesian shrinkage.** A 5★ place with 2 votes must not beat a
4.5★ with 40. Shrink the observed average toward the global mean until a place earns
trust:

```
v = p.num_ratings
R_obs = p.sum_ratings / v            (the place's observed like-rate, if v > 0)
C = global like-rate across all places
m = prior strength (e.g. 8)

rating_quality(p) = (v / (v + m)) * R_obs + (m / (v + m)) * C
```

With `v = 0` this is exactly `C` — an unrated place sits at the global mean and rises
or falls only as real ratings arrive.

**group_fit — who's actually in the room.** Each present person's predicted liking
of this place's cuisine, averaged. A person's predicted liking is their own historical
average rating for that cuisine (Bayesian-shrunk the same way, toward their personal
mean), defaulting to neutral when they have no history with it:

```
group_fit(p, R) = mean( user_pref(u, p.cuisine) for u in R )
```

This is what makes "places more users like get suggested more," and what makes the
same week produce different rankings depending on who RSVP'd.

**recency — don't repeat yourself.**

```
days = today - p.last_visit            (large if never visited)
recency(p) = 1 - exp(-days / 7)        (~0 right after a visit, -> 1 after a week)
```

**explore — the term that makes it learn instead of ossify.** Untried or
rarely-picked places get a bonus that decays as they're sampled:

```
explore(p) = K / sqrt(p.times_picked + 1)
```

Without this term the engine converges to the same three spots within a week and never
discovers anything new — the opposite of learning. This is a poor-man's UCB; swap in
Thompson sampling later if desired, but this is sufficient.

### 6.3 Selection

Rank by `score` descending, then return:
- **2 picks from the top** of the ranked list, and
- **1 pick from the exploratory tail** (highest `explore` among untried/low-sample),

so every poll offers proven options plus one chance to learn something new. De-dup so
the same place can't appear twice.

### 6.4 Tunables

| Symbol | Meaning                         | Suggested start |
|--------|---------------------------------|-----------------|
| `m`    | Bayesian prior strength         | 8               |
| `C`    | global like-rate prior          | computed (≈0.6) |
| `K`    | exploration weight              | 0.3             |
| half-life in `recency` | days              | 7               |
| roster default | if no RSVP data         | all known users |

---

## 7. How it self-learns

There is no model and no training step. "Learning" is the loop: every tap writes a
row → rows roll up into per-place and per-user stats → a fixed formula ranks from
those stats → the ranking changes because the stats changed. The memory lives in the
tables, not in weights.

### Worked example — a new taco spot over three visits

Thumbs are 1/0. Global like-rate `C = 0.60`, prior `m = 8`.

- **Visit 1:** never tried, so `rating_quality = C = 0.60` and `explore` is large → it
  gets offered because it's *unknown*, not because it's good. It wins; 4 of 6 thumbs up.
  `rating_quality = (6·0.67 + 8·0.60) / 14 = 0.63`
- **Visit 2:** 5 of 6 up. Cumulative 9/12.
  `rating_quality = (12·0.75 + 8·0.60) / 20 = 0.69`
- **Visit 3:** 6 of 6 up. Cumulative 15/18.
  `rating_quality = (18·0.83 + 8·0.60) / 26 = 0.76`

It climbs toward its true ~0.8 as confidence accumulates — that *is* "learning the
place is good." Meanwhile `explore` decays (`K/√2 → K/√3 → K/√4`), so the bonus that
got it tried fades and it competes on merit. A bad place runs the same trace downward
and quietly stops appearing.

### The highest-value signal: vote ≠ rating

Votes measure what *sounds* good; ratings measure what *was* good. The gap is the
single most useful thing the bot collects:

- A place that keeps **winning votes but earning 👎** has high intent, low quality. Its
  `rating_quality` stays low, so it drops out of suggestions even though people keep
  voting for the name — the bot corrects a popularity bias the group can't.
- A quiet place that **rarely wins votes but earns 👍 every time** climbs via
  `rating_quality + explore` and starts getting surfaced.

---

## 8. User preferences

### Implicit (the main engine — zero setup)
Derived from taps, never asked:
- **Ratings** → ground-truth quality per place and (rolled up by cuisine) per person.
- **Votes** → intent / appeal (distinct from quality; the gap is signal, see §7).
- **RSVP / attendance** → who to optimize for on a given day.

Each person's rating history *is* their profile. There is no "set your preferences"
requirement for the system to work.

### Explicit (only what can't be safely inferred)
- **Hard constraints** (diet, allergies) via a `/lunch prefs` modal — applied as a
  filter (§6.1), never a learned weight.
- **Optional soft prefs** (favorites, blocklist, spice/budget) — bias scoring, v2.

---

## 9. Cold start — bootstrap from channel history

Slack history is the ideal seed: it's restaurant names *with frequency and dates baked
in*, which is exactly what the engine wants on day one. Pipeline: **pull → extract →
dedup → seed with priors**, run once as a one-shot script.

### 9.1 Pull (include threads)
Paginate `conversations_history`; for any message with replies, also pull
`conversations_replies` — food chatter often lives in threads.

### 9.2 Extract with Claude
Raw messages are messy ("🌮 taco bell?", "that ramen place was mid"). Batch ~80 at a
time to Claude with a JSON-only system prompt that emits, per relevant message:
`{ index, name, cuisine, signal }` where `signal ∈ {went, suggested, positive,
negative}`. Each item carries the message `ts`, giving `last_visit` and frequency for
free.

### 9.3 Dedup / canonicalize
Collapse "chipotle" / "Chipotle" / "chipotle mexican grill" into one row via a
lowercase+strip key plus fuzzy match (e.g. `rapidfuzz` token-sort > 88), or one final
Claude "merge these duplicate names" call when the unique list is short.

### 9.4 Seed with priors (not a flat list)
Roll extracted items up per place and insert:
- `times_picked` ← count of `went`
- `last_visit` ← max ts
- `avg_rating` ← `positive / (positive + negative)` **only if** sentiment was found;
  otherwise leave null so the place starts at the prior `C` + exploration.

The bot's first-ever suggestion then already reflects months of team behavior:
frequent old favorites rank high, nothing recent gets the recency boost, anything with
a sour comment starts downweighted.

### 9.5 Top up with untried spots (avoid inbreeding)
History alone lets the bot only re-suggest known places — exploration has nothing new
to explore. Inject untried candidates so it can grow:
- **Google Places nearby search** around office coordinates → real nearby places with
  `times_picked = 0`, surfaced by the exploration term, or
- a **Claude call** to propose ~15 spots near the office address.

History gives the warm start; a discovery source gives room to grow.

---

## 10. Slack integration details

### App setup (one-time)
1. Create app at api.slack.com/apps (from scratch).
2. Bot scopes: `chat:write`, `commands`, `channels:history` (`groups:history` if
   private), `users:read`, `reactions:read` (if counting emoji reactions).
3. Enable **Socket Mode** → yields an app-level token `xapp-…`.
4. Install to workspace → yields a bot token `xoxb-…`.
5. `/invite @lunchbot` into the channel.

Two tokens: `xoxb-` is the bot's identity for Web API calls; `xapp-` is the websocket.

### Posting
- `chat_postMessage` with Block Kit `section` blocks; each candidate card has a `Vote`
  button whose `action_id = "vote"` and `value = "{slot}:{place_id}"`.
- Always include a plain-text `text` fallback for notifications.

### Receiving
- `@app.action("vote")` and `@app.action("rate")` handlers.
- **Hard rule:** `ack()` within 3 seconds, then do slow work (DB write, `chat_update`,
  any Claude call) after the ack. Missing the ack shows the user an error.
- Vote handler upserts into `votes` (latest per user per poll), then `chat_update`s the
  original message with live tallies.

### Slash command (optional)
`/lunch` → ad-hoc poll; `/lunch prefs` → constraints modal; `/lunch add <name>` →
admin add a place.

---

## 11. Claude usage

| Use                     | When            | Notes                                              |
|-------------------------|-----------------|----------------------------------------------------|
| History extraction      | backfill, once  | Batched, JSON-only output, `ts` preserved.         |
| Pitch blurbs            | each poll       | One line per candidate; cache the system prompt.   |
| Weekly pattern digest   | weekly job      | Dump visits+ratings, ask for patterns + blind spots.|
| Name de-duplication     | backfill        | Optional fallback when fuzzy match is ambiguous.   |
| NL requests             | v2/stretch      | "somewhere cheap and fast" → adjusted score weights.|

Claude's job is extraction, pattern-spotting, and copy — **not** the recommender core.
The ranking stays a transparent, debuggable formula. Use prompt caching on the system
prompt for the per-poll and extraction calls to keep cost down.

---

## 12. Edge cases and rules

- **Ties:** break by the recommender's own `score` (deterministic and nudges toward the
  spot the engine already favored). Document this so it's not surprising.
- **Zero votes at close:** auto-pick the #1-scored candidate and log it, so the day
  still produces a visit (otherwise the learning record has gaps). Announce it as the
  default pick.
- **No present roster / no RSVPs:** fall back to scoring against all known users.
- **Holidays / nobody in:** v2 — a skip calendar; for v1, anyone can `/lunch skip`.
- **Duplicate candidate:** selection must de-dup so a place can't appear twice in one poll.
- **Late vote after close:** rejected by the `closed` flag; buttons are already gone.
- **Place permanently closed:** `is_active = 0`; never suggested, history preserved.
- **Process restart mid-poll:** on boot, re-register `close_poll` jobs for any poll
  with `closed = 0` whose 10-minute window hasn't elapsed; immediately close any whose
  window already passed.
- **Rating without a meal (poll auto-skipped):** no rating prompt is posted.

---

## 13. Feature roadmap

### MVP (build first — the smallest loop that demonstrably learns)
- Hard-constraint filter + Bayesian score + exploration + recency.
- 3 picks with Claude pitch blurbs.
- Vote buttons with live tally; one vote per person, changeable.
- 10-minute poll close with winner announcement and visit logging.
- 1-tap 👍/👎 rating post-meal.
- RSVP / "I'm out today".
- Cold-start backfill from channel history + seed list of untried spots.
- Admin: add/remove/close a place; manual override; cold-start seed.

### v2 (highest-leverage adds)
- **Regret tracking** (downweight places that win votes but earn bad ratings).
- **Weekly Claude digest** (patterns + blind spots).
- Weekday/time-of-day pattern bias.
- Dietary badges + metadata (price, distance, menu link) on cards.
- Veto button; reroll / "surprise me".
- Explicit prefs modal (diet, spice, budget, favorites, blocklist).
- Holiday/skip calendar.

### Stretch
- Weather-aware biasing; rating decay over time; quality-decline alerts.
- Natural-language requests parsed by Claude into score weights.
- Review summarization (Google/Yelp consensus into the pitch).
- Participation streaks; monthly "top spot" award.

---

## 14. Build plan / milestones

1. **M1 — Skeleton:** Bolt Socket Mode process boots, connects, responds to `/lunch`
   with a hardcoded message. SQLite schema created. Env/config wired.
2. **M2 — Poll loop:** `post_picks` (hardcoded 3 places) → vote buttons → live tally →
   `close_poll` at 10 min → winner + visit log. Restart recovery for open polls.
3. **M3 — Ratings:** post-meal 👍/👎 prompt, `ratings` writes.
4. **M4 — Engine:** implement filtering + scoring + selection; wire `post_picks` to it.
5. **M5 — Backfill:** pull → extract (Claude) → dedup → seed priors; seed list top-up.
6. **M6 — Polish:** pitch blurbs, edge cases (ties, zero votes), admin commands.
7. **M7 — v2:** regret tracking + weekly digest.

M1–M4 is a runnable, self-learning bot — realistically a weekend.

---

## 15. Configuration

| Env var             | Purpose                                  |
|---------------------|------------------------------------------|
| `SLACK_BOT_TOKEN`   | `xoxb-…` Web API token                   |
| `SLACK_APP_TOKEN`   | `xapp-…` Socket Mode token               |
| `LUNCH_CHANNEL_ID`  | target channel id (e.g. `C0XXXXXXX`)     |
| `ANTHROPIC_API_KEY` | Claude API (or Bedrock creds)            |
| `TZ`                | scheduler timezone                       |
| `GOOGLE_PLACES_KEY` | optional, for nearby discovery           |
| `OFFICE_LATLNG`     | optional, for nearby discovery           |
| `DB_PATH`           | SQLite file path                         |
| Tunables            | `m`, `C`, `K`, recency half-life, slot times |

---

## 16. Testing

- **Unit:** scoring math — Bayesian shrinkage, exploration decay, recency curve,
  group_fit with mixed rosters; tie-break determinism; zero-vote default.
- **Unit:** extraction prompt against a fixture of messy real messages → expected JSON.
- **Integration:** simulate a full poll lifecycle against a Slack test workspace
  (post → vote → change vote → close → rate); assert DB state and rendered blocks.
- **Property:** a place with consistent 👍 must monotonically rise in rank over visits;
  exploration bonus must strictly decrease with `times_picked`.
- **Recovery:** kill the process mid-poll, restart, assert the poll still closes once.

---

## 17. Risks and open questions

- **Participation is the real bottleneck, not the algorithm.** If rating costs more
  than one tap, nobody rates and nothing learns. Keep it to one tap in-thread.
- **Sparse data early.** First weeks lean heavily on exploration; that's by design, but
  set expectations that quality ramps over ~4 weeks.
- **Extraction accuracy.** Claude will miss/misclassify some history; backfill is a
  warm start, not ground truth. Allow manual correction of seeded places.
- **Open:** keep both lunch and dinner, or lunch only to start?
- **Open:** poll close by time only, or also "first to N votes"?
- **Open:** should a single strong veto override a vote majority (v2 veto button)?
- **Open:** rating prompt timing per slot (e.g. lunch at 13:30, dinner next morning)?

---

## 18. Appendix — scoring reference

```
candidates = [p for p in active_places
              if not violates_hard_constraint(p, present_roster)]

for p in candidates:
    rq  = bayesian(p.sum_ratings, p.num_ratings, m=8, C=global_like_rate)
    gf  = mean(user_pref(u, p.cuisine) for u in present_roster)
    rec = 1 - exp(-days_since(p.last_visit) / 7)
    exp = K / sqrt(p.times_picked + 1)
    p.score = rq * gf * rec + exp

ranked   = sort(candidates, by=score, desc=True)
top_two  = ranked[:2]
explorer = max(untried_or_low_sample(candidates), by=explore_term)
picks    = dedup(top_two + [explorer])   # exactly 3
```