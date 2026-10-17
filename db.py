"""SQLite storage for the video library. One file: data/library.db.

Tables
  channels     one row per channel
  videos       one row per video (stats overwritten on refresh)
  comments     every top-level comment and reply
  scores       Claude's score per video (written via `ingest.py import-scores`)
  user_status  the viewer's choice: watched / interested / not_interested
  quota_log    API units spent per Pacific-time day
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "data" / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id       TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    handle           TEXT,
    url              TEXT NOT NULL,
    thumbnail_url    TEXT,
    uploads_playlist TEXT,
    last_fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    video_id             TEXT PRIMARY KEY,
    channel_id           TEXT NOT NULL REFERENCES channels(channel_id),
    title                TEXT NOT NULL,
    published_at         TEXT NOT NULL,
    duration_seconds     INTEGER,
    view_count           INTEGER,
    like_count           INTEGER,
    comment_count        INTEGER,
    thumbnail_url        TEXT,
    stats_updated_at     TEXT,
    comments_fetched_at  TEXT,
    comments_status      TEXT,
    duplicate_of         TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    comment_id   TEXT PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(video_id),
    parent_id    TEXT,
    author       TEXT,
    text         TEXT,
    like_count   INTEGER,
    published_at TEXT,
    reply_count  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
CREATE TABLE IF NOT EXISTS scores (
    video_id          TEXT PRIMARY KEY REFERENCES videos(video_id),
    score             REAL NOT NULL,
    summary           TEXT,
    reasons           TEXT,
    comments_seen     INTEGER,
    tie_rank          INTEGER,
    series            TEXT,
    series_order      INTEGER,
    scored_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_status (
    video_id   TEXT PRIMARY KEY REFERENCES videos(video_id),
    status     TEXT NOT NULL CHECK (status IN ('watched', 'interested', 'not_interested')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_log (
    day_pt TEXT PRIMARY KEY,
    units  INTEGER NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_pt() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


MIGRATIONS = {
    "videos": {"duplicate_of": "TEXT"},
    "channels": {"source_url": "TEXT"},
    "scores": {"tie_rank": "INTEGER", "series": "TEXT", "series_order": "INTEGER"},
}


def _migrate(con) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def upsert_channel(con, row: dict) -> None:
    con.execute(
        """INSERT INTO channels (channel_id, title, handle, url, source_url, thumbnail_url, uploads_playlist, last_fetched_at)
           VALUES (:channel_id, :title, :handle, :url, :source_url, :thumbnail_url, :uploads_playlist, :last_fetched_at)
           ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, handle=excluded.handle,
             url=excluded.url, source_url=COALESCE(excluded.source_url, source_url), thumbnail_url=excluded.thumbnail_url,
             uploads_playlist=excluded.uploads_playlist, last_fetched_at=excluded.last_fetched_at""",
        {"source_url": None, **row},
    )


def upsert_videos(con, rows: list[dict]) -> None:
    ts = now_iso()
    con.executemany(
        """INSERT INTO videos (video_id, channel_id, title, published_at, duration_seconds,
             view_count, like_count, comment_count, thumbnail_url, stats_updated_at)
           VALUES (:video_id, :channel_id, :title, :published_at, :duration_seconds,
             :view_count, :like_count, :comment_count, :thumbnail_url, :ts)
           ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
             duration_seconds=excluded.duration_seconds, view_count=excluded.view_count,
             like_count=excluded.like_count, comment_count=excluded.comment_count,
             thumbnail_url=excluded.thumbnail_url, stats_updated_at=excluded.stats_updated_at""",
        [{**r, "ts": ts} for r in rows],
    )


def replace_comments(con, video_id: str, rows: list[dict], status: str = "ok") -> None:
    con.execute("DELETE FROM comments WHERE video_id = ?", (video_id,))
    con.executemany(
        """INSERT OR REPLACE INTO comments (comment_id, video_id, parent_id, author, text,
             like_count, published_at, reply_count)
           VALUES (:comment_id, :video_id, :parent_id, :author, :text,
             :like_count, :published_at, :reply_count)""",
        rows,
    )
    con.execute(
        "UPDATE videos SET comments_fetched_at = ?, comments_status = ? WHERE video_id = ?",
        (now_iso(), status, video_id),
    )


def set_status(con, video_id: str, status: str | None) -> None:
    if status is None:
        con.execute("DELETE FROM user_status WHERE video_id = ?", (video_id,))
    else:
        con.execute(
            """INSERT INTO user_status (video_id, status, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
            (video_id, status, now_iso()),
        )
    con.commit()


