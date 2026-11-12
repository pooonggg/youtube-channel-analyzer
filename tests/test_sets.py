"""Sets (ชุดตอน): one channel's story in several clips, counted together toward the 10 h rule."""
import pytest

import db
import ingest
import update
import yt_api

H = 3600


def _channel(con, cid="C"):
    db.upsert_channel(con, {"channel_id": cid, "title": f"ch-{cid}", "handle": None, "url": "u",
                            "thumbnail_url": None, "uploads_playlist": "UU" + cid, "last_fetched_at": None})


def _video(vid, cid="C", title="t", dur=4 * H, views=100_000, pub="2026-01-01T00:00:00Z", desc=None):
    return {"video_id": vid, "channel_id": cid, "title": title, "published_at": pub, "duration_seconds": dur,
            "view_count": views, "like_count": 1, "comment_count": 3, "thumbnail_url": None, "description": desc}


def _eligible(con):
    return {r[0] for r in con.execute(f"SELECT v.video_id FROM videos v WHERE {db.ELIGIBLE_SQL}")}


def _in_library(con):
    return {r[0] for r in con.execute(f"SELECT v.video_id FROM videos v WHERE {db.IN_LIBRARY_SQL}")}


def test_set_counts_hours_without_overlap_and_uses_best_part_views(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", views=100_000), _video("B", views=5_000), _video("X", dur=4 * H),
                           _video("C", views=3_000, pub="2026-01-10T00:00:00Z")])
    # 4 + 4 + 4 h but C is covered by A+B (e.g. "SS1-2" vs "SS2") -> 8 h counted: not enough
    db.save_set(con, "เรื่อง", [{"id": "A"}, {"id": "B"}, {"id": "C", "counted": False, "overlap": "อยู่ใน B แล้ว"}])
    assert _eligible(con) == set() and _in_library(con) == set()
    # 3 counted parts = 12 h, only A passes the view bar -> the whole set is in, represented by A
    db.save_set(con, "เรื่อง", [{"id": "A"}, {"id": "B"}, {"id": "C"}])
    assert _eligible(con) == {"A"}
    assert _in_library(con) == {"A", "B", "C"}
    # a deleted part no longer counts
    con.execute("UPDATE videos SET gone_at = 'x' WHERE video_id = 'C'")
    assert _eligible(con) == set()


def test_part_of_a_set_is_never_a_unit_on_its_own(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", dur=11 * H), _video("B", dur=2 * H, views=10)])
    assert _eligible(con) == {"A"}
    db.save_set(con, "เรื่อง", [{"id": "B"}, {"id": "A"}])   # lead = first part (B)
    assert _eligible(con) == {"B"} and _in_library(con) == {"A", "B"}


def test_save_set_rules(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "C"); _channel(con, "D")
    db.upsert_videos(con, [_video("A"), _video("B", cid="D"), _video("E", pub="2026-09-01T00:00:00Z")])
    with pytest.raises(ValueError, match="ช่องเดียวกัน"):
        db.save_set(con, "x", [{"id": "A"}, {"id": "B"}])
    with pytest.raises(ValueError, match="ห่าง"):
        db.save_set(con, "x", [{"id": "A"}, {"id": "E"}])
    with pytest.raises(ValueError, match="อย่างน้อย 2"):
        db.save_set(con, "x", [{"id": "A"}])


def test_regrouping_moves_parts_and_drops_empty_sets(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video(i) for i in "ABC"])
    db.save_set(con, "old", [{"id": "A"}, {"id": "B"}])
    db.save_set(con, "new", [{"id": "B"}, {"id": "A"}, {"id": "C"}])
    assert [r[0] for r in con.execute("SELECT set_id FROM sets")] == ["B"]
    assert [p["video_id"] for p in db.set_parts(con, "B")] == ["B", "A", "C"]


