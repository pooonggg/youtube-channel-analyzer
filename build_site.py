"""Build the standalone static site (docs/index.html) from data/library.db for GitHub Pages.

  python build_site.py

Only public video metadata and Claude's summaries are published — never commenter names
or raw comments. Viewer status (watched / interested / not interested) is kept in
the visitor's own browser (localStorage), because a static page has no server.
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


def load_videos() -> list[dict]:
    con = db.connect()
    rows = con.execute(f"""
        SELECT v.video_id, v.title, v.published_at, v.duration_seconds, v.view_count, v.like_count,
               v.comment_count, v.thumbnail_url, c.title AS channel, c.url AS channel_url,
               s.score, s.tie_rank, s.summary, s.reasons, s.series, s.series_order
        FROM videos v JOIN channels c USING (channel_id) LEFT JOIN scores s USING (video_id)
        WHERE {db.ELIGIBLE_SQL}
        ORDER BY s.score IS NULL, s.score DESC, COALESCE(s.tie_rank, 9999), v.view_count DESC""").fetchall()
    con.close()
    videos = []
    for i, r in enumerate(rows, 1):
        videos.append({
            "id": r["video_id"], "title": r["title"], "pub": r["published_at"],
            "dur": r["duration_seconds"], "durText": yt_api.fmt_duration(r["duration_seconds"] or 0),
            "views": r["view_count"], "likes": r["like_count"], "comments": r["comment_count"],
            "thumb": r["thumbnail_url"], "ch": r["channel"], "chUrl": r["channel_url"],
            "score": r["score"], "rank": i if r["score"] is not None else None,
            "summary": md_to_html(r["summary"]), "reasons": html.escape(r["reasons"] or ""),
            "series": r["series"], "order": r["series_order"],
        })
    return videos


def main() -> None:
    videos = load_videos()
    con = db.connect()
    baseline = {r["video_id"]: r["status"] for r in con.execute("SELECT video_id, status FROM user_status")}
    con.close()
    data = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="minutes"), "videos": videos,
            "baseline": baseline}
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    (OUT.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"สร้าง {OUT} ({len(videos)} คลิป, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
