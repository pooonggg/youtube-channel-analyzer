import pytest

import db
import ingest
import update
import yt_api


def _channel(con, cid="C", **kw):
    db.upsert_channel(con, {"channel_id": cid, "title": f"ch-{cid}", "handle": None, "url": "u",
                            "thumbnail_url": None, "uploads_playlist": "UU" + cid, "last_fetched_at": None, **kw})


def _video(vid, cid="C", title="t", dur=40000, views=100_000, pub="2026-01-01T00:00:00Z", comments=10):
    return {"video_id": vid, "channel_id": cid, "title": title, "published_at": pub, "duration_seconds": dur,
            "view_count": views, "like_count": 1, "comment_count": comments, "thumbnail_url": None}


def test_quota_guard_stops_before_overspending(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.add_quota(con, db.DAILY_QUOTA - update.QUOTA_RESERVE - 10)
    update.ensure_budget(con, 10, "x")              # exactly enough
    with pytest.raises(update.QuotaLow):
        update.ensure_budget(con, 11, "y")


def test_manual_duplicate_survives_mark_duplicates(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", title="เรื่อง ก"), _video("B", title="ชื่ออื่นเลย")])
    con.execute("UPDATE videos SET manual_duplicate_of = 'A' WHERE video_id = 'B'")
    ingest.mark_duplicates(con)
    assert dict(con.execute("SELECT video_id, duplicate_of FROM videos").fetchall()) == {"A": None, "B": "A"}


def test_first_seen_is_set_once(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A")])
    con.execute("UPDATE videos SET first_seen_at = 'old' WHERE video_id = 'A'")
    db.upsert_videos(con, [_video("A", views=5)])
    assert con.execute("SELECT first_seen_at, view_count FROM videos").fetchone()[:] == ("old", 5)


def test_norm_title_drops_generic_words():
    assert update.norm_title("【พากย์ไทย】ระบบเทพเซียน ภาค 2 EP.1-300 (12 ชั่วโมง) 🔥") == \
        update.norm_title("ระบบเทพเซียน มังงะ ตอนที่ 1-300")


def test_look_alikes_flags_reupload_and_same_story(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "C"); _channel(con, "D")
    db.upsert_videos(con, [
        _video("NEW", "C", "ระบบเทพเซียนผู้ไร้เทียมทาน พากย์ไทย ภาค 1", dur=40000),
        _video("OLD", "D", "ระบบเทพเซียนผู้ไร้เทียมทาน มังงะ ตอนที่ 1-200", dur=40300),
        _video("SEQ", "D", "ระบบเทพเซียนผู้ไร้เทียมทาน ภาค 2", dur=55000),
        _video("OTHER", "D", "ราชันย์ปีศาจกลับชาติมาเกิด", dur=40000),
    ])
    for vid in ("OLD", "SEQ", "OTHER"):
        db.upsert_score(con, vid, 70, "", "", 0)
    out = "\n".join(update.look_alikes(con))
    assert "อาจเป็นคลิปเดิมลงซ้ำ" in out and "`OLD`" in out
    assert "`SEQ`" in out and "`OTHER`" not in out


def test_comment_signals_counts_complaints_with_likes():
    rows = [
        {"parent_id": None, "text": "สนุกมาก", "like_count": 10},
        {"parent_id": None, "text": "ไม่สนุกเลย", "like_count": 2},
        {"parent_id": None, "text": "ชั่วโมงที่ 5 เสียงหาย", "like_count": 30},
        {"parent_id": None, "text": "น้ำเยอะไปหน่อย", "like_count": 1},
        {"parent_id": "x", "text": "เสียงหายจริง", "like_count": 99},   # replies are not counted
    ]
    s = ingest.comment_signals(rows)
    assert "praise=1/10👍" in s and "dislike=1/2👍" in s and "audio=1/30👍" in s and "filler=1/1👍" in s


class FakeYT:
    """Stands in for yt_api fetch functions: records which channels were visited."""
    def __init__(self, fail_on=None):
        self.visited, self.fail_on = [], fail_on


def test_new_clips_visits_oldest_checked_first_and_resumes(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "A"); _channel(con, "B"); _channel(con, "N")
    db.upsert_videos(con, [_video("a1", "A"), _video("b1", "B")])
    con.execute("UPDATE channels SET last_fetched_at = '2026-10-02' WHERE channel_id = 'A'")
    con.execute("UPDATE channels SET last_fetched_at = '2026-10-01' WHERE channel_id = 'B'")
    fake = FakeYT()

    def fetch_ids(yt, playlist, known, cap):
        fake.visited.append((playlist, cap))
        if playlist == "UUB":
            raise update.QuotaLow("stop")   # simulate running out after the first two channels
        return [playlist + "-new"]

    monkeypatch.setattr(yt_api, "fetch_new_video_ids", fetch_ids)
    monkeypatch.setattr(yt_api, "fetch_videos", lambda yt, ids: [_video(i, ids[0][2]) for i in ids])
    monkeypatch.setattr(update, "ensure_budget", lambda *a: None)
    run = update.Run()
    with pytest.raises(update.QuotaLow):
        update.step_new_clips(con, None, run, new_channel_max=500)
    # never-checked channel first (with the first-visit cap), then the oldest-checked one
    assert fake.visited == [("UUN", 500), ("UUB", 5000)]
    checked = dict(con.execute("SELECT channel_id, last_fetched_at FROM channels").fetchall())
    assert checked["N"] is not None and checked["B"] == "2026-10-01"   # B stays first in line for the next run


def test_unsubscribed_subscription_channel_is_no_longer_tracked(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "M"); _channel(con, "S1"); _channel(con, "S2")
    con.execute("UPDATE channels SET origin = 'subscription', subscribed = 1 WHERE channel_id IN ('S1', 'S2')")
    con.execute("UPDATE channels SET subscribed = 0 WHERE channel_id = 'S2'")
    tracked = {r[0] for r in con.execute(f"SELECT channel_id FROM channels c WHERE {db.TRACKED_CHANNELS_SQL}")}
    assert tracked == {"M", "S1"}
