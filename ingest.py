"""Fetch YouTube data into the library and exchange scores with Claude.

  python ingest.py channel <url> [--no-comments] [--recheck-comments]
                                       all new videos of a channel; comments only for eligible ones
  python ingest.py video <url>         one video + its channel + all comments
  python ingest.py comments            fetch comments for eligible videos that have none yet
  python ingest.py refresh [--all]     update views/likes for videos marked interested/watched;
                                       --all = every non-duplicate clip (~13 units per 600 clips)
  python ingest.py import-status <file.json>   {"video_id": "watched|interested|not_interested"} -> user_status
  python ingest.py pull-status         copy the statuses saved by the website (Cloudflare Worker) into user_status
  python ingest.py stats               per-channel summary + estimated quota for missing comments
  python ingest.py export-scoring [--channel TEXT] [--limit N] [--ids a,b] [--all]
                                       compact unscored videos + deduplicated comments for Claude
  python ingest.py ranked [--score N] [--channel TEXT]
                                       compact list of scored videos (for tie-breaking)
  python ingest.py import-scores <file.json>
  python ingest.py set-ties <file.json>    {"video_id": tie_rank, ...} (1 = บนสุดในกลุ่มคะแนนเท่ากัน)
  python ingest.py set-series <file.json>   [{"series": "ชื่อเรื่อง", "videos": [id ตามลำดับ]}]
  python ingest.py set-duplicate <id> <of> [--clear]
                                       mark a re-upload Claude confirmed (kept across mark_duplicates)
  python ingest.py quota               units used today (Pacific day)

The whole update (subscriptions -> new clips -> comments -> site) is `python update.py`.
Eligible = not a duplicate, longer than 10 h, views above an age-based bar (see db.ELIGIBLE_SQL).
Quota rules: re-running a channel stops at the first already-known page; comments are fetched
once per eligible video (unless --recheck-comments finds the count grew 10%+).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError

import db
import yt_api

ROOT = Path(__file__).parent
DUPLICATE_DURATION_TOLERANCE = 3  # seconds; titles must match exactly


def load_api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if key:
        return key
    secrets = ROOT / ".streamlit" / "secrets.toml"
    if secrets.exists():
        import tomllib
        return tomllib.loads(secrets.read_text(encoding="utf-8")).get("YOUTUBE_API_KEY", "")
    return ""


def _channel_row(ch: dict, source_url: str | None = None) -> dict:
    handle = ch["snippet"].get("customUrl")
    if handle and not handle.isascii():  # YouTube drops Thai tone marks in customUrl -> link would not open
        handle = None
    return {
        "channel_id": ch["id"],
        "title": ch["snippet"]["title"],
        "handle": handle,
        "url": f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{ch['id']}",
        "source_url": source_url,
        "thumbnail_url": yt_api.best_thumb(ch["snippet"].get("thumbnails", {})),
        "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
        "last_fetched_at": db.now_iso(),
    }


def mark_duplicates(con) -> int:
    """Same exact title (whitespace-trimmed) and duration within a few seconds = same clip.
    The copy with the most views stays; the others point to it via duplicate_of."""
    con.execute("UPDATE videos SET duplicate_of = manual_duplicate_of")
    groups: dict[str, list] = {}
    for r in con.execute("SELECT video_id, title, duration_seconds, view_count FROM videos "
                         "WHERE manual_duplicate_of IS NULL"):
        groups.setdefault(" ".join(r["title"].split()), []).append(r)
    marked = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: -(r["view_count"] or 0))
        keepers: list = []
        for r in rows:
            match = next((k for k in keepers
                          if abs((k["duration_seconds"] or 0) - (r["duration_seconds"] or 0))
                          <= DUPLICATE_DURATION_TOLERANCE), None)
            if match:
                con.execute("UPDATE videos SET duplicate_of = ? WHERE video_id = ?",
                            (match["video_id"], r["video_id"]))
                marked += 1
            else:
                keepers.append(r)
    con.commit()
    return marked


def _fetch_comments_into(con, yt, video_ids: list[str], log) -> None:
    for i, vid in enumerate(video_ids, 1):
        before = yt_api.units_used
        try:
            rows = yt_api.fetch_all_comments(yt, vid)
            db.replace_comments(con, vid, rows)
            log(f"  คอมเมนต์ {i}/{len(video_ids)}: {vid} ได้ {len(rows)} รายการ "
                f"({yt_api.units_used - before} units)")
        except HttpError as e:
            reason = yt_api.error_reason(e)
            if reason == "quotaExceeded":
                raise
            db.replace_comments(con, vid, [], status=reason)
            log(f"  คอมเมนต์ {i}/{len(video_ids)}: {vid} ข้าม ({yt_api.ERROR_TH.get(reason, reason)})")
        _log_quota(con, yt_api.units_used - before)
        con.commit()


_units_logged = 0


def _log_quota(con, units: int) -> None:
    global _units_logged
    db.add_quota(con, units)
    _units_logged += units


def flush_quota(con) -> None:
    """Log units spent outside comment fetching (channels/playlists/videos)."""
    _log_quota(con, yt_api.units_used - _units_logged)
    con.commit()


def _ensure_channel(con, yt, channel_id: str) -> None:
    if not con.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)).fetchone():
        row = _channel_row(yt_api.fetch_channel(yt, channel_id=channel_id))
        row["last_fetched_at"] = None  # only one video was fetched, not the channel's list
        db.upsert_channel(con, row)


def _missing_comment_ids(con, channel_id: str | None = None) -> list[str]:
    sql = f"SELECT v.video_id FROM videos v WHERE {db.ELIGIBLE_SQL} AND v.comments_fetched_at IS NULL"
    args: tuple = ()
    if channel_id:
        sql += " AND v.channel_id = ?"
        args = (channel_id,)
    return [r["video_id"] for r in con.execute(sql + " ORDER BY v.view_count DESC", args)]


def ingest_video(con, yt, url: str, log=print) -> str:
    vid = yt_api.parse_video_id(url)
    if not vid:
        raise ValueError("อ่านรหัสวิดีโอจากลิงก์ไม่ได้")
    rows = yt_api.fetch_videos(yt, [vid])
    if not rows:
        raise ValueError(yt_api.ERROR_TH["videoNotFound"])
    _ensure_channel(con, yt, rows[0]["channel_id"])
    db.upsert_videos(con, rows)
    con.commit()
    log(f"วิดีโอ: {rows[0]['title']}")
    _fetch_comments_into(con, yt, [vid], log)
    return vid


def ingest_channel(con, yt, url: str, max_videos: int = 5000, recheck_comments: bool = False,
                   fetch_comments: bool = True, log=print) -> list[str]:
    ch = yt_api.fetch_channel(yt, url)
    if not ch:
        raise ValueError(yt_api.ERROR_TH["channelNotFound"])
    row = _channel_row(ch, url.strip())
    db.upsert_channel(con, row)
    known = {r["video_id"] for r in con.execute(
        "SELECT video_id FROM videos WHERE channel_id = ?", (ch["id"],))}
    new_ids = yt_api.fetch_new_video_ids(yt, row["uploads_playlist"], known, max_videos)
    videos = yt_api.fetch_videos(yt, new_ids)
    db.upsert_videos(con, videos)
    con.commit()
    dups = mark_duplicates(con)
    eligible = con.execute(f"SELECT COUNT(*) FROM videos v WHERE v.channel_id = ? AND {db.ELIGIBLE_SQL}",
                           (ch["id"],)).fetchone()[0]
    log(f"ช่อง: {row['title']} | คลิปใหม่ {len(videos)} (มีอยู่แล้ว {len(known)}) | "
        f"เข้าเกณฑ์ {eligible} | คลิปซ้ำในคลังทั้งหมด {dups}")
    if len(videos) < len(new_ids):
        log(f"  ข้าม {len(new_ids) - len(videos)} คลิปที่เป็น private/ถูกลบ")
    if not fetch_comments:
        return [v["video_id"] for v in videos]

    targets = _missing_comment_ids(con, ch["id"])
    if recheck_comments:
        targets += [r["video_id"] for r in con.execute(
            f"""SELECT v.video_id FROM videos v
               WHERE v.channel_id = ? AND {db.ELIGIBLE_SQL} AND v.comments_status = 'ok'
                 AND v.comment_count > 1.1 * (SELECT COUNT(*) FROM comments c WHERE c.video_id = v.video_id) + 5""",
            (ch["id"],))]
    _fetch_comments_into(con, yt, targets, log)
    return [v["video_id"] for v in videos]


def _eligible_ids(con) -> set[str]:
    return {r["video_id"] for r in con.execute(f"SELECT v.video_id FROM videos v WHERE {db.ELIGIBLE_SQL}")}


def refresh_stats(con, yt, statuses: list[str], log=print, all_videos: bool = False) -> int:
    """Update views/likes/comment counts. videos.list costs 1 unit per 50 clips, so refreshing
    every known clip (all_videos) is ~13 units for 600 clips — cheap, and it is the only way to
    notice a young clip that has just grown past the view threshold."""
    if all_videos:
        ids = [r["video_id"] for r in con.execute("SELECT video_id FROM videos WHERE duplicate_of IS NULL")]
    else:
        marks = ",".join("?" * len(statuses))
        ids = [r["video_id"] for r in con.execute(
            f"SELECT video_id FROM user_status WHERE status IN ({marks})", statuses)]
    before = _eligible_ids(con)
    rows = yt_api.fetch_videos(yt, ids)
    db.upsert_videos(con, rows)
    con.commit()
    log(f"อัปเดตยอดวิว/ไลก์ {len(rows)} คลิป")
    if all_videos:
        after = _eligible_ids(con)
        scored = {r["video_id"] for r in con.execute("SELECT video_id FROM scores")}
        new = sorted(after - before)
        log(f"เข้าเกณฑ์ใหม่ {len(new)} คลิป (ต้องดึงคอมเมนต์+ให้คะแนน: {len([i for i in new if i not in scored])}) · "
            f"หลุดเกณฑ์ {len(before - after)} คลิป")
        for i in new:
            log(f"  + {i}")
    return len(rows)


def print_stats(con) -> None:
    rows = con.execute(f"""
        SELECT c.title, COUNT(*) AS total,
          SUM(v.duplicate_of IS NOT NULL) AS dups,
          SUM(CASE WHEN {db.ELIGIBLE_SQL} THEN 1 ELSE 0 END) AS eligible,
          SUM(CASE WHEN {db.ELIGIBLE_SQL} AND v.comments_fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS with_comments,
          SUM(CASE WHEN {db.ELIGIBLE_SQL} AND v.comments_fetched_at IS NULL
                   THEN (COALESCE(v.comment_count, 0) + 99) / 100 + 1 ELSE 0 END) AS est_units,
          SUM(CASE WHEN {db.ELIGIBLE_SQL} AND s.video_id IS NOT NULL THEN 1 ELSE 0 END) AS scored,
          SUM(CASE WHEN {db.ELIGIBLE_SQL} THEN v.view_count ELSE 0 END) AS eligible_views
        FROM videos v JOIN channels c USING (channel_id) LEFT JOIN scores s USING (video_id)
        GROUP BY c.channel_id ORDER BY c.title""").fetchall()
    print("ช่อง | คลิปทั้งหมด | ซ้ำ | เข้าเกณฑ์ | มีคอมเมนต์แล้ว | ให้คะแนนแล้ว | โควตาที่ต้องใช้ดึงคอมเมนต์ที่ขาด (ประมาณ)")
    for r in rows:
        print(f"{r['title']} | {r['total']} | {r['dups']} | {r['eligible']} | {r['with_comments']} | "
              f"{r['scored']} | ~{r['est_units']}")
    print(f"โควตาวันนี้ (นับจากโค้ด): {db.quota_today(con)} / 10,000")


# ------------------------------------------------------------------ export for Claude

_MENTION = re.compile(r"@[\w.\-฀-๿]+")
_LETTER = re.compile(r"[A-Za-zก-ฮ]")
_NOISE = re.compile(r"^[\d\s:.,!?~\-+*/#()\[\]5]*$")


def _norm(text: str) -> str:
    return " ".join(_MENTION.sub("", text or "").replace("​", "").split())


def _is_noise(text: str) -> bool:
    """Emoji/laughs/timestamps only, e.g. '555', '😂😂', '6:38:51'."""
    letters = _LETTER.findall(text)
    return len(letters) < 2 or bool(_NOISE.match(text))


def _short(n) -> str:
    if n is None:
        return "-"
    for div, suf in ((1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


_NEG = re.compile(r"ไม่สนุก|น่าเบื่อ|เบื่อ|น้ำ|ยืด|ช้า|เสียง|พากย์|ข้าม|ตัด|ขาด|หาย|งง|ขยะ|กาก|ห่วย|เสียเวลา|"
                  r"ไม่ไหว|ไม่จบ|ซ้ำ|มั่ว|โง่|ไร้สาระ|ลาก|พูด|คุย")
_SEQUEL = re.compile(r"ต่อ|ภาค|ss ?\d|ซีซั่?น|season|ep|รอ|จบ", re.I)
_PRAISE = re.compile(r"สนุก|ชอบ|ดี|มันส์|มัน|เยี่ยม|สุดยอด|เพลิน|ฮา|ขำ|ดาว|เริ่ด|ปัง|แนะนำ|thank|good", re.I)
SHORT_LEN = 30


def compact_comments(rows, max_len: int = 160, max_reply_len: int = 100, max_replies: int = 3,
                     max_long: int = 50) -> tuple[list[str], int]:
    """Token-lean view of a video's comments for scoring. Author names are never included.

    - exact duplicates (after removing @mentions/extra spaces) are merged as ×n
    - emoji / laugh / timestamp-only comments are dropped and counted as noise
    - short (<=30 chars), low-like comments that only praise or ask for a sequel become counts;
      short comments with complaint words are always kept verbatim
    - longer comments: top 50 by likes, plus every remaining one that contains a complaint word
    - kept threads are numbered #1, #2, ... and their replies #1.1, #1.2 (at most 3 per thread,
      most-liked first), shown under threads with 3+ likes, a complaint word, or 2+ replies
    """
    tops: OrderedDict[str, dict] = OrderedDict()
    replies: dict[str, list[str]] = {}
    noise = 0
    for r in rows:
        text = _norm(r["text"])
        if _is_noise(text):
            noise += 1
            continue
        if r["parent_id"]:
            replies.setdefault(r["parent_id"], []).append((r["like_count"] or 0, text[:max_reply_len]))
            continue
        key = text.lower()
        g = tops.setdefault(key, {"text": text, "n": 0, "likes": 0, "ids": []})
        g["n"] += 1
        g["likes"] += r["like_count"] or 0
        g["ids"].append(r["comment_id"])

    praise = sequel = 0
    short_other: list[str] = []
    long_groups = []
    for g in tops.values():
        has_replies = any(replies.get(cid) for cid in g["ids"])
        if len(g["text"]) <= SHORT_LEN and g["likes"] < 2 and not has_replies:
            if _NEG.search(g["text"]):
                short_other.append(g["text"] + (f" ×{g['n']}" if g["n"] > 1 else ""))
            elif _SEQUEL.search(g["text"]):
                sequel += g["n"]
            elif _PRAISE.search(g["text"]):
                praise += g["n"]
            else:
                short_other.append(g["text"] + (f" ×{g['n']}" if g["n"] > 1 else ""))
        else:
            long_groups.append(g)

    lines = []
    ranked = sorted(long_groups, key=lambda g: (-g["likes"], -g["n"]))
    kept = ranked[:max_long] + [g for g in ranked[max_long:] if _NEG.search(g["text"])]
    for no, g in enumerate(kept, 1):
        dup = f" ×{g['n']}" if g["n"] > 1 else ""
        lines.append(f"#{no} [{g['likes']}] {g['text'][:max_len]}{dup}")
        reps = sorted((x for cid in g["ids"] for x in replies.get(cid, [])), key=lambda x: -x[0])
        # replies show how the crowd reacted (agreed / pushed back): keep them under popular threads,
        # under complaints, and under threads that drew 2+ replies
        if not reps or not (g["likes"] >= 3 or _NEG.search(g["text"]) or len(reps) >= 2):
            continue
        seen: set[str] = set()
        for rl, rep in reps:
            if rep.lower() not in seen and len(seen) < max_replies:
                seen.add(rep.lower())
                lines.append(f"  #{no}.{len(seen)} [{rl}] {rep}")
    if len(kept) < len(ranked):
        lines.append(f"(+{len(ranked) - len(kept)} more neutral/positive longer comments omitted)")
    lines.append(f"short: praise={praise} sequel_request={sequel}"
                 + (f" | {' | '.join(short_other)}" if short_other else ""))
    return lines, noise


# Rule-based counts over every top-level comment, so the scorer sees the crowd's balance before
# reading individual comments. Each category shows comments/likes (likes = how many agreed).
SIGNALS = {
    "praise": re.compile(r"(?<!ไม่)(สนุก|ชอบ|มันส์|เยี่ยม|สุดยอด|เพลิน|ปัง|เริ่ด|ดีมาก|คุ้ม|ดูวน|ดูจบ)|good|thank", re.I),
    "sequel": re.compile(r"ภาค ?ต่อ|ภาค ?\d|ขอต่อ|ต่อด่วน|ซีซั่?น|season|ss ?\d|รอ(ภาค|ตอน)", re.I),
    "audio": re.compile(r"เสียง ?(หาย|เงียบ|เบา|แตก|ไม่มี|ไม่ได้ยิน|ไม่ตรง)|ไม่มีเสียง|ไม่พากย์|พากย์หาย|ไม่มีซับ|ซับหาย"),
    "skip": re.compile(r"ข้าม|ตัดต่อ|ตัดจบ|ตอนหาย|ขาดหาย|เนื้อเรื่องหาย|หายไป ?\d|ไม่ต่อเนื่อง|กระโดด|ขาดตอน"),
    "filler": re.compile(r"น้ำ ?(เยอะ|มาก|ท่วม|ลาย)|ยืด|ลากยาว|เดินเรื่องช้า|เอาแต่พูด|พูดเยอะ|คุยเยอะ|(?<!ไม่)น่าเบื่อ"),
    "dislike": re.compile(r"ไม่สนุก|ไม่ชอบ|ห่วย|ขยะ|เสียเวลา|ไม่ไหว|ไม่สมเหตุสมผล|มั่ว|งง|เลิกดู"),
}


def comment_signals(rows) -> str:
    counts = {k: [0, 0] for k in SIGNALS}
    for r in rows:
        if r["parent_id"]:
            continue
        text = _norm(r["text"])
        for k, rx in SIGNALS.items():
            if rx.search(text):
                counts[k][0] += 1
                counts[k][1] += r["like_count"] or 0
    return "signals: " + " ".join(f"{k}={n}/{_short(l)}👍" for k, (n, l) in counts.items())


def _video_header(v, rows) -> str:
    like_pct = f"{100 * v['like_count'] / v['view_count']:.2f}%" if v["like_count"] and v["view_count"] else "-"
    return (f"ch={v['channel_title']} pub={v['published_at'][:10]} dur={(v['duration_seconds'] or 0) / 3600:.1f}h "
            f"views={_short(v['view_count'])} likes={_short(v['like_count'])} ({like_pct}) "
            f"comments={v['comment_count']} fetched={len(rows)}")


def _comment_rows(con, video_id: str):
    return con.execute("SELECT comment_id, parent_id, text, like_count FROM comments WHERE video_id = ? "
                       "ORDER BY published_at", (video_id,)).fetchall()


SCORE_BANDS = [(85, 101), (70, 85), (55, 70), (40, 55), (0, 40)]


def calibration_lines(con, per_band: int = 2) -> list[str]:
    """Already-scored clips, a couple per score band, shown as numbers + the one-line reason only
    (no comments) so new clips are scored on the same scale as the old ones."""
    out = []
    for lo, hi in SCORE_BANDS:
        rows = con.execute(f"""
            SELECT v.*, c.title AS channel_title, s.score, s.reasons FROM videos v
            JOIN channels c USING (channel_id) JOIN scores s USING (video_id)
            WHERE {db.ELIGIBLE_SQL} AND s.score >= ? AND s.score < ? AND v.comments_status = 'ok'
            ORDER BY v.comment_count DESC LIMIT ?""", (lo, hi, per_band)).fetchall()
        for v in rows:
            c_rows = _comment_rows(con, v["video_id"])
            out += [f"- **{v['score']:.0f}** {v['title'][:60]} | {_video_header(v, c_rows)}",
                    f"  {comment_signals(c_rows)} | เหตุผลเดิม: {(v['reasons'] or '')[:180]}"]
    return out


def export_scoring(con, channel: str | None = None, limit: int | None = None,
                   ids: list[str] | None = None, include_scored: bool = False,
                   with_calibration: bool = False, extra: list[str] | None = None,
                   tag: str | None = None) -> Path:
    where = [db.ELIGIBLE_SQL]
    args: list = []
    if not include_scored:
        where.append("s.video_id IS NULL")
    if channel:
        where.append("(c.title LIKE ? OR c.handle LIKE ?)")
        args += [f"%{channel}%", f"%{channel}%"]
    if ids:
        where.append(f"v.video_id IN ({','.join('?' * len(ids))})")
        args += ids
    sql = f"""SELECT v.*, c.title AS channel_title FROM videos v JOIN channels c USING (channel_id)
              LEFT JOIN scores s USING (video_id) WHERE {' AND '.join(where)}
              ORDER BY c.title, v.published_at"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    vids = con.execute(sql, args).fetchall()
    tag = tag or re.sub(r"\W+", "-", channel or ("ids" if ids else "all")).strip("-")
    out = ROOT / "data" / "to_score" / f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{tag}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {len(vids)} videos. Format: #n [likes] comment ×duplicates; #n.k = reply k under thread n. "
             "signals = rule-based counts over all top-level comments (comments/likes). "
             "Noise = emoji/laugh/timestamp-only.", ""]
    if with_calibration:
        lines += ["## มาตรฐานเดิม (คลิปที่ให้คะแนนแล้ว ช่วงคะแนนละ 2 คลิป)", *calibration_lines(con), ""]
    lines += extra or []
    for v in vids:
        rows = _comment_rows(con, v["video_id"])
        body, noise = compact_comments(rows)
        lines += [
            f"## {v['video_id']} | {v['title']}",
            f"{_video_header(v, rows)} noise={noise} status={v['comments_status']}",
            comment_signals(rows), *body, "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def print_ranked(con, score: float | None = None, channel: str | None = None) -> None:
    where, args = [db.ELIGIBLE_SQL], []
    if score is not None:
        where.append("s.score = ?"); args.append(score)
    if channel:
        where.append("(c.title LIKE ? OR c.handle LIKE ?)"); args += [f"%{channel}%"] * 2
    for r in con.execute(f"""
            SELECT v.video_id, v.title, v.view_count, v.like_count, v.published_at, v.duration_seconds,
                   s.score, s.tie_rank, s.series, s.series_order, c.title AS ch
            FROM videos v JOIN scores s USING (video_id) JOIN channels c USING (channel_id)
            WHERE {' AND '.join(where)} ORDER BY s.score DESC, s.tie_rank, v.view_count DESC""", args):
        series = f" series={r['series']}#{r['series_order']}" if r["series"] else ""
        print(f"{r['score']:.0f}/{r['tie_rank'] or '-'} {r['video_id']} {r['ch']} | views={_short(r['view_count'])} "
              f"likes={_short(r['like_count'])} {r['duration_seconds'] / 3600:.1f}h {r['published_at'][:10]}"
              f"{series} | {r['title'][:70]}")


def import_scores(con, path: Path) -> int:
    """JSON list: {video_id, score 0-100, summary, reasons, tie_rank?, series?, series_order?}"""
    items = json.loads(path.read_text(encoding="utf-8"))
    for it in items:
        seen = con.execute("SELECT COUNT(*) FROM comments WHERE video_id = ?", (it["video_id"],)).fetchone()[0]
        db.upsert_score(con, it["video_id"], float(it["score"]), it.get("summary", ""), it.get("reasons", ""),
                        seen, it.get("tie_rank"), it.get("series"), it.get("series_order"))
    con.commit()
    return len(items)


SYNC_URL = "https://yt-library-status.ytlib-765e3f.workers.dev/status"


def fetch_remote_status() -> dict:
    """Statuses the website saved to its Cloudflare Worker (public read)."""
    import urllib.request
    req = urllib.request.Request(SYNC_URL, headers={"User-Agent": "yt-library-sync/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))


def import_status(con, m: dict) -> tuple[int, int, int]:
    """{"video_id": status} or the website's {"items": {id: {"s": status|null, "t": ms}}} -> user_status.
    Returns (set, cleared, skipped)."""
    if "items" in m:
        m = {k: v.get("s") for k, v in m["items"].items()}
    known = {r["video_id"] for r in con.execute("SELECT video_id FROM videos")}
    ok = {k: v for k, v in m.items() if k in known and v in ("watched", "interested", "not_interested")}
    for vid, st in ok.items():
        db.set_status(con, vid, st)
    cleared = [k for k, v in m.items() if v is None and k in known]
    for vid in cleared:
        db.set_status(con, vid, None)
    return len(ok), len(cleared), len(m) - len(ok) - len(cleared)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("video"); s.add_argument("url")
    s = sub.add_parser("channel"); s.add_argument("url")
    s.add_argument("--max-videos", type=int, default=5000)
    s.add_argument("--recheck-comments", action="store_true")
    s.add_argument("--no-comments", action="store_true")
    sub.add_parser("comments")
    s = sub.add_parser("refresh"); s.add_argument("--status", default="interested,watched")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("import-status"); s.add_argument("file", type=Path)
    sub.add_parser("pull-status")
    sub.add_parser("stats")
    s = sub.add_parser("export-scoring")
    s.add_argument("--channel"); s.add_argument("--limit", type=int); s.add_argument("--ids")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("ranked"); s.add_argument("--score", type=float); s.add_argument("--channel")
    s = sub.add_parser("import-scores"); s.add_argument("file", type=Path)
    s = sub.add_parser("set-series"); s.add_argument("file", type=Path)
    s = sub.add_parser("set-ties"); s.add_argument("file", type=Path)
    s = sub.add_parser("set-duplicate"); s.add_argument("video_id"); s.add_argument("of")
    s.add_argument("--clear", action="store_true")
    sub.add_parser("quota")
    args = p.parse_args()

    con = db.connect()
    if args.cmd == "export-scoring":
        print(export_scoring(con, args.channel, args.limit, args.ids.split(",") if args.ids else None, args.all))
        return 0
    if args.cmd == "ranked":
        print_ranked(con, args.score, args.channel)
        return 0
    if args.cmd == "import-scores":
        print(f"นำเข้าคะแนน {import_scores(con, args.file)} คลิป")
        return 0
    if args.cmd == "set-ties":
        ranks = json.loads(args.file.read_text(encoding="utf-8"))
        db.set_tie_ranks(con, ranks)
        con.commit()
        print(f"ตั้งอันดับในกลุ่มคะแนนเท่ากัน {len(ranks)} คลิป")
        return 0
    if args.cmd == "set-series":
        groups = json.loads(args.file.read_text(encoding="utf-8"))
        for g in groups:
            db.set_series(con, g["series"], g["videos"])
        con.commit()
        print(f"จัดกลุ่ม {len(groups)} เรื่อง")
        return 0
    if args.cmd in ("import-status", "pull-status"):
        m = fetch_remote_status() if args.cmd == "pull-status" else json.loads(args.file.read_text(encoding="utf-8"))
        n_set, n_clear, n_skip = import_status(con, m)
        print(f"นำเข้าสถานะ {n_set} คลิป · ล้างสถานะ {n_clear} คลิป (ข้าม {n_skip})")
        return 0
    if args.cmd == "set-duplicate":
        for vid in (args.video_id, args.of):
            if not con.execute("SELECT 1 FROM videos WHERE video_id = ?", (vid,)).fetchone():
                print(f"ไม่พบคลิป {vid}", file=sys.stderr)
                return 1
        con.execute("UPDATE videos SET manual_duplicate_of = ? WHERE video_id = ?",
                    (None if args.clear else args.of, args.video_id))
        mark_duplicates(con)
        print(f"{args.video_id} {'ไม่ถือเป็นคลิปซ้ำแล้ว' if args.clear else 'เป็นคลิปซ้ำของ ' + args.of}")
        return 0
    if args.cmd == "stats":
        print_stats(con)
        return 0
    if args.cmd == "quota":
        print(f"วันนี้ (เวลา Pacific) ใช้ไป {db.quota_today(con)} / 10,000 units")
        return 0

    key = load_api_key()
    if not key:
        print("ไม่พบ YOUTUBE_API_KEY (ใส่ใน .streamlit/secrets.toml)", file=sys.stderr)
        return 2
    yt = yt_api.build_client(key)
    try:
        if args.cmd == "video":
            ingest_video(con, yt, args.url)
        elif args.cmd == "channel":
            ingest_channel(con, yt, args.url, args.max_videos, args.recheck_comments, not args.no_comments)
        elif args.cmd == "comments":
            ids = _missing_comment_ids(con)
            print(f"คลิปเข้าเกณฑ์ที่ยังไม่มีคอมเมนต์: {len(ids)}")
            _fetch_comments_into(con, yt, ids, print)
        elif args.cmd == "refresh":
            refresh_stats(con, yt, args.status.split(","), all_videos=args.all)
    except HttpError as e:
        print(yt_api.error_message_th(e), file=sys.stderr)
        return 1
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        flush_quota(con)
        print(f"ใช้โควตารอบนี้ {yt_api.units_used} units | วันนี้รวม {db.quota_today(con)} / 10,000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
