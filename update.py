"""One-command library update — every step is rule-based; Claude only scores at the end.

  python update.py                     full run (see steps below), then Claude scores the pack
  python update.py --publish-only [-m "ข้อความ commit"]
                                       after importing scores: pull statuses, build site, test, commit, push
  python update.py --no-publish        full run without touching git (for testing)

Steps of a full run:
  0 preflight   API key, viewing-account id, lock file (no two runs at once), git state
  1 statuses    copy the website's watched/interested/not-interested clicks into the DB
  2 channels    the viewing account's public subscriptions + channels added by hand
  3 new clips   per channel, oldest-checked first; the quota left is checked before each channel
  4 views       refresh views of every clip (the view bar rises with a clip's age)
  5 duplicates  exact re-uploads are hidden; look-alike titles are listed for Claude to decide
  6 publish     build docs/index.html (new clips show as "รอคะแนน"), run tests, commit, push
  7 comments    only for eligible clips that have none yet; quota checked before each clip
  8 pack        data/to_score/<time>_pack.md = scale examples + new clips' comments
  9 report      data/runs/latest.md (+ <time>.md/.log) — exit code 1 when something needs a look

A run that stops early (quota, network) is safe to re-run: channels are visited oldest-checked
first and a channel is only marked checked after its clips are saved, so nothing is skipped.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError

import db
import ingest
import yt_api

ROOT = Path(__file__).parent
RUNS = ROOT / "data" / "runs"
LOCK = RUNS / "update.lock"
QUOTA_RESERVE = 500            # never plan to spend the last 500 units of the day
NEW_CHANNEL_MAX_VIDEOS = 500   # first visit of a channel: newest 500 clips (~20 units)
LOCK_STALE_SECONDS = 6 * 3600


class QuotaLow(Exception):
    pass


class Run:
    def __init__(self) -> None:
        self.started = datetime.now(timezone.utc)
        self.stamp = self.started.astimezone().strftime("%Y%m%d_%H%M%S")
        self.log_lines: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.sections: list[tuple[str, list[str]]] = []
        self.pack: Path | None = None

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        self.log_lines.append(msg)

    def warn(self, msg: str) -> None:
        self.log("⚠️ " + msg)
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.log("❌ " + msg)
        self.errors.append(msg)

    def section(self, title: str, lines: list[str]) -> None:
        self.sections.append((title, lines))

    def write(self, con) -> Path:
        RUNS.mkdir(parents=True, exist_ok=True)
        status = "error" if self.errors else ("warning" if self.warnings else "ok")
        head = {"ok": "✅ เรียบร้อย ไม่มีปัญหา", "warning": "⚠️ เสร็จแต่มีคำเตือน",
                "error": "❌ มีข้อผิดพลาด ต้องตรวจสอบ"}[status]
        md = [f"# รายงานอัปเดตคลังคลิป {self.started.astimezone():%Y-%m-%d %H:%M}", "",
              f"**สถานะ:** {head}", "",
              f"**โควตา YouTube วันนี้ (เวลา Pacific):** ใช้ไป {db.quota_today(con):,} / {db.DAILY_QUOTA:,} units "
              f"(รอบนี้ {yt_api.units_used} units)", ""]
        if self.errors:
            md += ["## ข้อผิดพลาด", *[f"- {e}" for e in self.errors], ""]
        if self.warnings:
            md += ["## คำเตือน", *[f"- {w}" for w in self.warnings], ""]
        for title, lines in self.sections:
            md += [f"## {title}", *(lines or ["- ไม่มี"]), ""]
        if self.pack:
            md += ["## ขั้นถัดไปสำหรับ Claude", f"- อ่าน `{self.pack.relative_to(ROOT).as_posix()}` แล้วให้คะแนนตามเกณฑ์ใน AGENTS.md",
                   "- `python ingest.py import-scores data/scores/<ไฟล์>.json` → จัดคะแนนเท่ากัน/ซีรีส์ → "
                   "`python update.py --publish-only`", ""]
        text = "\n".join(md)
        out = RUNS / f"{self.stamp}.md"
        out.write_text(text, encoding="utf-8")
        (RUNS / "latest.md").write_text(text, encoding="utf-8")
        (RUNS / f"{self.stamp}.log").write_text("\n".join(self.log_lines), encoding="utf-8")
        (RUNS / "latest.json").write_text(json.dumps({
            "status": status, "started": self.started.isoformat(timespec="seconds"),
            "errors": self.errors, "warnings": self.warnings, "units": yt_api.units_used,
            "pack": self.pack.relative_to(ROOT).as_posix() if self.pack else None,
            "report": out.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=1), encoding="utf-8")
        return out


# ---------------------------------------------------------------- helpers

def load_secret(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    secrets = ROOT / ".streamlit" / "secrets.toml"
    if secrets.exists():
        import tomllib
        return str(tomllib.loads(secrets.read_text(encoding="utf-8")).get(name, ""))
    return ""


def ensure_budget(con, need: int, what: str) -> None:
    """Stop before starting `what` when the day's quota (minus the reserve) cannot cover it."""
    ingest.flush_quota(con)
    left = db.quota_left(con) - QUOTA_RESERVE
    if left < need:
        raise QuotaLow(f"โควตาเหลือ ~{max(left, 0)} units (กันสำรอง {QUOTA_RESERVE}) ไม่พอสำหรับ{what} "
                       f"(ประมาณ {need} units) — หยุดไว้ก่อน รอบหน้าจะทำต่อจากตรงนี้")


