"""YouTube Data API v3 helpers (pure functions, no Streamlit dependency).

Quota (default 10,000 units/day, resets at midnight Pacific): every read call below
costs 1 unit per page. search.list is never used. Each call is counted in
`units_used` so callers can log how much quota a run spent.
"""
from __future__ import annotations

import json
import re

import isodate
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

THUMB_ORDER = ["maxres", "standard", "high", "medium", "default"]

ERROR_TH = {
    "commentsDisabled": "วิดีโอนี้ปิดคอมเมนต์",
    "quotaExceeded": "โควตาวันนี้หมดแล้ว กรุณารอรีเซ็ต (เที่ยงคืนเวลา Pacific ≈ 14:00–15:00 เวลาไทย)",
    "keyInvalid": "API key ไม่ถูกต้อง",
    "badRequest": "API key ไม่ถูกต้อง หรือคำขอไม่ถูกต้อง",
    "forbidden": "API key ไม่มีสิทธิ์ (ตรวจว่าเปิด YouTube Data API v3 และตั้ง restriction ถูกต้อง)",
    "accessNotConfigured": "ยังไม่ได้เปิดใช้ YouTube Data API v3 ใน Google Cloud project นี้",
    "videoNotFound": "ไม่พบวิดีโอ",
    "channelNotFound": "ไม่พบช่อง ตรวจสอบ handle/ID อีกครั้ง",
}

units_used = 0


def _run(request):
    global units_used
    units_used += 1
    return request.execute(num_retries=3)


def build_client(api_key: str):
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def error_reason(e: HttpError) -> str:
    try:
        return json.loads(e.content)["error"]["errors"][0]["reason"]
    except Exception:
        return "unknown"


def error_message_th(e: HttpError) -> str:
    reason = error_reason(e)
    return ERROR_TH.get(reason, f"เกิดข้อผิดพลาดจาก API ({e.resp.status}: {reason})")


def best_thumb(thumbs: dict, prefer: list[str] = THUMB_ORDER) -> str | None:
    for size in prefer:
        if size in thumbs and thumbs[size].get("url"):
            return thumbs[size]["url"]
    return None


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_channel_input(text: str) -> dict:
    text = text.strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", text)
    if m:
        return {"id": m.group(1)}
    if re.fullmatch(r"UC[\w-]{22}", text):
        return {"id": text}
    m = re.search(r"youtube\.com/(@[^/?#\s]+)", text)
    if m:
        return {"forHandle": m.group(1)}
    return {"forHandle": text if text.startswith("@") else "@" + text}


def parse_video_id(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[\w-]{11}", text) else None


def _int(d: dict, k: str):
    return int(d[k]) if k in d else None


def fetch_channel(yt, query: str | None = None, channel_id: str | None = None) -> dict | None:
    params = {"id": channel_id} if channel_id else parse_channel_input(query or "")
    resp = _run(yt.channels().list(part="snippet,contentDetails", **params))
    items = resp.get("items", [])
    return items[0] if items else None


def fetch_new_video_ids(yt, playlist_id: str, known: set[str], max_videos: int) -> list[str]:
    """Walk the uploads playlist (newest first) and stop at the first page that
    contains an already-known video, so re-running a channel costs ~1 unit."""
    ids, token = [], None
    while len(ids) < max_videos:
        resp = _run(yt.playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=token,
        ))
        page = [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        ids += [v for v in page if v not in known]
        token = resp.get("nextPageToken")
        if not token or any(v in known for v in page):
            break
    return ids[:max_videos]


def fetch_videos(yt, ids: list[str]) -> list[dict]:
    rows = []
    for i in range(0, len(ids), 50):
        resp = _run(yt.videos().list(part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50])))
        for v in resp.get("items", []):
            sn, stats, cd = v["snippet"], v.get("statistics", {}), v["contentDetails"]
            rows.append({
                "video_id": v["id"],
                "channel_id": sn["channelId"],
                "title": sn["title"],
                "published_at": sn["publishedAt"],
                "duration_seconds": int(isodate.parse_duration(cd.get("duration", "PT0S")).total_seconds()),
                "view_count": _int(stats, "viewCount"),
                "like_count": _int(stats, "likeCount"),
                "comment_count": _int(stats, "commentCount"),
                "thumbnail_url": best_thumb(sn.get("thumbnails", {}), ["maxres", "standard", "high", "medium", "default"]),
            })
    return rows


def _comment_row(c: dict, video_id: str, parent_id: str | None, reply_count: int = 0) -> dict:
    s = c["snippet"]
    return {
        "comment_id": c["id"], "video_id": video_id, "parent_id": parent_id,
        "author": s.get("authorDisplayName"), "text": s.get("textDisplay"),
        "like_count": s.get("likeCount", 0), "published_at": s.get("publishedAt"),
        "reply_count": reply_count,
    }


def fetch_all_comments(yt, video_id: str, max_threads: int = 5000) -> list[dict]:
    """Every top-level comment and every reply. Replies embedded in commentThreads
    are partial, so threads with more replies are completed via comments.list."""
    rows, token, threads = [], None, 0
    while threads < max_threads:
        resp = _run(yt.commentThreads().list(
            part="snippet,replies", videoId=video_id, maxResults=100,
            order="relevance", textFormat="plainText", pageToken=token,
        ))
        for th in resp.get("items", []):
            threads += 1
            top = th["snippet"]["topLevelComment"]
            total = th["snippet"].get("totalReplyCount", 0)
            rows.append(_comment_row(top, video_id, None, total))
            embedded = th.get("replies", {}).get("comments", [])
            replies = embedded if len(embedded) >= total else _fetch_replies(yt, top["id"])
            rows += [_comment_row(r, video_id, top["id"]) for r in replies]
        token = resp.get("nextPageToken")
        if not token:
            break
    return rows


def _fetch_replies(yt, parent_id: str) -> list[dict]:
    out, token = [], None
    while True:
        resp = _run(yt.comments().list(
            part="snippet", parentId=parent_id, maxResults=100,
            textFormat="plainText", pageToken=token,
        ))
        out += resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            return out
