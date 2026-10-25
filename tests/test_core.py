import db
from yt_api import best_thumb, fmt_duration, parse_channel_input, parse_video_id


def test_parse_channel_input():
    cid = "UC" + "a" * 22
    assert parse_channel_input(cid) == {"id": cid}
    assert parse_channel_input(f"https://www.youtube.com/channel/{cid}") == {"id": cid}
    assert parse_channel_input("https://www.youtube.com/@Brahimamigo-e6h") == {"forHandle": "@Brahimamigo-e6h"}
    assert parse_channel_input("abc") == {"forHandle": "@abc"}


def test_parse_video_id():
    assert parse_video_id("https://youtu.be/Fg7XAFLV7Ss?si=LScqQpyKqI2rCOya") == "Fg7XAFLV7Ss"
    assert parse_video_id("https://www.youtube.com/watch?v=Fg7XAFLV7Ss&t=3") == "Fg7XAFLV7Ss"
    assert parse_video_id("https://www.youtube.com/@x") is None


def test_best_thumb_fallback():
    assert best_thumb({"high": {"url": "h"}, "default": {"url": "d"}}) == "h"
    assert best_thumb({}) is None


def test_fmt_duration():
    assert fmt_duration(3723) == "1:02:03"
    assert fmt_duration(59) == "0:59"


def test_db_roundtrip(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_channel(con, {"channel_id": "C", "title": "ch", "handle": "@ch", "url": "u",
                            "thumbnail_url": None, "uploads_playlist": "UU", "last_fetched_at": None})
    db.upsert_videos(con, [{"video_id": "V", "channel_id": "C", "title": "t",
                            "published_at": "2026-10-01T00:00:00Z", "duration_seconds": 60,
                            "view_count": 10, "like_count": None, "comment_count": 1, "thumbnail_url": None}])
    db.set_status(con, "V", "interested")
    db.upsert_score(con, "V", 80, "s", "r", 1)
    db.add_quota(con, 3)
    db.add_quota(con, 2)
    row = con.execute(db.LIBRARY_SQL).fetchone()
    assert (row["status"], row["score"], row["channel_title"]) == ("interested", 80, "ch")
    assert db.quota_today(con) == 5
    db.set_status(con, "V", None)
    assert con.execute(db.LIBRARY_SQL).fetchone()["status"] is None


def test_mark_duplicates_requires_exact_title(tmp_path, monkeypatch):
    import ingest
    con = db.connect(tmp_path / "t.db")
    db.upsert_channel(con, {"channel_id": "C", "title": "ch", "handle": None, "url": "u",
                            "thumbnail_url": None, "uploads_playlist": "UU", "last_fetched_at": None})
    base = {"channel_id": "C", "published_at": "2026-10-01T00:00:00Z", "like_count": 1,
            "comment_count": 1, "thumbnail_url": None}
    db.upsert_videos(con, [
        {**base, "video_id": "A", "title": "เรื่อง X", "duration_seconds": 40000, "view_count": 900},
        {**base, "video_id": "B", "title": "เรื่อง  X ", "duration_seconds": 40002, "view_count": 100},
        {**base, "video_id": "C1", "title": "เรื่อง X ภาค 2", "duration_seconds": 40000, "view_count": 50},
        {**base, "video_id": "D", "title": "เรื่อง X", "duration_seconds": 50000, "view_count": 10},
    ])
    assert ingest.mark_duplicates(con) == 1
    dup = dict(con.execute("SELECT video_id, duplicate_of FROM videos").fetchall())
    assert dup == {"A": None, "B": "A", "C1": None, "D": None}


def test_compact_comments_groups_and_drops_noise():
    from ingest import compact_comments
    rows = [
        {"comment_id": "1", "parent_id": None, "text": "สนุกมาก", "like_count": 3},
        {"comment_id": "2", "parent_id": None, "text": "สนุกมาก ", "like_count": 2},
        {"comment_id": "3", "parent_id": None, "text": "555 😂", "like_count": 9},
        {"comment_id": "4", "parent_id": None, "text": "6:38:51", "like_count": 0},
        {"comment_id": "5", "parent_id": "1", "text": "@someone จริง ชอบ", "like_count": 0},
    ]
    lines, noise = compact_comments(rows)
    assert noise == 2
    assert lines == ["#1 [5] สนุกมาก ×2", "  #1.1 [0] จริง ชอบ", "short: praise=0 sequel_request=0"]


def test_compact_comments_counts_short_praise_but_keeps_complaints():
    from ingest import compact_comments
    rows = [
        {"comment_id": "1", "parent_id": None, "text": "สนุกมาก", "like_count": 0},
        {"comment_id": "2", "parent_id": None, "text": "ต่อๆ", "like_count": 0},
        {"comment_id": "3", "parent_id": None, "text": "เสียงหายช่วงกลาง", "like_count": 0},
        {"comment_id": "4", "parent_id": None, "text": "น้ำเยอะ", "like_count": 1},
    ]
    lines, _ = compact_comments(rows)
    assert lines == ["short: praise=1 sequel_request=1 | เสียงหายช่วงกลาง | น้ำเยอะ"]


def test_compact_comments_keeps_pushback_replies_under_complaints():
    from ingest import compact_comments
    rows = [
        {"comment_id": "1", "parent_id": None, "text": "น้ำเยอะมากกกกกกกกกกกกกกกก เดินเรื่องช้า", "like_count": 0},
        {"comment_id": "2", "parent_id": "1", "text": "ไม่เห็นด้วย ช่วงนั้นปูเรื่องก่อนหักมุม", "like_count": 7},
        {"comment_id": "3", "parent_id": "1", "text": "เห็นด้วย", "like_count": 1},
    ]
    lines, _ = compact_comments(rows)
    assert lines[1] == "  #1.1 [7] ไม่เห็นด้วย ช่วงนั้นปูเรื่องก่อนหักมุม"
    assert lines[2] == "  #1.2 [1] เห็นด้วย"