def quota_note(con) -> str:
    ingest.flush_quota(con)
    return f"ใช้โควตารอบนี้ {yt_api.units_used} · เหลือวันนี้ ~{db.quota_left(con):,}"


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} ล้มเหลว: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def eligible_ids(con) -> set[str]:
    return {r["video_id"] for r in con.execute(f"SELECT v.video_id FROM videos v WHERE {db.ELIGIBLE_SQL}")}


def acquire_lock(run: Run) -> bool:
    RUNS.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
        except Exception:
            info = {}
        alive = False
        try:
            import psutil
            alive = psutil.pid_exists(int(info.get("pid", -1)))
        except Exception:
            alive = time.time() - LOCK.stat().st_mtime < LOCK_STALE_SECONDS
        if alive:
            run.error(f"มีการอัปเดตอีกรอบกำลังทำงานอยู่ (pid {info.get('pid')} เริ่ม {info.get('started')}) — "
                      f"รอให้จบก่อน ถ้าแน่ใจว่าไม่มีแล้วให้ลบ {LOCK.relative_to(ROOT).as_posix()}")
            return False
        run.warn("พบไฟล์ล็อกค้างจากรอบก่อนที่จบไม่ปกติ — ลบทิ้งแล้วทำต่อ")
    LOCK.write_text(json.dumps({"pid": os.getpid(), "started": run.started.isoformat(timespec="seconds")}),
                    encoding="utf-8")
    return True


# ---------------------------------------------------------------- steps

def step_pull_status(con, run: Run) -> bool:
    try:
        n_set, n_clear, _ = ingest.import_status(con, ingest.fetch_remote_status())
        run.log(f"[1] สถานะจากเว็บ: ตั้ง {n_set} คลิป · ล้าง {n_clear} คลิป")
        return True
    except Exception as e:  # network / worker down — the site baseline would be stale, so do not publish
        run.warn(f"ดึงสถานะจากเว็บไม่ได้ ({e}) — รอบนี้จะไม่ publish เว็บ เพื่อไม่ให้สถานะเก่าทับของใหม่")
        return False


