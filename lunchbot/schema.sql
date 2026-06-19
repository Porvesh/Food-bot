-- Lunch Bot schema. The database is the entire memory of the system:
-- there are no model weights anywhere. Derived facts (per-user cuisine
-- preference, global like-rate) are computed on the fly from these tables.

-- Restaurants and their rolled-up stats (the core "what we know" table).
CREATE TABLE IF NOT EXISTS places (
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

-- People (lazily created on first interaction).
CREATE TABLE IF NOT EXISTS users (
    slack_id   TEXT PRIMARY KEY,
    display    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Hard dietary constraints -- a FILTER, never a learned weight.
CREATE TABLE IF NOT EXISTS constraints (
    slack_id TEXT,
    kind     TEXT,    -- vegetarian | vegan | halal | gluten_free | allergy:peanut | ...
    PRIMARY KEY (slack_id, kind)
);

-- One row per poll.
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY,
    slot       TEXT,            -- lunch | dinner
    date       TEXT,            -- ISO date
    ts         TEXT,            -- Slack message timestamp (also the update handle)
    candidates TEXT,            -- JSON array of place ids offered
    winner_id  INTEGER,         -- set at close
    closed     INTEGER DEFAULT 0,
    close_at   TEXT,            -- ISO datetime the poll should close (restart recovery)
    rated      INTEGER DEFAULT 0,  -- 1 once the rating prompt has been posted
    created_at TEXT DEFAULT (datetime('now'))
);

-- One row per vote (latest wins per user per poll via upsert).
CREATE TABLE IF NOT EXISTS votes (
    poll_id  INTEGER,
    slack_id TEXT,
    place_id INTEGER,
    voted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (poll_id, slack_id)
);

-- One row per rating (post-meal thumbs).
CREATE TABLE IF NOT EXISTS ratings (
    poll_id  INTEGER,
    slack_id TEXT,
    place_id INTEGER,
    value    REAL,             -- 1.0 = up, 0.0 = down (room for 1..5 later)
    rated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (poll_id, slack_id)
);

-- Per-user RSVP / attendance for a given slot+date (who to optimize for).
CREATE TABLE IF NOT EXISTS attendance (
    slack_id TEXT,
    date     TEXT,
    slot     TEXT,
    present  INTEGER,           -- 1 = in, 0 = out
    PRIMARY KEY (slack_id, date, slot)
);

CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes (poll_id);
CREATE INDEX IF NOT EXISTS idx_ratings_place ON ratings (place_id);
CREATE INDEX IF NOT EXISTS idx_polls_open ON polls (closed);
