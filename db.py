"""SQLite storage for the video library. One file: data/library.db.

Tables
  channels     one row per channel
  videos       one row per video (stats overwritten on refresh)
  comments     every top-level comment and reply
  scores       Claude's score per video (written via `ingest.py import-scores`)
  user_status  the viewer's choice: watched / interested / not_interested
  quota_log    API units spent per Pacific-time day
  sets         one story uploaded in several parts by ONE channel (ชุดตอน) — scored as one unit
  set_parts    the clips of a set, in story order; counted = 0 when another part already covers it
"""
from __future__ import annotations

import re
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
CREATE TABLE IF NOT EXISTS sets (
    set_id     TEXT PRIMARY KEY REFERENCES videos(video_id),  -- = the lead part (normally part 1); its
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),  -- scores row is the score of the whole set
    title      TEXT NOT NULL,
    note       TEXT,                                           -- e.g. "ช่องไม่ได้ลงตอนที่ 4"
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS set_parts (
    video_id     TEXT PRIMARY KEY REFERENCES videos(video_id),  -- a clip is in at most one set
    set_id       TEXT NOT NULL REFERENCES sets(set_id) ON DELETE CASCADE,
    part_order   INTEGER NOT NULL,
    label        TEXT,                                          -- "SS 1-2", "ตอนที่ 7"
    counted      INTEGER NOT NULL DEFAULT 1,
    overlap_note TEXT                                           -- why counted = 0
);
CREATE INDEX IF NOT EXISTS idx_set_parts_set ON set_parts(set_id);
-- "does the story really end in this clip?" — judged once per clip from a 1-unit comment sample
-- (user asked 2026-11-12: a clip of 4 h+ whose story is truly finished joins the library even under
-- 10 h or the view bar). done = enters the library · open/unsure = no · nosignal = nobody talks about
-- an ending (decided by code, no reading needed)
CREATE TABLE IF NOT EXISTS finish_checks (
    video_id    TEXT PRIMARY KEY REFERENCES videos(video_id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('done', 'open', 'unsure', 'nosignal')),
    note        TEXT,
    n_comments  INTEGER,
    checked_at  TEXT NOT NULL
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
    # manual_duplicate_of: a re-upload Claude confirmed by reading it (survives mark_duplicates)
    # first_seen_at: when this library first saw the clip (set once, used by the run report)
    "videos": {"duplicate_of": "TEXT", "manual_duplicate_of": "TEXT", "first_seen_at": "TEXT",
               # description: from the same videos.list call (no extra units) — helps link parts of a set
               # gone_at: a full refresh no longer got the clip back (deleted / made private) -> hidden
               "description": "TEXT", "gone_at": "TEXT"},
    # origin: NULL/'manual' = added by hand with `ingest.py channel`, 'subscription' = from the
    # viewing account's subscriptions · subscribed: 1 while that account still follows it
    # language: audio language of the channel's clips — NULL = guess from titles (see channel_languages),
    # 'th' = Thai dub (main page), anything else ('zh', 'en', ...) = the separate "other languages" tab
    "channels": {"source_url": "TEXT", "origin": "TEXT", "subscribed": "INTEGER", "language": "TEXT",
                 # full_scan_at: last walk of the WHOLE uploads playlist (the normal run stops at the first
                 # known page, so clips that show up late in the middle of the list would be missed)
                 "full_scan_at": "TEXT"},
    # finished: 1 = comments make it clear the story is complete ("จบแล้ว" tag) — never part of the score
    "scores": {"tie_rank": "INTEGER", "series": "TEXT", "series_order": "INTEGER",
               "finished": "INTEGER", "finished_note": "TEXT"},
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
             view_count, like_count, comment_count, thumbnail_url, stats_updated_at, first_seen_at, description)
           VALUES (:video_id, :channel_id, :title, :published_at, :duration_seconds,
             :view_count, :like_count, :comment_count, :thumbnail_url, :ts, :ts, :description)
           ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
             duration_seconds=excluded.duration_seconds, view_count=excluded.view_count,
             like_count=excluded.like_count, comment_count=excluded.comment_count,
             thumbnail_url=excluded.thumbnail_url, stats_updated_at=excluded.stats_updated_at,
             description=COALESCE(excluded.description, description), gone_at=NULL""",
        [{"description": None, **r, "ts": ts} for r in rows],
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


# Channels the update run pulls new clips from: everything added by hand plus whatever the
# viewing account currently subscribes to (a channel that was unsubscribed keeps its clips/scores).
TRACKED_CHANNELS_SQL = "(c.origin IS NULL OR c.origin = 'manual' OR c.subscribed = 1)"

DAILY_QUOTA = 10_000


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


def quota_left(con) -> int:
    return DAILY_QUOTA - quota_today(con)


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
MIN_VIEWS = 40_000  # was 50_000 until 2026-10-17 (user asked for a slight easing: +11 clips)
RAMP_DAYS = 60
SET_MAX_GAP_DAYS = 120  # parts of one set: each part uploaded at most ~4 months after the previous one
# Finished stories (user 2026-11-12): 4 h+ and judged "done" from comments -> in the library whatever the
# length or views. Only clips old enough to have comments are judged; unclear verdicts are re-judged later.
MIN_FINISHED_SECONDS = 4 * 3600
FINISH_MIN_AGE_DAYS = 14
FINISH_RECHECK_DAYS = 90


def _views_ok(alias: str) -> str:
    """views > MIN_VIEWS*sqrt(r)  <=>  views^2 > MIN_VIEWS^2 * r   (SQLite has no sqrt by default)"""
    age = f"MAX(0.5, julianday('now') - julianday({alias}.published_at))"
    return (f"1.0 * COALESCE({alias}.view_count, 0) * COALESCE({alias}.view_count, 0) > "
            f"{MIN_VIEWS}.0 * {MIN_VIEWS} * MIN(1.0, {age} / {RAMP_DAYS})")


# A set (one channel's story in several parts, agreed with the user 2026-11-12) is eligible when the
# parts that count (overlaps excluded) add up to more than 10 h and its most-watched part passes the
# view bar for that part's age — later parts always have fewer views than part 1.
ELIGIBLE_SETS_SQL = (f"SELECT p.set_id FROM set_parts p JOIN videos pv ON pv.video_id = p.video_id "
                     f"WHERE pv.duplicate_of IS NULL AND pv.gone_at IS NULL GROUP BY p.set_id "
                     f"HAVING SUM(CASE WHEN p.counted = 1 THEN pv.duration_seconds ELSE 0 END) > {MIN_DURATION_SECONDS} "
                     f"AND MAX(CASE WHEN {_views_ok('pv')} THEN 1 ELSE 0 END) = 1")
# One "unit" of the library = a single clip, or a set represented by its lead clip (set_id).
# Clips that belong to a set are never units on their own.
SINGLE_ELIGIBLE_SQL = (f"v.duplicate_of IS NULL AND v.gone_at IS NULL AND v.duration_seconds > {MIN_DURATION_SECONDS} "
                       f"AND {_views_ok('v')} AND v.video_id NOT IN (SELECT video_id FROM set_parts)")
FINISHED_SHORT_SQL = (f"v.duplicate_of IS NULL AND v.gone_at IS NULL AND v.duration_seconds >= {MIN_FINISHED_SECONDS} "
                      f"AND v.video_id NOT IN (SELECT video_id FROM set_parts) "
                      f"AND v.video_id IN (SELECT video_id FROM finish_checks WHERE verdict = 'done')")
ELIGIBLE_SQL = f"(({SINGLE_ELIGIBLE_SQL}) OR ({FINISHED_SHORT_SQL}) OR v.video_id IN ({ELIGIBLE_SETS_SQL}))"
# Every clip shown on the site / that needs comments: eligible units + all live parts of eligible sets
IN_LIBRARY_SQL = (f"({ELIGIBLE_SQL} OR (v.duplicate_of IS NULL AND v.gone_at IS NULL AND v.video_id IN "
                  f"(SELECT video_id FROM set_parts WHERE set_id IN ({ELIGIBLE_SETS_SQL}))))")


def required_views(age_days: float) -> float:
    return MIN_VIEWS * min(1.0, max(0.5, age_days) / RAMP_DAYS) ** 0.5


# Clips not dubbed in Thai live in their own tab on the site (user asked 2026-11-02: hidden from the
# main page until the "other languages" tab is opened; the user dubs them into Thai themself).
LANGUAGE_LABELS = {"th": "พากย์ไทย", "zh": "เสียงจีน", "en": "เสียงอังกฤษ", "other": "ภาษาอื่น"}
_THAI = re.compile(r"[฀-๿]")
_CJK = re.compile(r"[一-鿿]")


def detect_language(titles: list[str]) -> str:
    """'th' when at least half of a channel's titles contain Thai script, else 'zh' / 'other'."""
    if not titles or sum(bool(_THAI.search(t)) for t in titles) * 2 >= len(titles):
        return "th"
    return "zh" if sum(bool(_CJK.search(t)) for t in titles) * 2 >= len(titles) else "other"