def step_channels(con, yt, run: Run) -> None:
    viewer = load_secret("VIEWER_CHANNEL_ID")
    lines: list[str] = []
    if not viewer:
        run.warn("ไม่พบ VIEWER_CHANNEL_ID ใน .streamlit/secrets.toml — ใช้เฉพาะช่องที่มีอยู่แล้ว")
    else:
        try:
            ensure_budget(con, 10, "ดึงรายชื่อช่องที่ติดตาม")
            subs = yt_api.fetch_subscriptions(yt, viewer)
        except HttpError as e:
            reason = yt_api.error_reason(e)
            if reason == "quotaExceeded":
                raise
            run.warn(f"ดึงรายชื่อช่องที่ติดตามไม่ได้: {yt_api.ERROR_TH.get(reason, reason)} — ใช้รายชื่อชุดล่าสุดที่เคยดึงไว้")
            subs = None
        if subs is not None:
            sub_ids = {s["channel_id"] for s in subs}
            known = {r["channel_id"]: r for r in con.execute("SELECT channel_id, title, origin, subscribed FROM channels")}
            new_ids = [i for i in sub_ids if i not in known]
            for ch in yt_api.fetch_channels(yt, new_ids):
                row = ingest._channel_row(ch)
                row["last_fetched_at"] = None
                db.upsert_channel(con, row)
                con.execute("UPDATE channels SET origin = 'subscription' WHERE channel_id = ?", (ch["id"],))
                lines.append(f"- ➕ ติดตามใหม่: {ch['snippet']['title']}")
            con.execute(f"UPDATE channels SET subscribed = CASE WHEN channel_id IN ({','.join('?' * len(sub_ids)) or 'NULL'}) "
                        "THEN 1 ELSE 0 END", list(sub_ids))
            for cid, r in known.items():
                if r["subscribed"] == 1 and cid not in sub_ids:
                    note = "เลิกดึงคลิปใหม่ คะแนนเดิมยังอยู่" if r["origin"] == "subscription" else "ยังดึงต่อเพราะเป็นช่องที่เพิ่มเอง"
                    lines.append(f"- ➖ เลิกติดตาม: {r['title']} ({note})")
            con.commit()
            run.log(f"[2] ช่องที่บัญชีดูติดตาม {len(sub_ids)} ช่อง · ใหม่ {len(new_ids)} · {quota_note(con)}")
    n = con.execute(f"SELECT COUNT(*) FROM channels c WHERE {db.TRACKED_CHANNELS_SQL}").fetchone()[0]
    lines.append(f"- ช่องที่ดึงคลิปใหม่รอบนี้: {n} ช่อง (ติดตามอยู่ + ช่องที่เพิ่มเอง)")
    run.section("ช่อง", lines)


def step_new_clips(con, yt, run: Run, new_channel_max: int) -> None:
    chans = con.execute(f"""SELECT c.channel_id, c.title, c.uploads_playlist, c.last_fetched_at,
                                   (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id) AS n
                            FROM channels c WHERE {db.TRACKED_CHANNELS_SQL}
                            ORDER BY c.last_fetched_at IS NOT NULL, c.last_fetched_at""").fetchall()
    lines = []
    for i, ch in enumerate(chans, 1):
        first = ch["n"] == 0
        cap = new_channel_max if first else 5000
        ensure_budget(con, 2 * math.ceil(cap / 50) + 1 if first else 4, f"ช่อง {ch['title']}")
        try:
            known = {r["video_id"] for r in con.execute("SELECT video_id FROM videos WHERE channel_id = ?",
                                                         (ch["channel_id"],))}
            playlist = ch["uploads_playlist"] or "UU" + ch["channel_id"][2:]
            new_ids = yt_api.fetch_new_video_ids(yt, playlist, known, cap)
            videos = yt_api.fetch_videos(yt, new_ids)
            db.upsert_videos(con, videos)
            con.execute("UPDATE channels SET last_fetched_at = ? WHERE channel_id = ?", (db.now_iso(), ch["channel_id"]))
            con.commit()
        except HttpError as e:
            reason = yt_api.error_reason(e)
            if reason == "quotaExceeded":
                raise
            run.warn(f"ช่อง {ch['title']}: {yt_api.ERROR_TH.get(reason, reason)} — ข้ามช่องนี้ รอบหน้าลองใหม่")
            continue
        long_ = sum(1 for v in videos if (v["duration_seconds"] or 0) > db.MIN_DURATION_SECONDS)
        msg = f"{ch['title']}: คลิปใหม่ {len(videos)} (ยาวเกิน 10 ชม. {long_})"
        if first and len(new_ids) >= cap:
            msg += f" — ช่องใหม่ ดึงย้อนหลังแค่ {cap} คลิปล่าสุด"
        if len(videos) < len(new_ids):
            msg += f" · ข้าม {len(new_ids) - len(videos)} คลิป private/ถูกลบ"
        run.log(f"[3] {i}/{len(chans)} {msg} · {quota_note(con)}")
        if videos:
            lines.append("- " + msg)
    run.section("คลิปใหม่จากแต่ละช่อง", lines)


