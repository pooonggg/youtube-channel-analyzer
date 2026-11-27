"""New subscriptions of the viewing account: baseline on first sight, cheap rule gate, Claude's verdict."""
import json

import db
import ingest
import update
import yt_api


def _channel(con, cid):
    db.upsert_channel(con, {"channel_id": cid, "title": f"ch-{cid}", "handle": None, "url": "u",
                            "thumbnail_url": None, "uploads_playlist": "UU" + cid, "last_fetched_at": None})
    con.execute("UPDATE channels SET origin = 'manual' WHERE channel_id = ?", (cid,))


def _api_channel(cid):
    return {"id": cid, "snippet": {"title": f"api-{cid}", "description": "เล่าเรื่อง  มังงะ\nทุกวัน", "thumbnails": {}},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}


def _fake_api(monkeypatch, subs, durations):
    """subs: channel ids the viewer follows · durations: {channel_id: [seconds of its newest clips]}"""
    monkeypatch.setattr(update, "ensure_budget", lambda *a: None)
    monkeypatch.setattr(update, "load_secret", lambda name: "VIEWER")
    monkeypatch.setattr(yt_api, "fetch_subscriptions", lambda yt, cid: [{"channel_id": c, "title": f"t-{c}"} for c in subs])
    monkeypatch.setattr(yt_api, "fetch_channels", lambda yt, ids: [_api_channel(c) for c in ids])
    monkeypatch.setattr(yt_api, "fetch_new_video_ids",
                        lambda yt, playlist, known, cap, full=False: [f"{playlist[2:]}-{i}" for i in range(len(durations[playlist[2:]]))])
    monkeypatch.setattr(yt_api, "fetch_videos", lambda yt, ids: [
        {"video_id": i, "title": f"เรื่อง {i}", "duration_seconds": durations[i.split("-")[0]][int(i.split("-")[1])],
         "view_count": 1000 * (int(i.split("-")[1]) + 1)} for i in ids])


def _status(con):
    return dict(con.execute("SELECT channel_id, status FROM sub_watch").fetchall())


def test_first_run_remembers_existing_subscriptions_and_screens_only_later_ones(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    _channel(con, "LIB")
    _fake_api(monkeypatch, ["LIB", "OLD1", "OLD2"], {})
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    run = update.Run()
    viewer, subs = update.step_channels(con, None, run)
    assert _status(con) == {"OLD1": "baseline", "OLD2": "baseline"}       # library channel is not watched

    _fake_api(monkeypatch, ["LIB", "OLD1", "OLD2", "NEW"], {"NEW": [600, 4000]})
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    viewer, subs = update.step_channels(con, None, update.Run())
    assert _status(con)["NEW"] == "pending"
    update.step_screen(con, None, run, viewer, subs)
    assert _status(con) == {"OLD1": "baseline", "OLD2": "baseline", "NEW": "review"}   # baseline never screened
    sheet = run.channel_pack.read_text(encoding="utf-8")
    assert "NEW | api-NEW | คลิป 1 ชม.+ 1/2" in sheet and "เล่าเรื่อง มังงะ ทุกวัน" in sheet


def test_channel_without_long_clips_is_not_shown_to_claude(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    _fake_api(monkeypatch, [], {})
    update.step_channels(con, None, update.Run())                        # empty first sight -> marker row
    _fake_api(monkeypatch, ["SHORT"], {"SHORT": [300, 1200, 3599]})
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    run = update.Run()
    viewer, subs = update.step_channels(con, None, run)
    update.step_screen(con, None, run, viewer, subs)
    assert _status(con)["SHORT"] == "no_long" and run.channel_pack is None


def test_manga_verdict_adds_a_tracked_channel_and_others_stay_out(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    _fake_api(monkeypatch, [], {})
    update.step_channels(con, None, update.Run())
    _fake_api(monkeypatch, ["M", "X", "U"], {c: [40000] for c in ("M", "X", "U")})
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    run = update.Run()
    update.step_screen(con, None, run, *update.step_channels(con, None, run))
    n = ingest.import_channel_verdicts(con, {"M": ["manga", "มังฮวาพากย์ไทย"], "X": ["other"], "U": "unsure"})
    assert n == {"manga": 1, "other": 1, "unsure": 1}
    tracked = {r[0] for r in con.execute(f"SELECT channel_id FROM channels c WHERE {db.TRACKED_CHANNELS_SQL}")}
    assert tracked == {"M"}
    ch = con.execute("SELECT origin, subscribed, last_fetched_at, uploads_playlist FROM channels").fetchone()
    assert tuple(ch) == ("subscription", 1, None, "UUM")                  # next run pulls its back catalogue
    assert json.loads(con.execute("SELECT sample FROM sub_watch WHERE channel_id = 'M'").fetchone()[0])["n_long"] == 1
    # next run: M is a library channel now, X / U are not screened again
    update.step_screen(con, None, update.Run(), *update.step_channels(con, None, update.Run()))
    assert _status(con) == {"-": "baseline", "M": "manga", "X": "other", "U": "unsure"}


def test_unknown_verdict_is_rejected(tmp_path):
    con = db.connect(tmp_path / "t.db")
    try:
        ingest.import_channel_verdicts(con, {"Z": ["anime"]})
    except ValueError as e:
        assert "manga/other/unsure" in str(e)
    else:
        raise AssertionError("expected ValueError")