def channel_languages(con) -> dict[str, str]:
    """channel_id -> language code; channels.language overrides the guess from titles."""
    titles: dict[str, list[str]] = {}
    for r in con.execute("SELECT channel_id, title FROM videos"):
        titles.setdefault(r[0], []).append(r[1])
    return {r[0]: r[1] or detect_language(titles.get(r[0], []))
            for r in con.execute("SELECT channel_id, language FROM channels")}


# ---------------------------------------------------------------- sets (ชุดตอน)

def save_set(con, title: str, parts: list[dict], note: str | None = None, set_id: str | None = None) -> str:
    """parts: [{"id", "label"?, "counted"? (default True), "overlap"?}] in story order.
    Replaces whatever sets these clips were in. All parts must come from one channel and each part
    must be uploaded within SET_MAX_GAP_DAYS of the previous one (by upload date)."""
    ids = [p["id"] for p in parts]
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError(f"ชุด {title}: ต้องมีอย่างน้อย 2 คลิปและห้ามซ้ำ")
    rows = {r["video_id"]: r for r in con.execute(
        f"SELECT video_id, channel_id, published_at FROM videos WHERE video_id IN ({','.join('?' * len(ids))})", ids)}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise ValueError(f"ชุด {title}: ไม่พบคลิป {missing}")
    chans = {rows[i]["channel_id"] for i in ids}
    if len(chans) != 1:
        raise ValueError(f"ชุด {title}: คลิปมาจากหลายช่อง — ชุดตอนต้องมาจากช่องเดียวกัน (ต่างช่องให้ใช้ series)")
    dates = sorted(datetime.fromisoformat(rows[i]["published_at"].replace("Z", "+00:00")) for i in ids)
    gap = max((b - a).days for a, b in zip(dates, dates[1:]))
    if gap > SET_MAX_GAP_DAYS:
        raise ValueError(f"ชุด {title}: มีตอนที่ลงห่างจากตอนก่อนหน้า {gap} วัน (เกิน {SET_MAX_GAP_DAYS})")
    if not any(p.get("counted", True) for p in parts):
        raise ValueError(f"ชุด {title}: ต้องมีอย่างน้อย 1 ตอนที่นับเวลา")
    set_id = set_id or ids[0]
    if set_id not in ids:
        raise ValueError(f"ชุด {title}: set_id ต้องเป็นหนึ่งในคลิปของชุด")
    old = {r[0] for r in con.execute(f"SELECT DISTINCT set_id FROM set_parts WHERE video_id IN ({','.join('?' * len(ids))})", ids)}
    con.execute(f"DELETE FROM set_parts WHERE video_id IN ({','.join('?' * len(ids))})", ids)
    con.execute("""INSERT INTO sets (set_id, channel_id, title, note, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(set_id) DO UPDATE SET title=excluded.title, note=excluded.note,
                     updated_at=excluded.updated_at""", (set_id, chans.pop(), title, note, now_iso()))
    con.executemany("""INSERT INTO set_parts (video_id, set_id, part_order, label, counted, overlap_note)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [(p["id"], set_id, i, p.get("label"), 1 if p.get("counted", True) else 0, p.get("overlap"))
                     for i, p in enumerate(parts, 1)])
    for sid in old - {set_id}:  # a set that lost all its parts to this one disappears
        if not con.execute("SELECT 1 FROM set_parts WHERE set_id = ?", (sid,)).fetchone():
            con.execute("DELETE FROM sets WHERE set_id = ?", (sid,))
    return set_id


def remove_set(con, set_id: str) -> None:
    con.execute("DELETE FROM set_parts WHERE set_id = ?", (set_id,))
    con.execute("DELETE FROM sets WHERE set_id = ?", (set_id,))


def set_parts(con, set_id: str) -> list:
    return con.execute("""SELECT p.*, v.title, v.duration_seconds, v.view_count, v.like_count, v.comment_count,
                                 v.published_at, v.thumbnail_url, v.gone_at, v.duplicate_of
                          FROM set_parts p JOIN videos v USING (video_id)
                          WHERE p.set_id = ? ORDER BY p.part_order""", (set_id,)).fetchall()


def set_finished(con, video_id: str, finished: bool | None, note: str | None = None) -> None:
    """The "จบแล้ว" tag of a scored unit — display/filter only, never part of the score."""
    cur = con.execute("UPDATE scores SET finished = ?, finished_note = ? WHERE video_id = ?",
                      (None if finished is None else int(finished), note, video_id))
    if cur.rowcount == 0:
        raise ValueError(f"{video_id} has no score yet")


# ---------------------------------------------------------------- finish checks (เรื่องจบจริง)

def finish_candidates(con) -> list[sqlite3.Row]:
    """Thai-dubbed clips of 4 h+ that are not in the library, old enough to have comments, and never judged
    (or judged nosignal/unsure more than FINISH_RECHECK_DAYS ago). Parts of a set are left to the set rules."""
    langs = channel_languages(con)
    rows = con.execute(f"""
        SELECT v.video_id, v.channel_id, v.title, v.duration_seconds, v.view_count FROM videos v
        LEFT JOIN finish_checks f USING (video_id)
        WHERE v.duplicate_of IS NULL AND v.gone_at IS NULL AND v.duration_seconds >= {MIN_FINISHED_SECONDS}
          AND v.video_id NOT IN (SELECT video_id FROM set_parts) AND NOT ({ELIGIBLE_SQL})
          AND julianday('now') - julianday(v.published_at) >= {FINISH_MIN_AGE_DAYS}
          AND (f.video_id IS NULL OR (f.verdict IN ('nosignal', 'unsure')
               AND julianday('now') - julianday(f.checked_at) >= {FINISH_RECHECK_DAYS}))
        ORDER BY v.view_count DESC""").fetchall()
    return [r for r in rows if langs.get(r["channel_id"], "th") == "th"]


def save_finish_check(con, video_id: str, verdict: str, note: str | None = None, n_comments: int | None = None) -> None:
    con.execute("""INSERT INTO finish_checks (video_id, verdict, note, n_comments, checked_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET verdict=excluded.verdict, note=excluded.note,
                     n_comments=COALESCE(excluded.n_comments, n_comments), checked_at=excluded.checked_at""",
                (video_id, verdict, note, n_comments, now_iso()))


# Library clips whose comments still have to be fetched in full. A finish-check sample (100 threads,
# comments_status = 'sample') is enough to score a finished story; any other clip that later enters the
# library through the 10 h route gets its full comments.
NEEDS_COMMENTS_SQL = (f"{IN_LIBRARY_SQL} AND (v.comments_fetched_at IS NULL OR (v.comments_status = 'sample' "
                      f"AND v.video_id NOT IN (SELECT video_id FROM finish_checks WHERE verdict = 'done')))")
