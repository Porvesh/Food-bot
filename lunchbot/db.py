"""Thin SQLite data-access layer.

One connection per process (the bot is single-threaded around APScheduler +
Bolt's socket handler). Rows come back as sqlite3.Row so callers can use
dict-style access. All write helpers commit immediately -- volumes are tiny.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date as date_cls
from importlib import resources
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_key(name: str) -> str:
    """Lowercase + strip punctuation so 'Chipotle!' and 'chipotle' collapse."""
    return _NON_ALNUM.sub(" ", name.lower()).strip()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        schema = resources.files("lunchbot").joinpath("schema.sql").read_text()
        self.conn.executescript(schema)
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns to an already-created DB. CREATE TABLE IF NOT EXISTS won't
        alter an existing table, so new columns are added idempotently here."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(places)")}
        if "manual_score" not in cols:
            self.conn.execute("ALTER TABLE places ADD COLUMN manual_score REAL")
        if "meal" not in cols:
            self.conn.execute("ALTER TABLE places ADD COLUMN meal TEXT DEFAULT 'both'")

        # Multi-vote: rebuild the votes table if it still has the old one-vote-per
        # -user primary key (poll_id, slack_id) instead of (..., place_id).
        vote_pk = {r["name"] for r in self.conn.execute("PRAGMA table_info(votes)") if r["pk"]}
        if vote_pk and vote_pk != {"poll_id", "slack_id", "place_id"}:
            self.conn.executescript(
                """
                ALTER TABLE votes RENAME TO votes_old;
                CREATE TABLE votes (
                    poll_id  INTEGER,
                    slack_id TEXT,
                    place_id INTEGER,
                    voted_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (poll_id, slack_id, place_id)
                );
                INSERT OR IGNORE INTO votes (poll_id, slack_id, place_id, voted_at)
                    SELECT poll_id, slack_id, place_id, voted_at FROM votes_old;
                DROP TABLE votes_old;
                CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes (poll_id);
                """
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def backup(self, retain: int = 14) -> Optional[str]:
        """Write a consistent snapshot using SQLite's online-backup API (safe to
        run while the bot is live). Snapshots land next to the DB as
        <name>.YYYY-MM-DD.bak; older ones beyond `retain` are pruned. Returns the
        snapshot path, or None for an in-memory DB."""
        if self.path == ":memory:":
            return None
        base = os.path.basename(self.path)
        backup_dir = os.path.join(os.path.dirname(self.path) or ".", "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = date_cls.today().isoformat()
        dest_path = os.path.join(backup_dir, f"{base}.{stamp}.bak")
        with sqlite3.connect(dest_path) as dest:
            self.conn.backup(dest)
        self._prune_backups(backup_dir, base, retain)
        return dest_path

    @staticmethod
    def _prune_backups(backup_dir: str, base: str, retain: int) -> None:
        snaps = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith(f"{base}.") and f.endswith(".bak")
        )
        for stale in snaps[:-retain] if retain > 0 else []:
            try:
                os.remove(os.path.join(backup_dir, stale))
            except OSError:
                pass

    # -- places ---------------------------------------------------------------

    def active_places(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM places WHERE is_active = 1"
        ).fetchall()

    def get_place(self, place_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM places WHERE id = ?", (place_id,)
        ).fetchone()

    def upsert_place(
        self,
        name: str,
        cuisine: Optional[str] = None,
        price_band: Optional[int] = None,
        source: str = "manual",
    ) -> int:
        """Insert a place or return the id of an existing one (by canonical key)."""
        key = canonical_key(name)
        existing = self.conn.execute(
            "SELECT id FROM places WHERE canonical_key = ?", (key,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = self.conn.execute(
            """INSERT INTO places (name, canonical_key, cuisine, price_band, source)
               VALUES (?, ?, ?, ?, ?)""",
            (name, key, cuisine, price_band, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_place_active(self, place_id: int, active: bool) -> None:
        self.conn.execute(
            "UPDATE places SET is_active = ? WHERE id = ?",
            (1 if active else 0, place_id),
        )
        self.conn.commit()

    def find_place(self, name: str) -> Optional[sqlite3.Row]:
        """Look up a place by name (canonical-key match), active or not."""
        return self.conn.execute(
            "SELECT * FROM places WHERE canonical_key = ?", (canonical_key(name),)
        ).fetchone()

    def enrich_place(
        self, place_id: int, cuisine: Optional[str] = None, price_band: Optional[int] = None
    ) -> None:
        """Fill cuisine/price_band only where they are currently NULL, so an
        auto-enrichment never overwrites a value a human set on purpose."""
        self.conn.execute(
            "UPDATE places SET cuisine = COALESCE(cuisine, ?), "
            "price_band = COALESCE(price_band, ?) WHERE id = ?",
            (cuisine, price_band, place_id),
        )
        self.conn.commit()

    def set_place_cuisine(self, place_id: int, cuisine: Optional[str]) -> None:
        """Explicit override (used by the manual-correction command)."""
        self.conn.execute(
            "UPDATE places SET cuisine = ? WHERE id = ?", (cuisine, place_id)
        )
        self.conn.commit()

    def set_manual_score(self, place_id: int, score: Optional[float]) -> None:
        self.conn.execute(
            "UPDATE places SET manual_score = ? WHERE id = ?", (score, place_id)
        )
        self.conn.commit()

    def set_place_meal(self, place_id: int, meal: str) -> None:
        self.conn.execute("UPDATE places SET meal = ? WHERE id = ?", (meal, place_id))
        self.conn.commit()

    def global_like_rate(self, fallback: float = 0.6) -> float:
        """C in the spec: mean rating across all places that have any ratings."""
        row = self.conn.execute(
            "SELECT SUM(sum_ratings) AS s, SUM(num_ratings) AS n FROM places"
        ).fetchone()
        if row and row["n"]:
            return float(row["s"]) / float(row["n"])
        return fallback

    # -- users / constraints --------------------------------------------------

    def ensure_user(self, slack_id: str, display: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO users (slack_id, display) VALUES (?, ?)
               ON CONFLICT(slack_id) DO UPDATE SET display = COALESCE(excluded.display, display)""",
            (slack_id, display),
        )
        self.conn.commit()

    def all_user_ids(self) -> list[str]:
        return [r["slack_id"] for r in self.conn.execute("SELECT slack_id FROM users")]

    def constraints_for(self, slack_ids: Iterable[str]) -> set[str]:
        ids = list(slack_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT kind FROM constraints WHERE slack_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {r["kind"] for r in rows}

    def set_constraint(self, slack_id: str, kind: str, enabled: bool) -> None:
        if enabled:
            self.conn.execute(
                "INSERT OR IGNORE INTO constraints (slack_id, kind) VALUES (?, ?)",
                (slack_id, kind),
            )
        else:
            self.conn.execute(
                "DELETE FROM constraints WHERE slack_id = ? AND kind = ?",
                (slack_id, kind),
            )
        self.conn.commit()

    # -- attendance -----------------------------------------------------------

    def set_attendance(self, slack_id: str, date: str, slot: str, present: bool) -> None:
        self.conn.execute(
            """INSERT INTO attendance (slack_id, date, slot, present) VALUES (?, ?, ?, ?)
               ON CONFLICT(slack_id, date, slot) DO UPDATE SET present = excluded.present""",
            (slack_id, date, slot, 1 if present else 0),
        )
        self.conn.commit()

    def present_roster(self, date: str, slot: str) -> list[str]:
        """Who to optimize for: users marked present, else everyone we know."""
        rows = self.conn.execute(
            "SELECT slack_id FROM attendance WHERE date = ? AND slot = ? AND present = 1",
            (date, slot),
        ).fetchall()
        present = [r["slack_id"] for r in rows]
        opted_out = {
            r["slack_id"]
            for r in self.conn.execute(
                "SELECT slack_id FROM attendance WHERE date = ? AND slot = ? AND present = 0",
                (date, slot),
            )
        }
        if present:
            return present
        # No explicit RSVPs: fall back to all known users minus anyone who opted out.
        return [u for u in self.all_user_ids() if u not in opted_out]

    # -- polls ----------------------------------------------------------------

    def create_poll(
        self, slot: str, date: str, candidates: list[int], close_at: str
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO polls (slot, date, candidates, close_at)
               VALUES (?, ?, ?, ?)""",
            (slot, date, json.dumps(candidates), close_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_poll_ts(self, poll_id: int, ts: str) -> None:
        self.conn.execute("UPDATE polls SET ts = ? WHERE id = ?", (ts, poll_id))
        self.conn.commit()

    def set_poll_candidates(self, poll_id: int, candidates: list[int]) -> None:
        self.conn.execute(
            "UPDATE polls SET candidates = ? WHERE id = ?", (json.dumps(candidates), poll_id)
        )
        self.conn.commit()

    def clear_votes(self, poll_id: int) -> None:
        self.conn.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
        self.conn.commit()

    def get_poll(self, poll_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()

    def poll_candidates(self, poll: sqlite3.Row) -> list[int]:
        return json.loads(poll["candidates"])

    def open_polls(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM polls WHERE closed = 0").fetchall()

    def polls_awaiting_rating(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM polls WHERE closed = 1 AND rated = 0 AND winner_id IS NOT NULL"
        ).fetchall()

    def mark_poll_closed(self, poll_id: int, winner_id: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE polls SET closed = 1, winner_id = ? WHERE id = ?",
            (winner_id, poll_id),
        )
        self.conn.commit()

    def mark_poll_rated(self, poll_id: int) -> None:
        self.conn.execute("UPDATE polls SET rated = 1 WHERE id = ?", (poll_id,))
        self.conn.commit()

    # -- votes ----------------------------------------------------------------

    def cast_vote(self, poll_id: int, slack_id: str, place_id: int) -> bool:
        """Toggle a user's vote for a place. Multi-select: a user may vote for
        several places. Returns True if the vote is now on, False if removed."""
        existing = self.conn.execute(
            "SELECT 1 FROM votes WHERE poll_id = ? AND slack_id = ? AND place_id = ?",
            (poll_id, slack_id, place_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                "DELETE FROM votes WHERE poll_id = ? AND slack_id = ? AND place_id = ?",
                (poll_id, slack_id, place_id),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO votes (poll_id, slack_id, place_id) VALUES (?, ?, ?)",
            (poll_id, slack_id, place_id),
        )
        self.conn.commit()
        return True

    def vote_tally(self, poll_id: int) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT place_id, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY place_id",
            (poll_id,),
        ).fetchall()
        return {int(r["place_id"]): int(r["n"]) for r in rows}

    # -- ratings --------------------------------------------------------------

    def record_rating(self, poll_id: int, slack_id: str, place_id: int, value: float) -> None:
        """Write the rating and roll it up into the place's aggregates."""
        existing = self.conn.execute(
            "SELECT value FROM ratings WHERE poll_id = ? AND slack_id = ?",
            (poll_id, slack_id),
        ).fetchone()
        with self.conn:
            if existing is not None:
                old = float(existing["value"])
                self.conn.execute(
                    "UPDATE ratings SET value = ?, rated_at = datetime('now') "
                    "WHERE poll_id = ? AND slack_id = ?",
                    (value, poll_id, slack_id),
                )
                # Adjust the rolled-up sum; count is unchanged for an update.
                self.conn.execute(
                    "UPDATE places SET sum_ratings = sum_ratings + ? WHERE id = ?",
                    (value - old, place_id),
                )
            else:
                self.conn.execute(
                    "INSERT INTO ratings (poll_id, slack_id, place_id, value) VALUES (?, ?, ?, ?)",
                    (poll_id, slack_id, place_id, value),
                )
                self.conn.execute(
                    "UPDATE places SET sum_ratings = sum_ratings + ?, num_ratings = num_ratings + 1 "
                    "WHERE id = ?",
                    (value, place_id),
                )

    def cuisine_ratings_by_user(self) -> dict[str, dict[str, tuple[float, int]]]:
        """{slack_id: {cuisine: (sum, count)}} -- used for per-user cuisine pref."""
        rows = self.conn.execute(
            """SELECT r.slack_id AS uid, p.cuisine AS cuisine,
                      SUM(r.value) AS s, COUNT(*) AS n
               FROM ratings r JOIN places p ON p.id = r.place_id
               WHERE p.cuisine IS NOT NULL
               GROUP BY r.slack_id, p.cuisine"""
        ).fetchall()
        out: dict[str, dict[str, tuple[float, int]]] = {}
        for r in rows:
            out.setdefault(r["uid"], {})[r["cuisine"]] = (float(r["s"]), int(r["n"]))
        return out

    def user_mean_rating(self) -> dict[str, float]:
        """{slack_id: personal mean rating} -- prior for per-user cuisine shrinkage."""
        rows = self.conn.execute(
            "SELECT slack_id, AVG(value) AS m FROM ratings GROUP BY slack_id"
        ).fetchall()
        return {r["slack_id"]: float(r["m"]) for r in rows}

    # -- winner bookkeeping ---------------------------------------------------

    def record_win(self, place_id: int, on_date: Optional[str] = None) -> None:
        on_date = on_date or date_cls.today().isoformat()
        self.conn.execute(
            "UPDATE places SET times_picked = times_picked + 1, last_visit = ? WHERE id = ?",
            (on_date, place_id),
        )
        self.conn.commit()


def open_database(path: str) -> Database:
    return Database(path)
