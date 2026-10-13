import html
import math
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import db
import yt_api

st.set_page_config(page_title="คลังคลิปน่าดู", page_icon="▶️", layout="wide",
                   initial_sidebar_state="collapsed")

STATUS_LABEL = {"watched": "✅ ดูแล้ว", "interested": "⭐ สนใจ", "not_interested": "🚫 ไม่สนใจ"}
STATUS_FILTER = {"none": "ยังไม่เลือก", "interested": "⭐ สนใจ", "watched": "✅ ดูแล้ว",
                 "not_interested": "🚫 ไม่สนใจ"}
# label -> (column, ascending)
SORTS = {
    "อันดับของ Claude": ("rank_key", True),
    "ยอดวิวมากสุด": ("view_count", False),
    "วิวต่อวันมากสุด": ("views_per_day", False),
    "ไลก์มากสุด": ("like_count", False),
    "คอมเมนต์มากสุด": ("comment_count", False),
    "ใหม่ล่าสุด": ("published_at", False),
    "ยาวที่สุด": ("duration_seconds", False),
}
PAGE_SIZE = 24

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Anuphan:wght@400;500;600;700&display=swap');
html, body, .stApp, .stMarkdown, p, input, button, label, summary { font-family: 'Anuphan', sans-serif; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }
[data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none; }
.block-container { padding-top: 2.2rem; max-width: 1500px; }
.hero h1 { font-size: 30px; font-weight: 700; letter-spacing: -.3px; margin: 0; padding: 0; }
.hero p { margin: 4px 0 0; color: #aaa; font-size: 14px; }
.hero b { color: #f1f1f1; font-weight: 600; }
.yt-thumb { position: relative; width: 100%; aspect-ratio: 16/9; border-radius: 12px; overflow: hidden;
  background: #222; transition: border-radius .2s ease; }
.yt-thumb:hover { border-radius: 4px; }
.yt-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.yt-badge { position: absolute; color: #fff; font-size: 12px; font-weight: 600; padding: 2px 7px;
  border-radius: 6px; line-height: 1.5; }
.yt-dur { right: 8px; bottom: 8px; background: rgba(0,0,0,.82); }
.yt-rank { left: 8px; top: 8px; background: rgba(0,0,0,.82); font-size: 13px; }
.yt-score { right: 8px; top: 8px; font-size: 13px; }
.s-hi { background: #1e8e3e; } .s-mid { background: #c77700; } .s-lo { background: #b3261e; }
.yt-title { font-size: 15.5px; font-weight: 600; line-height: 1.4; margin: 12px 0 4px; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.yt-title a, .yt-meta a { color: inherit; text-decoration: none; }
.yt-title a:hover, .yt-meta a:hover { text-decoration: underline; }
.yt-meta { font-size: 13px; color: #aaa; line-height: 1.55; }
.yt-meta .ch { color: #ddd; }
.ep { font-size: 13.5px; line-height: 1.5; padding: 6px 0; border-bottom: 1px solid #2a2a2a; }
.ep a { color: #f1f1f1; text-decoration: none; } .ep a:hover { text-decoration: underline; }
.ep span { color: #aaa; font-size: 12.5px; }
div[data-testid="stExpander"] details { border: none; background: #181818; border-radius: 10px; }
</style>
"""


def short_num(n) -> str:
    if n is None or pd.isna(n):
        return "–"
    n = int(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else f"{int(v)}") + suf
    return str(n)


def time_ago(ts: pd.Timestamp) -> str:
    sec = (datetime.now(timezone.utc) - ts.to_pydatetime()).total_seconds()
    for unit, size in (("ปี", 31_536_000), ("เดือน", 2_592_000), ("สัปดาห์", 604_800),
                       ("วัน", 86_400), ("ชั่วโมง", 3_600), ("นาที", 60)):
        if sec >= size:
            return f"{int(sec // size)} {unit}ที่แล้ว"
    return "เมื่อสักครู่"


def load_library() -> pd.DataFrame:
    con = db.connect()
    df = pd.read_sql_query(db.LIBRARY_SQL, con)
    con.close()
    df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
    age_days = (pd.Timestamp.now(tz="UTC") - df["published_at"]).dt.total_seconds() / 86400
    need = age_days.map(db.required_views)
    df = df[(df["duration_seconds"] > db.MIN_DURATION_SECONDS) & (df["view_count"].fillna(0) > need)].copy()
    if df.empty:
        return df
    df["views_per_day"] = df["view_count"] / age_days[df.index].clip(lower=1)
    df["status"] = df["status"].fillna("none")
    # Claude's order: score high→low, then Claude's tie-break; unscored go last.
    order = df.sort_values(["score", "tie_rank", "view_count"], ascending=[False, True, False],
                           na_position="last").index
    df.loc[order, "rank_key"] = range(1, len(df) + 1)
    df["claude_rank"] = df["rank_key"].where(df["score"].notna(), 0).astype(int)
    return df


def collapse_series(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """One card per story: the clip that ranks highest in the current sort order (df is
    already sorted). Every clip of the story, in story order, goes behind a button."""
    grouped = df[df["series"].notna()]
    episodes = {k: g.sort_values("series_order") for k, g in grouped.groupby("series") if len(g) > 1}
    if not episodes:
        return df, {}
    lead_ids = {grouped[grouped["series"] == k].iloc[0]["video_id"] for k in episodes}
    hide = df["series"].isin(episodes) & ~df["video_id"].isin(lead_ids)
    return df[~hide], episodes


def on_status_change(video_id: str):
    con = db.connect()
    db.set_status(con, video_id, st.session_state[f"status_{video_id}"])
    con.close()


def score_class(score: float) -> str:
    return "s-hi" if score >= 75 else "s-mid" if score >= 50 else "s-lo"


def card(row, episodes: pd.DataFrame | None, ranks: dict[str, int]):
    dur = yt_api.fmt_duration(row.duration_seconds or 0)
    rank = f'<span class="yt-badge yt-rank">#{row.claude_rank}</span>' if row.claude_rank else ""
    score = ("" if pd.isna(row.score)
             else f'<span class="yt-badge yt-score {score_class(row.score)}">{row.score:.0f}</span>')
    url = f"https://www.youtube.com/watch?v={row.video_id}"
    st.markdown(
        f"""<a href="{url}" target="_blank"><div class="yt-thumb">
              <img src="{html.escape(row.thumbnail_url or '')}" loading="lazy">{rank}{score}
              <span class="yt-badge yt-dur">{dur}</span></div></a>
            <div class="yt-title"><a href="{url}" target="_blank" title="{html.escape(row.title)}">{html.escape(row.title)}</a></div>
            <div class="yt-meta"><a class="ch" href="{html.escape(row.channel_url)}" target="_blank">{html.escape(row.channel_title)}</a><br>
              ยอดดู {short_num(row.view_count)} ครั้ง · {time_ago(row.published_at)} · 👍 {short_num(row.like_count)} · 💬 {short_num(row.comment_count)}</div>""",
        unsafe_allow_html=True,
    )
    if isinstance(row.summary, str) and row.summary:
        with st.expander("ทำไมได้คะแนนนี้"):
            st.markdown(row.summary)
            if isinstance(row.reasons, str) and row.reasons:
                st.caption(row.reasons)
    if episodes is not None:
        with st.popover(f"📚 เรื่องนี้มี {len(episodes)} คลิป (ภาคต่อ/ฉบับอื่น)", width="stretch"):
            st.caption("เรียงตามลำดับเรื่อง · คลิปที่มีหลายซีซั่นมักครอบคลุมคลิปก่อนหน้า")
            for ep in episodes.itertuples():
                sc = "" if pd.isna(ep.score) else f" · คะแนน <b>{ep.score:.0f}</b>"
                rk = f" · อันดับ #{ranks[ep.video_id]}" if ranks.get(ep.video_id) else ""
                here = " ◀ การ์ดนี้" if ep.video_id == row.video_id else ""
                st.markdown(
                    f'<div class="ep"><a href="https://www.youtube.com/watch?v={ep.video_id}" target="_blank">'
                    f'{int(ep.series_order)}. {html.escape(ep.title)}</a>{here}<br>'
                    f'<span>{html.escape(ep.channel_title)} · {yt_api.fmt_duration(ep.duration_seconds)} · '
                    f'ยอดดู {short_num(ep.view_count)}{sc}{rk}</span></div>',
                    unsafe_allow_html=True)
    current = None if row.status == "none" else row.status
    st.segmented_control(
        "สถานะ", list(STATUS_LABEL), format_func=STATUS_LABEL.get, default=current,
        key=f"status_{row.video_id}", on_change=on_status_change, args=(row.video_id,),
        label_visibility="collapsed", width="stretch",
    )


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    df = load_library()
    n_ch = df["channel_id"].nunique() if not df.empty else 0
    st.markdown(
        f'<div class="hero"><h1>▶️ คลังคลิปน่าดู</h1>'
        f'<p>คลิปยาวกว่า <b>10 ชั่วโมง</b> · ยอดวิว <b>50K+</b> (คลิปอายุไม่ถึง 2 เดือนใช้เกณฑ์ตามอายุ) · <b>{len(df)}</b> คลิป จาก <b>{n_ch}</b> ช่อง · '
        f'Claude ให้คะแนนแล้ว <b>{int(df["score"].notna().sum()) if n_ch else 0}</b> คลิป</p></div>',
        unsafe_allow_html=True,
    )
    if df.empty:
        st.info("ยังไม่มีคลิปที่เข้าเกณฑ์ในคลัง")
        return

    st.write("")
    c1, c2, c3 = st.columns([4, 2, 2])
    kw = c1.text_input("ค้นหา", placeholder="🔍  ค้นหาชื่อคลิป", label_visibility="collapsed")
    k1 = c2.selectbox("เรียงตาม", list(SORTS), label_visibility="collapsed")
    k2 = c3.selectbox("แล้วตาม", ["แล้วตาม: —"] + [f"แล้วตาม: {k}" for k in SORTS if k != k1],
                      label_visibility="collapsed")
    statuses = st.segmented_control("สถานะ", list(STATUS_FILTER), format_func=STATUS_FILTER.get,
                                    selection_mode="multi", default=["none", "interested", "watched"],
                                    label_visibility="collapsed")
    channels = sorted(df["channel_title"].unique())
    pick_ch = st.pills("ช่อง", channels, selection_mode="multi", label_visibility="collapsed") if len(channels) > 1 else []

    view = df[df["status"].isin(statuses or list(STATUS_FILTER))]
    if pick_ch:
        view = view[view["channel_title"].isin(pick_ch)]
    if kw:
        view = view[view["title"].str.contains(kw, case=False, regex=False)]
    cols, asc = [SORTS[k1][0]], [SORTS[k1][1]]
    second = k2.removeprefix("แล้วตาม: ")
    if second in SORTS:
        cols.append(SORTS[second][0]); asc.append(SORTS[second][1])
    view = view.sort_values(cols, ascending=asc, na_position="last")
    ranks = dict(zip(df["video_id"], df["claude_rank"]))
    view, series = collapse_series(view)

    if view.empty:
        st.caption("ไม่พบคลิปตามตัวกรองนี้")
        return
    pages = math.ceil(len(view) / PAGE_SIZE)
    page = 1
    if pages > 1:
        page = st.segmented_control("หน้า", list(range(1, pages + 1)), default=1,
                                    format_func=lambda p: f"หน้า {p}", label_visibility="collapsed") or 1
    st.write("")
    rows = list(view.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE].itertuples())
    for r0 in range(0, len(rows), 4):
        grid = st.columns(4, gap="large")
        for j, row in enumerate(rows[r0:r0 + 4]):
            with grid[j]:
                card(row, series.get(row.series) if isinstance(row.series, str) else None, ranks)


if __name__ == "__main__":
    main()