def step_refresh(con, yt, run: Run) -> None:
    n = con.execute("SELECT COUNT(*) FROM videos WHERE duplicate_of IS NULL").fetchone()[0]
    ensure_budget(con, math.ceil(n / 50) + 1, "อัปเดตยอดวิว")
    ingest.refresh_stats(con, yt, [], log=lambda m: run.log("[4] " + m), all_videos=True)


_GENERIC = re.compile(r"พากย์ไทย|พากย์|ซับไทย|มังงะ|มังฮวา|มังฮัว|การ์ตูน|อนิเมะ|อนิเม|นิยาย|ตอนที่|ตอน|ภาค|ซีซั่?น|"
                      r"season|ss|ep|จบ|รวม|ชั่วโมง|ชม|คลิปยาว|ดูยาว|ฟังยาว|ยาวๆ|ล่าสุด|ใหม่|สปอย|สรุป|เล่าเรื่อง|"
                      r"full|movie|\d+", re.I)


def norm_title(title: str) -> str:
    return "".join(ch for ch in _GENERIC.sub(" ", title.lower()) if ch.isalnum())


def look_alikes(con, max_per_clip: int = 3) -> list[str]:
    """Rule-based candidates only — nothing is hidden here. Pairs each unscored eligible clip with
    eligible clips whose cleaned titles are similar: near-equal duration = likely re-upload,
    otherwise maybe the same story (series)."""
    rows = con.execute(f"""SELECT v.video_id, v.title, v.duration_seconds, c.title AS ch, s.score, s.series
                           FROM videos v JOIN channels c USING (channel_id) LEFT JOIN scores s USING (video_id)
                           WHERE {db.ELIGIBLE_SQL}""").fetchall()
    normed = [(r, norm_title(r["title"])) for r in rows]
    out, seen = [], set()
    for r, a in normed:
        if r["score"] is not None or len(a) < 6:
            continue
        cands = []
        for w, b in normed:
            if w["video_id"] == r["video_id"] or len(b) < 6 or frozenset((r["video_id"], w["video_id"])) in seen:
                continue
            sm = difflib.SequenceMatcher(None, a, b)
            if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
                continue
            ratio = sm.ratio()
            if ratio >= 0.6:
                cands.append((ratio, w))
        for ratio, w in sorted(cands, key=lambda x: -x[0])[:max_per_clip]:
            seen.add(frozenset((r["video_id"], w["video_id"])))
            d1, d2 = r["duration_seconds"] or 0, w["duration_seconds"] or 0
            same_len = abs(d1 - d2) <= 0.02 * max(d1, d2)
            kind = "อาจเป็นคลิปเดิมลงซ้ำ" if ratio >= 0.8 and same_len else "อาจเป็นเรื่องเดียวกัน"
            other = f"คะแนน {w['score']:.0f}" + (f", ซีรีส์ {w['series']}" if w["series"] else "") if w["score"] is not None else "ยังไม่มีคะแนน"
            out.append(f"- {kind} ({ratio:.0%}): `{r['video_id']}` {r['title'][:50]} ({r['ch']}, {d1 / 3600:.1f}h) "
                       f"↔ `{w['video_id']}` {w['title'][:50]} ({w['ch']}, {d2 / 3600:.1f}h, {other})")
    return out


def step_duplicates(con, run: Run) -> list[str]:
    hidden = ingest.mark_duplicates(con)
    alikes = look_alikes(con)
    run.log(f"[5] คลิปซ้ำแบบชื่อตรงกันทุกตัวอักษร (ซ่อนอัตโนมัติ) {hidden} คลิป · ชื่อคล้ายที่ต้องให้ Claude ตัดสิน {len(alikes)} คู่")
    run.section("ชื่อคล้ายกัน (ระบบไม่ซ่อนเอง ให้ Claude ตัดสิน)", alikes)
    return alikes


