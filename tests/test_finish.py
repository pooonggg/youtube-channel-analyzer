"""Finished stories (เรื่องจบจริง): 4 h+ clips whose comments confirm the story ends join the library."""
import db
import ingest
from test_sets import H, _channel, _eligible, _video


def _comments(con, vid, items, status="sample"):
    rows = [{"comment_id": f"{vid}{i}", "video_id": vid, "parent_id": None, "author": a, "text": t,
             "like_count": lk, "published_at": "2026-01-02T00:00:00Z", "reply_count": 0}
            for i, (a, t, lk) in enumerate(items)]
    db.replace_comments(con, vid, rows, status=status)


def test_done_verdict_admits_a_short_clip_and_only_from_4h(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", title="เรื่อง", dur=6 * H, views=500), _video("B", title="เรื่อง", dur=3 * H, views=500)])
    assert _eligible(con) == set()
    assert [r["video_id"] for r in db.finish_candidates(con)] == ["A"]     # under 4 h is never checked
    ingest.import_finish_verdicts(con, {"A": ["done", "ผู้ชมยืนยันว่าจบ"], "B": ["done"]})
    assert _eligible(con) == {"A"}
    assert db.finish_candidates(con) == []                                 # judged once
    ingest.import_finish_verdicts(con, {"A": ["open"]})
    assert _eligible(con) == set()


def test_young_clips_wait_and_unclear_ones_are_rechecked_later(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", title="เรื่อง", dur=5 * H, views=10), _video("Y", title="เรื่อง", dur=5 * H, pub=db.now_iso())])
    assert [r["video_id"] for r in db.finish_candidates(con)] == ["A"]     # Y is < 14 days old
    db.save_finish_check(con, "A", "unsure")
    assert db.finish_candidates(con) == []
    con.execute("UPDATE finish_checks SET checked_at = '2025-01-01T00:00:00+00:00'")
    assert [r["video_id"] for r in db.finish_candidates(con)] == ["A"]
    db.save_finish_check(con, "A", "open")
    con.execute("UPDATE finish_checks SET checked_at = '2025-01-01T00:00:00+00:00'")
    assert db.finish_candidates(con) == []                                 # open is final


def test_triage_skips_clips_nobody_calls_finished_and_keeps_lines_short(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", title="เรื่อง", dur=6 * H, views=10), _video("N", title="เรื่อง", dur=6 * H, views=10)])
    _comments(con, "A", [("@x", "สนุกมาก", 50), ("@y", "จบแล้วครับ ดีมาก " + "ก" * 200, 9),
                         ("@z", "รอภาค 2 นะ", 30), ("@ch-C", "เรื่องนี้จบบริบูรณ์ครับ", 1)])
    _comments(con, "N", [("@x", "สนุกมาก รอตอนต่อไป", 50)])
    pack, n_read, n_none = ingest.finish_triage(con, ["A", "N"], [])
    assert (n_read, n_none) == (1, 1)
    assert con.execute("SELECT verdict FROM finish_checks WHERE video_id = 'N'").fetchone()[0] == "nosignal"
    text = pack.read_text(encoding="utf-8")
    assert "N |" not in text and "สนุกมาก" not in text                    # no ending talk -> not shown
    a = text[text.index("A |"):]
    assert a.index("[ช่อง]") < a.index("จบแล้วครับ") and "รอภาค 2" in a     # uploader first, both sides kept
    assert all(len(l) <= 2 + 10 + 80 for l in a.splitlines()[1:] if l.startswith("  "))


def test_scoring_a_finished_story_keeps_its_tag_and_samples_are_enough(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _channel(con)
    db.upsert_videos(con, [_video("A", title="เรื่อง", dur=6 * H, views=10), _video("L", title="เรื่อง", dur=11 * H)])
    _comments(con, "A", [("@y", "จบดี", 1)])
    _comments(con, "L", [("@y", "ดี", 1)])
    ingest.import_finish_verdicts(con, {"A": ["done", "จบบริบูรณ์"]})
    need = {r[0] for r in con.execute(f"SELECT v.video_id FROM videos v WHERE {db.NEEDS_COMMENTS_SQL}")}
    assert need == {"L"}            # a 10 h clip with only a sample still gets its full comments
    p = tmp_path / "s.json"
    p.write_text('[{"video_id": "A", "score": 70, "summary": "", "reasons": ""}]', encoding="utf-8")
    ingest.import_scores(con, p)
    assert tuple(con.execute("SELECT finished, finished_note FROM scores WHERE video_id = 'A'").fetchone()) == (1, "จบบริบูรณ์")