def test_refresh_hides_clips_youtube_no_longer_returns(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", dur=11 * H), _video("B", dur=11 * H)])
    monkeypatch.setattr(yt_api, "fetch_videos", lambda yt, ids: [_video("A", dur=11 * H)])
    ingest.refresh_stats(con, None, [], log=lambda m: None, all_videos=True)
    assert _eligible(con) == {"A"}
    monkeypatch.setattr(yt_api, "fetch_videos", lambda yt, ids: [_video("A", dur=11 * H), _video("B", dur=11 * H)])
    ingest.refresh_stats(con, None, [], log=lambda m: None, all_videos=True)   # back online -> shown again
    assert _eligible(con) == {"A", "B"}


def test_episode_range():
    assert update.episode_range("{SS 3-4}ข้ามมิติ") == ("season", 3, 4)
    assert update.episode_range("ดูรวดเดียว 3 ซีซัน! รับช่วงสำนัก") == ("season", 1, 3)
    assert update.episode_range("เทพซ่อนคม ตอนที่ 7") == ("episode", 7, 7)
    assert update.episode_range("ตอน 444~535 | เจ้าหญิงน้อย") == ("episode", 444, 535)
    assert update.episode_range("ไม่มีเลขตอน") is None


def test_set_candidates_need_same_channel_and_nearly_same_title(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "C"); _channel(con, "D")
    db.upsert_videos(con, [
        _video("P1", title="หน่วยรบพิเศษตะลุยต้าซ่ง Ep.1", pub="2026-09-30T00:00:00Z", views=90_000),
        _video("P2", title="หน่วยรบพิเศษตะลุยต้าซ่ง Ep.2", pub="2026-10-01T00:00:00Z", views=3_000),
        _video("P3", title="หน่วยรบพิเศษตะลุยต้าซ่ง Ep.3", pub="2026-10-02T00:00:00Z", views=3_000),
        # same template, different story -> must not be pulled in
        _video("Q1", title="ทะลุมิติมาเป็นคนไร้พลัง เขาปลุกระบบลงทุนตัวร้าย", pub="2026-10-03T00:00:00Z"),
        _video("Q2", title="ทะลุมิติมาเป็นชาวประมงยากจน เขาปลุกระบบตกปลา", pub="2026-10-04T00:00:00Z"),
        _video("R1", cid="D", title="หน่วยรบพิเศษตะลุยต้าซ่ง Ep.4", pub="2026-10-03T00:00:00Z"),
    ])
    lines = update.set_candidates(con)
    heads = [l for l in lines if l.startswith("- ")]
    assert len(heads) == 1 and "3 คลิป" in heads[0]
    body = "\n".join(lines)
    assert all(i in body for i in ("P1", "P2", "P3")) and "Q1" not in body and "R1" not in body


def test_set_lead_scores_the_whole_set_on_the_site(tmp_path, monkeypatch):
    import build_site
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", views=100_000), _video("B", views=5_000), _video("C", views=4_000),
                           _video("S", dur=11 * H, views=200_000)])
    db.save_set(con, "ชุด", [{"id": "A", "label": "SS1"}, {"id": "B"}, {"id": "C"}])
    for vid, sc in (("A", 80), ("B", 99), ("S", 70)):   # B's old score is ignored once it is a part
        db.upsert_score(con, vid, sc, "**pitch**\n\n- 👍 x", "r", 1)
    db.set_finished(con, "A", True, "คอมเมนต์บอกว่าจบแล้ว")
    con.commit()
    monkeypatch.setattr(db.connect, "__defaults__", (tmp_path / "t.db",))
    videos, sets = build_site.load_data()
    got = {v["id"]: (v["rank"], v["set"], v["finished"]) for v in videos}
    assert got == {"A": (1, "A", True), "S": (2, None, None), "B": (None, "A", None), "C": (None, "A", None)}
    assert sets["A"]["parts"] == ["A", "B", "C"] and sets["A"]["dur"] == 12 * H and sets["A"]["views"] == 109_000


def test_finish_signals_counts_both_sides():
    rows = [{"text": "จบแล้วสนุกมาก", "like_count": 5, "parent_id": None},
            {"text": "ยังไม่จบเหรอ รอภาคต่อ", "like_count": 2, "parent_id": None},
            {"text": "ไม่จบ", "like_count": 0, "parent_id": None}]
    s = ingest.finish_signals(rows)
    assert s.startswith("finish: done=1") and "open=2" in s