def upsert_score(con, video_id: str, score: float, summary: str, reasons: str, comments_seen: int,
                 tie_rank: int | None = None, series: str | None = None, series_order: int | None = None) -> None:
    con.execute(
        """INSERT INTO scores (video_id, score, summary, reasons, comments_seen, tie_rank, series,
             series_order, scored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(video_id) DO UPDATE SET score=excluded.score, summary=excluded.summary,
             reasons=excluded.reasons, comments_seen=excluded.comments_seen, tie_rank=excluded.tie_rank,
             series=excluded.series, series_order=excluded.series_order, scored_at=excluded.scored_at""",
        (video_id, score, summary, reasons, comments_seen, tie_rank, series, series_order, now_iso()),
    )


def set_tie_ranks(con, ranks: dict[str, int]) -> None:
    for vid, rank in ranks.items():
        if con.execute("UPDATE scores SET tie_rank = ? WHERE video_id = ?", (rank, vid)).rowcount == 0:
            raise ValueError(f"{vid} has no score yet")


def set_series(con, series: str | None, video_ids: list[str]) -> None:
    """Group versions/sequels of one story; order = position in video_ids (1-based)."""
    for i, vid in enumerate(video_ids, 1):
        cur = con.execute("UPDATE scores SET series = ?, series_order = ? WHERE video_id = ?",
                          (series, i if series else None, vid))
        if cur.rowcount == 0:
            raise ValueError(f"{vid} has no score yet — score it before grouping")


def add_quota(con, units: int) -> None:
    if units:
        con.execute(
            """INSERT INTO quota_log (day_pt, units) VALUES (?, ?)
               ON CONFLICT(day_pt) DO UPDATE SET units = units + excluded.units""",
            (today_pt(), units),
        )


def quota_today(con) -> int:
    row = con.execute("SELECT units FROM quota_log WHERE day_pt = ?", (today_pt(),)).fetchone()
    return row["units"] if row else 0


LIBRARY_SQL = """
SELECT v.video_id, v.title, v.published_at, v.duration_seconds, v.view_count, v.like_count,
       v.comment_count, v.thumbnail_url, v.stats_updated_at, v.comments_fetched_at,
       c.channel_id, c.title AS channel_title, c.url AS channel_url,
       s.score, s.summary, s.reasons, s.scored_at, s.tie_rank, s.series, s.series_order,
       u.status
FROM videos v
JOIN channels c USING (channel_id)
LEFT JOIN scores s USING (video_id)
LEFT JOIN user_status u USING (video_id)
WHERE v.duplicate_of IS NULL
"""

# Selection rule for what we fetch comments for, score and show (agreed with the user 2026-10-13).
# Views requirement ramps up with the video's age: required = 50K * sqrt(min(1, age_days / 60)).
# Long videos in this library reach their typical view level after ~2 months (cross-section of 92
# videos >10h on 2026-10-13: median ~14K at <=3 days, ~40K at 2-6 weeks, ~76-87K at 2-4 months),
# so young videos with a good pace are kept as "potential" instead of being cut by a flat 50K.
MIN_DURATION_SECONDS = 10 * 3600
MIN_VIEWS = 50_000
RAMP_DAYS = 60
_AGE_DAYS = "MAX(0.5, julianday('now') - julianday(v.published_at))"
# views > 50K*sqrt(r)  <=>  views^2 > 50K^2 * r   (SQLite has no sqrt by default)
ELIGIBLE_SQL = (f"v.duplicate_of IS NULL AND v.duration_seconds > {MIN_DURATION_SECONDS} "
                f"AND 1.0 * COALESCE(v.view_count, 0) * COALESCE(v.view_count, 0) > "
                f"{MIN_VIEWS}.0 * {MIN_VIEWS} * MIN(1.0, {_AGE_DAYS} / {RAMP_DAYS})")


def required_views(age_days: float) -> float:
    return MIN_VIEWS * min(1.0, max(0.5, age_days) / RAMP_DAYS) ** 0.5
