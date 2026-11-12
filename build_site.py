"""Build the standalone static site (docs/index.html) from data/library.db for GitHub Pages.

  python build_site.py

Only public video metadata and Claude's summaries are published — never commenter names
or raw comments. Viewer status (watched / interested / not interested) is synced through the
Cloudflare Worker; filters/sort choices are remembered in the visitor's browser (localStorage).
Eligible clips without a score yet are published too and shown as "⏳ รอคะแนน".
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import db
import yt_api

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "index.html"
TEMPLATE = ROOT / "site_template.html"


def md_to_html(text: str) -> str:
    """Tiny renderer for the summary format: **bold**, '- ' bullets, blank-line paragraphs."""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for line in (text or "").splitlines():
        line = html.escape(line.strip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.startswith("- "):
            items.append(line[2:])
        else:
            flush()
            if line:
                out.append(f"<p>{line}</p>")
    flush()
    return "".join(out)


def split_summary(text: str | None) -> tuple[str, str]:
    """('one-line story pitch', rest) — the summary's first line is **bold** by convention."""
    lines = (text or "").strip().splitlines()
    m = re.fullmatch(r"\*\*(.+)\*\*", lines[0].strip()) if lines else None
    return (m.group(1), "\n".join(lines[1:])) if m else ("", text or "")


def load_videos() -> list[dict]:
    return load_data()[0]


def load_data() -> tuple[list[dict], dict]:
    """(clips, sets). Clips = every clip shown: eligible single clips and all parts of eligible sets.
    A set is one card on the site; its lead clip (set_id) carries the set's score, the other parts
    only appear in the set's episode list."""
    con = db.connect()
    rows = con.execute(f"""
        SELECT v.video_id, v.channel_id, v.title, v.published_at, v.duration_seconds, v.view_count, v.like_count,
               v.comment_count, v.thumbnail_url, c.title AS channel, c.url AS channel_url,
               s.score, s.tie_rank, s.summary, s.reasons, s.series, s.series_order,
               COALESCE(s.finished, CASE WHEN f.verdict = 'done' THEN 1 END) AS finished,
               COALESCE(s.finished_note, f.note) AS finished_note,
               p.set_id, p.part_order, p.label, p.counted, p.overlap_note
        FROM videos v JOIN channels c USING (channel_id) LEFT JOIN scores s USING (video_id)
        LEFT JOIN set_parts p USING (video_id) LEFT JOIN finish_checks f USING (video_id)
        WHERE {db.IN_LIBRARY_SQL}
        ORDER BY s.score IS NULL, s.score DESC, COALESCE(s.tie_rank, 9999), v.view_count DESC""").fetchall()
    set_rows = {r["set_id"]: r for r in con.execute("SELECT * FROM sets")}
    langs = db.channel_languages(con)
    con.close()
    videos, ranks = [], {}  # Thai-dub clips and other-language clips are ranked separately (separate tabs)
    sets: dict[str, dict] = {}
    for r in rows:
        lang = langs.get(r["channel_id"], "th")
        foreign = lang != "th"
        sid = r["set_id"]
        unit = sid is None or sid == r["video_id"]   # a non-lead part is never ranked on its own
        score = r["score"] if unit else None
        if score is not None:
            ranks[foreign] = ranks.get(foreign, 0) + 1
        tag, rest = split_summary(r["summary"]) if unit else ("", "")
        videos.append({
            "tag": html.escape(tag),
            "lang": lang, "foreign": foreign, "langLabel": db.LANGUAGE_LABELS.get(lang, lang),
            "id": r["video_id"], "title": r["title"], "pub": r["published_at"],
            "dur": r["duration_seconds"], "durText": yt_api.fmt_duration(r["duration_seconds"] or 0),
            "views": r["view_count"], "likes": r["like_count"], "comments": r["comment_count"],
            "thumb": r["thumbnail_url"], "ch": r["channel"], "chUrl": r["channel_url"],
            "score": score, "rank": ranks[foreign] if score is not None else None,
            "summary": md_to_html(rest) if unit else "", "reasons": html.escape(r["reasons"] or "") if unit else "",
            "series": r["series"] if unit else None, "order": r["series_order"] if unit else None,
            "finished": bool(r["finished"]) if unit and r["finished"] is not None else None,
            "finNote": html.escape(r["finished_note"] or "") if unit else "",
            "set": sid, "part": r["part_order"], "plabel": r["label"] or "",
            "counted": bool(r["counted"]) if sid else True, "overlap": r["overlap_note"] or "",
        })
        if sid:
            st = sets.setdefault(sid, {"title": set_rows[sid]["title"], "note": set_rows[sid]["note"] or "",
                                       "parts": [], "dur": 0, "views": 0, "likes": 0, "comments": 0})
            st["parts"].append((r["part_order"], r["video_id"]))
            if r["counted"]:
                st["dur"] += r["duration_seconds"] or 0
            st["views"] += r["view_count"] or 0
            st["likes"] += r["like_count"] or 0
            st["comments"] += r["comment_count"] or 0
    for st in sets.values():
        st["parts"] = [vid for _, vid in sorted(st["parts"])]
        st["n"] = len(st["parts"])
        st["durText"] = yt_api.fmt_duration(st["dur"])
    return videos, sets


def main() -> None:
    videos, sets = load_data()
    ids = [v["id"] for v in videos]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:  # one card (one link) per video — never publish the same video twice
        raise SystemExit(f"พบคลิปซ้ำบนเว็บ ห้าม publish: {dup}")
    con = db.connect()
    baseline = {r["video_id"]: r["status"] for r in con.execute("SELECT video_id, status FROM user_status")}
    con.close()
    data = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="minutes"), "videos": videos,
            "sets": sets, "baseline": baseline, "minViews": db.MIN_VIEWS}
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    (OUT.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"สร้าง {OUT} ({len(videos)} คลิป, ชุดตอน {len(sets)} ชุด, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
