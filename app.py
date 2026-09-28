"""YouTube Trending Analyzer — web UI.

Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from ytrend.channel import analyze_channel
from ytrend.master_prompt import collect_images, generate_template, generate_with_claude
from ytrend.trending import collect_trending, summarize
from ytrend.utils import LANGUAGE_NAMES, fmt_seconds
from ytrend.youtube import YouTubeClient, YouTubeError

st.set_page_config(page_title="YouTube Trending Analyzer", page_icon="📈", layout="wide")

REGIONS = {
    "India": "IN", "United States": "US", "United Kingdom": "GB", "Canada": "CA",
    "Australia": "AU", "Pakistan": "PK", "Bangladesh": "BD", "Sri Lanka": "LK",
    "Nepal": "NP", "UAE": "AE", "Saudi Arabia": "SA", "Indonesia": "ID",
    "Philippines": "PH", "Brazil": "BR", "Mexico": "MX", "Spain": "ES",
    "France": "FR", "Germany": "DE", "Japan": "JP", "South Korea": "KR",
    "Russia": "RU", "Turkey": "TR", "Nigeria": "NG", "Kenya": "KE",
}

# Thumbnails and uploaded screenshots are kept next to the app.
WORKDIR = Path(__file__).resolve().parent / "data"
WORKDIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🔑 API keys")
    yt_key = st.text_input(
        "YouTube Data API key", value=os.environ.get("YOUTUBE_API_KEY", ""), type="password",
        help="Google Cloud Console → APIs & Services → Credentials. Enable 'YouTube Data API v3'.",
    )
    claude_key = st.text_input(
        "Anthropic API key (optional)", value=os.environ.get("ANTHROPIC_API_KEY", ""), type="password",
        help="Used for thumbnail/screenshot vision analysis and the AI master prompt. "
             "Without it, a template prompt is generated from the numbers.",
    )
    if claude_key:
        os.environ["ANTHROPIC_API_KEY"] = claude_key
    st.caption("Keys stay in this browser session only.")
    st.divider()
    st.markdown(
        "**Workflow**\n1. Find what's trending by language\n2. Analyse a channel's formula\n"
        "3. Add screens + your brief → master prompt"
    )


def yt_client() -> YouTubeClient | None:
    if not yt_key:
        st.warning("Enter your YouTube Data API key in the sidebar first.")
        return None
    return YouTubeClient(yt_key)


st.title("📈 YouTube Trending Analyzer")
tab1, tab2, tab3 = st.tabs(["① Trending by language", "② Analyse channel", "③ Master prompt"])

# ----------------------------------------------------------------------------- tab 1
with tab1:
    c1, c2, c3 = st.columns([3, 2, 1])
    regions = c1.multiselect("Regions", list(REGIONS), default=["India"])
    lang_filter = c2.selectbox(
        "Language filter", ["All"] + sorted(v for k, v in LANGUAGE_NAMES.items() if k != "unknown")
    )
    per_region = c3.number_input("Videos / region", 10, 200, 50, step=10)

    if st.button("Find trending", type="primary", disabled=not regions):
        yt = yt_client()
        if yt:
            lang_code = None
            if lang_filter != "All":
                lang_code = next(k for k, v in LANGUAGE_NAMES.items() if v == lang_filter)
            try:
                with st.spinner("Fetching trending charts…"):
                    rows = collect_trending(yt, [REGIONS[r] for r in regions], int(per_region),
                                            language=lang_code)
                st.session_state.trending = summarize(rows)
                st.session_state.trending_rows = rows
            except YouTubeError as e:
                st.error(str(e))

    summary = st.session_state.get("trending")
    if summary:
        if not summary["total_videos"]:
            st.info("No trending videos matched this filter.")
        else:
            langs = summary["languages"]
            m1, m2, m3 = st.columns(3)
            m1.metric("Trending videos", summary["total_videos"])
            m2.metric("Languages", len(langs))
            m3.metric("Top category", summary["overall_categories"][0][0])

            st.subheader("Share of trending by language")
            st.bar_chart(pd.DataFrame(
                {"videos": [l["trending_videos"] for l in langs]},
                index=[l["language_name"] for l in langs],
            ), horizontal=True)

            for lang in langs:
                with st.expander(
                    f"**{lang['language_name']}** — {lang['trending_videos']} videos "
                    f"({lang['share'] * 100:.0f}%)", expanded=lang is langs[0],
                ):
                    a, b, c = st.columns(3)
                    a.markdown("**Top channel types (categories)**\n\n" + "\n".join(
                        f"- {cat} ({n})" for cat, n in lang["top_categories"]))
                    b.markdown(
                        f"**Format mix:** {lang['format_mix']}\n\n"
                        f"**Median length:** {fmt_seconds(lang['median_duration_s'])}\n\n"
                        f"**Median views/hour:** {lang['median_views_per_hour']:,}"
                    )
                    c.markdown("**Hot keywords:** " + ", ".join(w for w, _ in lang["top_keywords"][:12]))

                    st.markdown("**Top trending channels** — pick one to analyse:")
                    for ch in lang["top_channels"]:
                        col_a, col_b = st.columns([5, 1])
                        col_a.write(
                            f"**{ch['channel']}** · {ch['main_category']} · {ch['videos']} trending "
                            f"video(s) · best {ch['best_views_per_hour']:,} views/h"
                        )
                        if col_b.button("Analyse →", key=f"pick_{lang['language']}_{ch['channel_id']}"):
                            st.session_state.channel_ref = ch["channel_id"]
                            st.session_state.trending_lang = lang
                            st.success(f"Selected {ch['channel']} — open tab ② to analyse.")

                    st.markdown("**Hottest videos**")
                    cols = st.columns(5)
                    for col, v in zip(cols, lang["hottest_videos"]):
                        if v["thumbnail"]:
                            col.image(v["thumbnail"])
                        col.markdown(f"[{v['title'][:70]}]({v['url']})")
                        col.caption(f"{v['channel']} · {v['views_per_hour']:,}/h")

            with st.expander("All trending videos (table)"):
                df = pd.DataFrame(st.session_state.trending_rows)
                st.dataframe(df[["title", "channel", "language", "category", "format",
                                 "views", "views_per_hour", "engagement", "url"]],
                             use_container_width=True)

# ----------------------------------------------------------------------------- tab 2
with tab2:
    ref = st.text_input(
        "Channel to analyse", value=st.session_state.get("channel_ref", ""),
        placeholder="@handle, channel URL, channel ID (UC…), or any video URL from the channel",
    )
    c1, c2 = st.columns(2)
    n_videos = c1.slider("Recent videos to analyse", 10, 100, 30, step=5)
    n_transcripts = c2.slider(
        "Top videos to read transcripts of", 0, 6, 3,
        help="Transcripts reveal the spoken sequence (hook → segments → CTA).",
    )

    if st.button("Analyse channel", type="primary", disabled=not ref):
        yt = yt_client()
        if yt:
            try:
                with st.spinner("Reading uploads, transcripts and thumbnails…"):
                    st.session_state.analysis = analyze_channel(
                        yt, ref, n_videos=n_videos, transcripts=n_transcripts, workdir=WORKDIR)
            except (YouTubeError, RuntimeError) as e:
                st.error(str(e))

    an = st.session_state.get("analysis")
    if an:
        ch = an["channel"]
        st.subheader(f"{ch['title']}  ·  [{ch.get('handle') or 'open'}]({ch['url']})")
        m = st.columns(5)
        m[0].metric("Subscribers", f"{ch['subscribers']:,}" if ch["subscribers"] else "hidden")
        m[1].metric("Uploads / week", an["cadence"]["videos_per_week"] or "—")
        m[2].metric("Median views", f"{int(an['performance']['median_views']):,}")
        m[3].metric("Median length (long)", an["formats"]["median_long_duration"] or "—")
        m[4].metric("Main language", next(iter(an["languages"]), "—"))

        a, b = st.columns(2)
        with a:
            st.markdown("#### Upload rhythm")
            st.write(f"Best days: {', '.join(f'{d} ({n})' for d, n in an['cadence']['top_weekdays'])}")
            st.write(f"Best hours (UTC): {', '.join(f'{h}:00 ({n})' for h, n in an['cadence']['top_upload_hours_utc'])}")
            st.write(f"Format mix: {an['formats']['mix']} · Shorts median: {an['formats']['median_short_duration'] or '—'}")
            st.markdown("#### Title pattern — winners vs rest")
            st.dataframe(pd.DataFrame({"Top 25%": an["titles"]["top_quartile"],
                                       "Others": an["titles"]["rest"]}),
                         use_container_width=True)
        with b:
            st.markdown("#### Top-performing videos")
            for v in an["top_videos"][:6]:
                st.markdown(f"- [{v['title']}]({v['url']}) — {v['views_per_day']:,} views/day "
                            f"· {fmt_seconds(v['duration_s'])}")
            st.markdown("#### Frequent title words")
            st.write(", ".join(w for w, _ in an["titles"]["frequent_words"]))
            st.markdown("#### Common tags")
            st.write(", ".join(t for t, _ in an["tags"][:20]) or "—")

        if an["thumbnail_files"]:
            st.markdown("#### Thumbnails of the best videos")
            st.image(an["thumbnail_files"], width=220)

        if an["chapters"]["examples"]:
            st.markdown(f"#### Video sequence from chapters ({an['chapters']['pct_with_chapters']}% of videos use chapters)")
            for ex in an["chapters"]["examples"]:
                st.markdown(f"**{ex['title']}**")
                st.code("\n".join(f"{fmt_seconds(c['start'])}  {c['title']}" for c in ex["chapters"]))

        for d in an["deep_dive"]:
            with st.expander(f"🎬 Transcript structure — {d['title']} ({d['duration']})"):
                st.text(d["transcript"] or "No transcript available for this video.")

        st.download_button("Download analysis (JSON)",
                           json.dumps(an, ensure_ascii=False, indent=2, default=str),
                           file_name=f"analysis_{ch['id']}.json")
        st.info("Next: open tab ③ to add screenshots and your brief.")

# ----------------------------------------------------------------------------- tab 3
with tab3:
    an = st.session_state.get("analysis")
    if not an:
        st.info("Analyse a channel in tab ② first (or upload a saved analysis JSON below).")
        up = st.file_uploader("Saved analysis JSON", type=["json"])
        if up:
            st.session_state.analysis = json.load(up)
            st.rerun()
    else:
        st.markdown(f"Building a master prompt from **{an['channel']['title']}**.")
        st.markdown("#### 1. Visual references")
        st.caption("Thumbnails of the channel's top videos are already included. For a precise match "
                   "of the on-screen sequence, add screenshots of key moments of a trending video: "
                   "the first frame / hook, the title card, typical talking-head or B-roll shots, "
                   "graphics/captions, and the end screen.")
        uploads = st.file_uploader("Screenshots / extra thumbnails",
                                   type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        include_thumbs = st.checkbox("Include the auto-downloaded thumbnails", value=True)

        st.markdown("#### 2. Your brief")
        c1, c2 = st.columns(2)
        brief = {
            "my_channel_name": c1.text_input("Your channel name", placeholder="e.g. Tech Tadka"),
            "language": c2.text_input("Video language", value=next(iter(an["languages"]), "")),
            "niche_or_topics": c1.text_input("Topic(s) you want to cover", placeholder="e.g. budget phones under ₹15k"),
            "target_audience": c2.text_input("Target audience", placeholder="e.g. college students in Tier-2 cities"),
            "format": c1.selectbox("Format", ["Same as reference", "Long-form", "Shorts", "Both"]),
            "target_length": c2.text_input("Target length", placeholder="e.g. 8-10 min"),
            "on_camera": c1.selectbox("Presenter", ["Face on camera", "Faceless / voice-over",
                                                    "AI avatar", "Animation", "Screen recording"]),
            "production_tools": c2.text_input("Tools you use", placeholder="e.g. CapCut, ElevenLabs, Runway, Canva"),
            "notes": st.text_area("Anything else (what you like about the reference, constraints, budget…)"),
        }

        with st.expander("Optional: add the trending context from tab ①"):
            use_trend = st.checkbox("Include language trending summary",
                                    value=bool(st.session_state.get("trending_lang")),
                                    disabled=not st.session_state.get("trending_lang"))

        missing = [k for k in ("language", "niche_or_topics") if not brief[k]]
        if missing:
            st.caption("Tip: fill in " + " and ".join(m.replace('_', ' ') for m in missing)
                       + " for a more tailored prompt.")

        if st.button("Generate master prompt", type="primary"):
            up_dir = WORKDIR / "uploads" / an["channel"]["id"]
            up_dir.mkdir(parents=True, exist_ok=True)
            up_paths = []
            for f in uploads or []:
                p = up_dir / Path(f.name).name
                p.write_bytes(f.getbuffer())
                up_paths.append(p)
            images = collect_images(*up_paths, *(an["thumbnail_files"] if include_thumbs else []))

            trend = None
            if use_trend and st.session_state.get("trending_lang"):
                t = dict(st.session_state.trending_lang)
                t.pop("hottest_videos", None)
                trend = t

            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    with st.spinner(f"Claude is studying the data and {len(images)} image(s)… "
                                    "this can take a few minutes."):
                        st.session_state.master = generate_with_claude(an, images, brief,
                                                                       trending_context=trend)
                except Exception as e:  # show any API error in the UI
                    st.error(f"Claude request failed: {e}")
            else:
                st.warning("No Anthropic key — generating the template version (no image analysis).")
                st.session_state.master = generate_template(an, brief)

        if st.session_state.get("master"):
            st.divider()
            st.markdown(st.session_state.master)
            st.download_button("Download master prompt (.md)", st.session_state.master,
                               file_name=f"master_prompt_{an['channel']['id']}.md")