def step_publish(run: Run, message: str) -> None:
    import build_site
    try:
        build_site.main()
    except SystemExit as e:
        run.error(f"สร้างเว็บไม่ผ่าน: {e}")
        return
    t = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8")
    if t.returncode != 0:
        run.error("ชุดทดสอบไม่ผ่าน — ไม่ push เว็บ\n" + t.stdout[-1500:])
        return
    try:
        if git("rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
            run.error("git ไม่ได้อยู่ที่ branch main — ไม่ push เว็บ")
            return
        paths = ["docs/index.html", "docs/.nojekyll"]
        if not git("status", "--porcelain", "--", *paths).strip():
            run.log("[6] เว็บไม่มีอะไรเปลี่ยน ไม่ต้อง push")
            return
        git("add", "--", *paths)
        git("commit", "-m", message, "--", *paths)
        git("push", "origin", "main")
        run.log(f"[6] push เว็บแล้ว: {message} (GitHub Pages อัปเดตใน ~1 นาที)")
    except RuntimeError as e:
        run.error(str(e))


def step_comments(con, yt, run: Run) -> None:
    rows = con.execute(f"""SELECT v.video_id, v.title, v.comment_count FROM videos v
                           WHERE {db.ELIGIBLE_SQL} AND v.comments_fetched_at IS NULL
                           ORDER BY v.view_count DESC""").fetchall()
    run.log(f"[7] คลิปเข้าเกณฑ์ที่ยังไม่มีคอมเมนต์ {len(rows)} คลิป")
    for i, r in enumerate(rows, 1):
        ensure_budget(con, 2 * math.ceil((r["comment_count"] or 0) / 100) + 2, f"คอมเมนต์ของ {r['title'][:40]}")
        ingest._fetch_comments_into(con, yt, [r["video_id"]],
                                    lambda m: run.log(f"[7] {i}/{len(rows)} " + m.strip().split(": ", 1)[-1]))
    skipped = con.execute(f"""SELECT v.video_id, v.title, v.comments_status FROM videos v
                              LEFT JOIN scores s USING (video_id)
                              WHERE {db.ELIGIBLE_SQL} AND s.video_id IS NULL
                                AND v.comments_status IS NOT NULL AND v.comments_status != 'ok'""").fetchall()
    for r in skipped:
        run.warn(f"คลิป `{r['video_id']}` {r['title'][:50]} ดึงคอมเมนต์ไม่ได้ "
                 f"({yt_api.ERROR_TH.get(r['comments_status'], r['comments_status'])}) — ให้คะแนนจากสถิติอย่างเดียว")


def step_pack(con, run: Run, alikes: list[str]) -> None:
    ids = [r["video_id"] for r in con.execute(f"""
        SELECT v.video_id FROM videos v LEFT JOIN scores s USING (video_id)
        WHERE {db.ELIGIBLE_SQL} AND s.video_id IS NULL AND v.comments_fetched_at IS NOT NULL""")]
    waiting = con.execute(f"""SELECT COUNT(*) FROM videos v LEFT JOIN scores s USING (video_id)
                              WHERE {db.ELIGIBLE_SQL} AND s.video_id IS NULL""").fetchone()[0]
    if waiting > len(ids):
        run.warn(f"มี {waiting - len(ids)} คลิปที่รอคะแนนแต่ยังไม่มีคอมเมนต์ (โควตาไม่พอ) — รอบหน้าจะดึงต่อ")
    if not ids:
        run.log("[8] ไม่มีคลิปใหม่ที่ต้องให้คะแนน")
        run.section("คลิปรอคะแนน", [])
        return
    extra = (["## ชื่อคล้ายกัน (ตัดสินก่อนให้คะแนน: ลงซ้ำ → `ingest.py set-duplicate <id> <of>` · "
              "เรื่องเดียวกัน → ใส่ series ตอน import-scores)", *alikes, ""] if alikes else [])
    run.pack = ingest.export_scoring(con, ids=ids, with_calibration=True, extra=extra, tag="pack")
    lines = [f"- `{r['video_id']}` {r['title'][:70]} ({r['ch']}, views {ingest._short(r['view_count'])})"
             for r in con.execute(f"""SELECT v.video_id, v.title, v.view_count, c.title AS ch FROM videos v
                                      JOIN channels c USING (channel_id)
                                      WHERE v.video_id IN ({','.join('?' * len(ids))})
                                      ORDER BY v.view_count DESC""", ids)]
    run.section(f"คลิปรอคะแนน {len(ids)} คลิป (อยู่บนเว็บแล้วพร้อมป้าย ⏳ รอคะแนน)", lines)
    run.log(f"[8] ไฟล์ให้คะแนน: {run.pack.relative_to(ROOT).as_posix()} ({run.pack.stat().st_size // 1024} KB, {len(ids)} คลิป)")


# ---------------------------------------------------------------- main

def full_run(con, run: Run, publish: bool, new_channel_max: int) -> None:
    key = ingest.load_api_key()
    if not key:
        run.error("ไม่พบ YOUTUBE_API_KEY ใน .streamlit/secrets.toml")
        return
    dirty = [l for l in git("status", "--porcelain", check=False).splitlines() if not l[3:].startswith("docs/")]
    if dirty:
        run.warn(f"มีไฟล์โค้ดที่ยังไม่ commit {len(dirty)} ไฟล์ — รอบนี้ commit เฉพาะหน้าเว็บ (docs/)")
    yt = yt_api.build_client(key)
    status_ok = step_pull_status(con, run)
    before = eligible_ids(con)
    alikes: list[str] = []
    quota_stop = False
    try:
        step_channels(con, yt, run)
        step_new_clips(con, yt, run, new_channel_max)
        step_refresh(con, yt, run)
    except (QuotaLow, HttpError) as e:
        quota_stop = True
        msg = str(e) if isinstance(e, QuotaLow) else yt_api.error_message_th(e)
        (run.warn if isinstance(e, QuotaLow) or yt_api.error_reason(e) == "quotaExceeded" else run.error)(msg)
    alikes = step_duplicates(con, run)
    after = eligible_ids(con)
    new_elig = sorted(after - before)
    run.section("เกณฑ์คัดคลิป", [f"- เข้าเกณฑ์ทั้งหมด {len(after)} คลิป · เข้าใหม่รอบนี้ {len(new_elig)} · "
                                 f"หลุดเกณฑ์ {len(before - after)} (ยอดวิวไม่ทันเกณฑ์ตามอายุคลิป)"])
    unscored = con.execute(f"""SELECT COUNT(*) FROM videos v LEFT JOIN scores s USING (video_id)
                               WHERE {db.ELIGIBLE_SQL} AND s.video_id IS NULL""").fetchone()[0]
    if publish and status_ok:
        step_publish(run, f"Update library: {len(new_elig)} new eligible clips, {unscored} waiting for scores")
    if not quota_stop:
        try:
            step_comments(con, yt, run)
        except (QuotaLow, HttpError) as e:
            run.warn(str(e) if isinstance(e, QuotaLow) else yt_api.error_message_th(e))
    step_pack(con, run, alikes)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--publish-only", action="store_true")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("-m", "--message", default="Update scores")
    p.add_argument("--new-channel-max", type=int, default=NEW_CHANNEL_MAX_VIDEOS)
    args = p.parse_args()

    run = Run()
    con = db.connect()
    if not acquire_lock(run):
        run.write(con)
        return 1
    try:
        if args.publish_only:
            if step_pull_status(con, run):
                step_publish(run, args.message)
            else:
                run.error("ไม่ publish เพราะดึงสถานะจากเว็บไม่ได้")
            waiting = con.execute(f"""SELECT COUNT(*) FROM videos v LEFT JOIN scores s USING (video_id)
                                      WHERE {db.ELIGIBLE_SQL} AND s.video_id IS NULL""").fetchone()[0]
            run.section("คลิปรอคะแนน", [f"- เหลือ {waiting} คลิป"] if waiting else [])
        else:
            full_run(con, run, not args.no_publish, args.new_channel_max)
    except Exception as e:  # anything unexpected still ends in a report Claude can read
        import traceback
        run.error(f"ข้อผิดพลาดที่ไม่คาดคิด: {e!r}\n```\n{traceback.format_exc()[-2000:]}\n```")
    finally:
        ingest.flush_quota(con)
        report = run.write(con)
        LOCK.unlink(missing_ok=True)
    print(f"\nรายงาน: {report.relative_to(ROOT).as_posix()} · {'มีข้อผิดพลาด' if run.errors else 'สำเร็จ'}")
    return 1 if run.errors else 0


if __name__ == "__main__":
    sys.exit(main())
