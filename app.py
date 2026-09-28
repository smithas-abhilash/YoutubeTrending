"""YouTube Trending Analyzer — web UI.

Run:  streamlit run app.py   (or double-click run.bat on Windows)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from ytrend import ai, storage
from ytrend.ai_finder import (
    AI_VERDICTS_SHOWN,
    DURATIONS,
    KEYWORD_PACKS,
    discover,
    scan_channels,
    seed_queries,
    summarize_niches,
    verify_with_claude,
)
from ytrend.channel import analyze_channel, combine_analyses
from ytrend.frames import analyze_video_file
from ytrend.master_prompt import collect_images, generate_template, refine, start_conversation
from ytrend.trending import (
    classify_styles,
    collect_trending,
    filter_language,
    find_breakouts,
    refine_languages,
    rising,
    summarize,
    trend_counts,
)
from ytrend.utils import LANGUAGE_NAMES, TIMEZONES, fmt_seconds, language_name
from ytrend.youtube import DAILY_QUOTA, YouTubeClient, YouTubeError

st.set_page_config(page_title="YouTube Trending Analyzer", page_icon="📈", layout="wide")

REGIONS = {
    "India": "IN", "United States": "US", "United Kingdom": "GB", "Canada": "CA",
    "Australia": "AU", "Pakistan": "PK", "Bangladesh": "BD", "Sri Lanka": "LK",
    "Nepal": "NP", "UAE": "AE", "Saudi Arabia": "SA", "Indonesia": "ID",
    "Philippines": "PH", "Brazil": "BR", "Mexico": "MX", "Spain": "ES",
    "France": "FR", "Germany": "DE", "Japan": "JP", "South Korea": "KR",
    "Russia": "RU", "Turkey": "TR", "Nigeria": "NG", "Kenya": "KE",
}
CATEGORIES = ["Music", "Film & Animation", "Gaming", "Entertainment", "People & Blogs", "Comedy",
              "Sports", "News & Politics", "Education", "Science & Technology", "Howto & Style",
              "Autos & Vehicles", "Pets & Animals", "Travel & Events", "Nonprofits & Activism"]
LANG_CHOICES = sorted(v for k, v in LANGUAGE_NAMES.items() if k != "unknown")
NAME_TO_CODE = {v: k for k, v in LANGUAGE_NAMES.items()}

WORKDIR = storage.DATA_DIR
WORKDIR.mkdir(exist_ok=True)
ss = st.session_state
ss.setdefault("to_analyse", [])

# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("🔑 API keys")
    yt_key = st.text_input(
        "YouTube Data API key", value=os.environ.get("YOUTUBE_API_KEY", ""), type="password",
        help="Google Cloud Console → APIs & Services → Credentials. Enable 'YouTube Data API v3'.",
    )
    claude_key = st.text_input(
        "Anthropic API key (optional)", value=os.environ.get("ANTHROPIC_API_KEY", ""), type="password",
        help="Enables channel-style labels, better language detection, comment insights, "
             "image analysis and the AI master prompt.",
    )
    if claude_key:
        os.environ["ANTHROPIC_API_KEY"] = claude_key
    st.caption("✅ Claude features on" if ai.available() else "Claude features off (template mode)")

    st.divider()
    tz = st.selectbox("Your time zone", TIMEZONES, index=0, help="Upload times are shown in this zone.")
    use_cache = st.toggle("Cache YouTube results", value=True,
                          help="Repeat lookups are served from data/ytrend.db and cost no quota.")
    used = storage.quota_used_today()
    st.markdown(f"**YouTube quota today:** {used:,} / {DAILY_QUOTA:,} units")
    st.progress(min(used / DAILY_QUOTA, 1.0))
    st.caption("Resets at midnight Pacific time. Breakout searches cost 100 units per page.")
    if st.button("Clear cache"):
        storage.cache_clear()
        st.toast("Cache cleared")


def yt_client() -> YouTubeClient | None:
    if not yt_key:
        st.warning("Enter your YouTube Data API key in the sidebar first.")
        return None
    return YouTubeClient(yt_key, use_cache=use_cache)


def add_to_analysis(ref: str, name: str) -> None:
    if ref not in ss.to_analyse:
        ss.to_analyse.append(ref)
    st.toast(f"Added {name} — open tab ④ to analyse.")


def claude_error(e: Exception) -> None:
    st.error(f"Claude request failed: {e}")


st.title("📈 YouTube Trending Analyzer")
tab_tr, tab_ai, tab_br, tab_an, tab_mp, tab_hi = st.tabs(
    ["① Trending by language", "② 🤖 AI video finder", "③ Breakout finder", "④ Analyse channels",
     "⑤ Master prompt", "⑥ History"]
)

# ============================================================================= ① trending
with tab_tr:
    c1, c2, c3 = st.columns([3, 2, 1])
    regions = c1.multiselect("Regions", list(REGIONS), default=["India"])
    lang_filter = c2.selectbox("Language filter", ["All"] + LANG_CHOICES)
    per_region = c3.number_input("Videos / region", 10, 200, 50, step=10)
    o1, o2, o3 = st.columns(3)
    want_styles = o1.checkbox("Label channel styles with Claude", value=ai.available(),
                              disabled=not ai.available(),
                              help="e.g. 'faceless AI horror Shorts' instead of 'Entertainment'.")
    want_lang = o2.checkbox("Refine languages with Claude", value=ai.available(),
                            disabled=not ai.available(),
                            help="Tells Hindi from Marathi and catches Hinglish written in English letters.")
    hide_big = o3.checkbox("Hide TV / film / music labels", value=False,
                           help="Needs style labels. Shows only channels a newcomer can realistically copy.")
    f1, f2 = st.columns([3, 1])
    excluded = f1.multiselect("Exclude categories", CATEGORIES, default=["Music", "Film & Animation", "Gaming"],
                              help="The trending chart is dominated by trailers, music videos and big gamers.")
    min_vph = f2.number_input("Min views/hour", 0, 10_000_000, 0, step=1000)

    st.caption("Note: YouTube's trending chart ranks the day's biggest videos and barely includes Shorts. "
               "For AI Shorts use tab ② 🤖 AI video finder.")
    if st.button("Find trending", type="primary", disabled=not regions):
        yt = yt_client()
        if yt:
            try:
                with st.spinner("Fetching trending charts…"):
                    rows = collect_trending(yt, [REGIONS[r] for r in regions], int(per_region))
                if want_lang:
                    with st.spinner("Claude is checking languages…"):
                        n = refine_languages(rows)
                    st.caption(f"Claude corrected the language of {n} video(s).")
                if want_styles:
                    with st.spinner("Claude is labelling channel styles…"):
                        classify_styles(rows)
                storage.snapshot_save([REGIONS[r] for r in regions], rows)
                ss.trending_rows = rows
                ss.trending_regions = [REGIONS[r] for r in regions]
            except YouTubeError as e:
                st.error(str(e))
            except Exception as e:
                claude_error(e)

    rows = ss.get("trending_rows")
    if rows:
        code = NAME_TO_CODE.get(lang_filter) if lang_filter != "All" else None
        view = [r for r in filter_language(rows, code)
                if r["category"] not in excluded and r["views_per_hour"] >= min_vph]
        summary = summarize(view, exclude_big_media=hide_big)
        if not summary["total_videos"]:
            st.info("No trending videos matched this filter.")
        else:
            langs = summary["languages"]
            m1, m2, m3 = st.columns(3)
            m1.metric("Trending videos", summary["total_videos"])
            m2.metric("Languages", len(langs))
            top_label = (summary["overall_styles"] or summary["overall_categories"])[0][0]
            m3.metric("Top channel type", top_label)

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
                    if lang["top_styles"]:
                        a.markdown("**Trending channel types**\n\n" + "\n".join(
                            f"- {s} ({n})" for s, n in lang["top_styles"]))
                    else:
                        a.markdown("**Top categories**\n\n" + "\n".join(
                            f"- {cat} ({n})" for cat, n in lang["top_categories"]))
                    presenters = ", ".join(f"{p.replace('_', ' ')} ({n})" for p, n in lang["presenters"])
                    b.markdown(
                        f"**Format mix:** {lang['format_mix']}\n\n"
                        f"**Median length:** {fmt_seconds(lang['median_duration_s'])}\n\n"
                        f"**Median views/hour:** {lang['median_views_per_hour']:,}"
                        + (f"\n\n**Presenter:** {presenters}" if presenters else "")
                    )
                    c.markdown("**Hot keywords:** " + ", ".join(w for w, _ in lang["top_keywords"][:12]))

                    st.markdown("**Top trending channels** — add to your analysis list:")
                    for ch in lang["top_channels"]:
                        col_a, col_b = st.columns([5, 1])
                        badge = " · 🏢 big media" if ch.get("is_big_media") else ""
                        col_a.write(
                            f"**{ch['channel']}** · {ch.get('style') or ch['main_category']} · "
                            f"{ch['videos']} trending video(s) · best {ch['best_views_per_hour']:,} views/h{badge}"
                        )
                        if col_b.button("Add →", key=f"pick_{lang['language']}_{ch['channel_id']}"):
                            add_to_analysis(ch["channel_id"], ch["channel"])
                            ss.trending_lang = lang

                    st.markdown("**Hottest videos**")
                    cols = st.columns(5)
                    for col, v in zip(cols, lang["hottest_videos"]):
                        if v["thumbnail"]:
                            col.image(v["thumbnail"])
                        col.markdown(f"[{v['title'][:70]}]({v['url']})")
                        col.caption(f"{v['channel']} · {v['views_per_hour']:,}/h")

            with st.expander("All trending videos (table)"):
                cols_ = [c for c in ("title", "channel", "language", "style", "category", "format",
                                     "views", "views_per_hour", "engagement", "url") if c in view[0]]
                st.dataframe(pd.DataFrame(view)[cols_], width="stretch")

    # ---- trends over time
    st.divider()
    st.subheader("📅 Trends over time")
    hist_regions = ss.get("trending_regions") or [REGIONS[r] for r in regions]
    snaps = storage.snapshots_load(hist_regions, days=60)
    if len(snaps) < 2:
        st.caption(f"{len(snaps)} daily snapshot(s) saved for {', '.join(hist_regions) or '—'}. "
                   "Run 'Find trending' on at least two different days to see what is rising or fading.")
    else:
        t1, t2 = st.columns(2)
        by = t1.radio("Group by", ["style", "category", "language", "format"], horizontal=True)
        tlang = t2.selectbox("Language", ["All"] + LANG_CHOICES, key="trend_lang")
        table = trend_counts(snaps, by=by, language=NAME_TO_CODE.get(tlang) if tlang != "All" else None)
        if table:
            df = pd.DataFrame(table).T.fillna(0).sort_index()
            top_cols = df.mean().sort_values(ascending=False).index[:8]
            st.line_chart(df[top_cols])
            st.caption("% of trending videos per day")
            rf = rising(table)
            r1, r2 = st.columns(2)
            r1.markdown("**Rising** 📈\n\n" + ("\n".join(f"- {k}: +{d} pts" for k, d in rf["rising"]) or "—"))
            r2.markdown("**Fading** 📉\n\n" + ("\n".join(f"- {k}: {d} pts" for k, d in rf["fading"]) or "—"))

# ============================================================================= ② AI video finder
VERDICT_BADGE = {"ai_generated": "🤖 AI", "likely_ai": "🤖 likely AI", "unclear": "❔ unclear", "not_ai": "🎥 not AI"}


def render_ai_results(found: list[dict], funnel: dict | None, context_label: str) -> None:
    if funnel:
        st.caption("How results were narrowed: " + " → ".join(f"{k}: **{v}**" for k, v in funnel.items()))
    if not found:
        st.info("Nothing left after the filters — lower 'Min views/hour', allow more days, or add keywords. "
                "The counts above show which step removed the videos.")
        return
    # Without Claude most AI videos can't be recognised from metadata, so show "unclear" too.
    show_unclear = st.toggle("Include videos marked ❔ unclear", value=not any(r.get("ai_checked") for r in found),
                             key=f"unclear_{context_label}",
                             help="Many AI channels never write 'AI' in titles; without Claude's thumbnail "
                                  "check they are marked unclear.")
    show_not = st.toggle("Include videos judged 🎥 not AI", value=False, key=f"notai_{context_label}")
    allowed = set(AI_VERDICTS_SHOWN) | ({"unclear"} if show_unclear else set()) | ({"not_ai"} if show_not else set())
    shown = [r for r in found if r["ai_verdict"] in allowed]

    ai_rows = [r for r in found if r["ai_verdict"] in AI_VERDICTS_SHOWN]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Videos", len(found))
    m2.metric("AI / likely AI", len(ai_rows))
    m3.metric("Shorts", sum(r["format"] == "short" for r in found))
    m4.metric("Median VPH (shown)", f"{int(pd.Series([r['views_per_hour'] for r in shown]).median()):,}" if shown else "—")

    niches = summarize_niches(found, include_unclear=show_unclear)
    if niches:
        st.subheader("Niches ranked by median views/hour")
        st.dataframe(pd.DataFrame(niches), width="stretch", hide_index=True,
                     column_config={"best_url": st.column_config.LinkColumn("best video link"),
                                    "median_vph": st.column_config.NumberColumn("median VPH", format="%d"),
                                    "best_vph": st.column_config.NumberColumn("best VPH", format="%d"),
                                    "shorts_pct": st.column_config.NumberColumn("% Shorts", format="%d%%")})
        ss.trending_lang = {"source": f"AI video finder ({context_label})", "ai_niches_by_vph": niches[:12]}
        st.caption("This table is passed to the master prompt as trending context.")

    watched = {w["channel_id"] for w in storage.watch_list()}
    st.subheader(f"Videos ({len(shown)})")
    for r in shown[:60]:
        a, b, c = st.columns([1, 4, 1])
        if r["thumbnail"]:
            a.image(r["thumbnail"])
        subs = f"{r['subscribers']:,} subs" if r.get("subscribers") is not None else "subs hidden"
        b.markdown(
            f"**[{r['title']}]({r['url']})**  \n{r['channel']} · {subs} · "
            f"**{r['views_per_hour']:,} views/h** · {r['views']:,} views · "
            f"{r['format']} {fmt_seconds(r['duration_s'])} · {language_name(r['language'])}  \n"
            f"{VERDICT_BADGE[r['ai_verdict']]} ({r['ai_confidence']}%)"
            + (f" · _{r['ai_niche']}_" if r.get("ai_niche") else "")
            + f" — {r['ai_reason']}"
        )
        if c.button("Add →", key=f"ai_{context_label}_{r['video_id']}", help="Add channel to the analysis list"):
            add_to_analysis(r["channel_id"], r["channel"])
        if r["channel_id"] not in watched and c.button("⭐ Watch", key=f"w_{context_label}_{r['video_id']}"):
            storage.watch_add(r["channel_id"], r["channel"])
            st.toast(f"{r['channel']} added to your watchlist")


with tab_ai:
    st.markdown("Finds **AI-generated videos (mostly Shorts) with strong views per hour**. YouTube's trending "
                "chart barely includes Shorts, so AI channels rarely show up in tab ①.")
    if not ai.available():
        st.warning("No Anthropic key: videos can only be recognised as AI from YouTube's AI label or AI words in the "
                   "title/tags. Most AI channels don't write 'AI', so they show as ❔ unclear. Add a key in the "
                   "sidebar for Claude's thumbnail check.")
    mode = st.radio("How to search", ["🔎 Keyword search", "🌱 Similar to channels I like", "⭐ My watchlist"],
                    horizontal=True)

    c1, c2, c3, c4 = st.columns(4)
    a_region = c1.selectbox("Region", ["Any"] + list(REGIONS), index=1 + list(REGIONS).index("India"), key="a_region")
    a_lang = c2.selectbox("Language", ["Any"] + LANG_CHOICES, key="a_lang",
                          help="Only a hint to YouTube search — results in other languages still appear.")
    a_days = c3.slider("Uploaded in the last N days", 1, 14, 3 if mode != "⭐ My watchlist" else 7, key="a_days")
    a_fmt = c4.selectbox("Format", list(DURATIONS), key="a_fmt")
    c5, c6, c7 = st.columns(3)
    a_min_vph = c5.number_input("Min views/hour", 0, 10_000_000, 200, step=100)
    a_max_subs = c6.select_slider("Max channel subscribers", ["No limit", 10_000, 50_000, 100_000, 500_000, 1_000_000],
                                  value="No limit", format_func=lambda n: n if isinstance(n, str) else f"{n:,}")
    a_pages = c7.number_input("Pages per query (50 videos each)", 1, 3, 1)
    v1, v2, v3 = st.columns(3)
    a_verify = v1.checkbox("Verify with Claude (thumbnails)", value=ai.available(), disabled=not ai.available())
    a_thumbs = v2.checkbox("Include thumbnails in the check", value=True, disabled=not a_verify)
    a_no_tut = v3.checkbox("Remove tutorials / 'kaise banaye' videos", value=True)

    region_code = None if a_region == "Any" else REGIONS[a_region]
    lang_code = NAME_TO_CODE.get(a_lang) if a_lang != "Any" else None
    max_subs = None if a_max_subs == "No limit" else a_max_subs

    def _verify(found: list[dict]) -> None:
        if found and a_verify:
            with st.spinner(f"Claude is checking {min(len(found), 120)} videos for AI content…"):
                verify_with_claude(found, use_thumbnails=a_thumbs)

    def _run(label: str, fn) -> None:
        yt = yt_client()
        if not yt:
            return
        try:
            prog = st.progress(0.0, text="Searching…")
            found, funnel = fn(yt, lambda f: prog.progress(f, text="Searching…"))
            _verify(found)
            ss.ai_found, ss.ai_funnel, ss.ai_label = found, funnel, label
            storage.history_save("ai_finder", f"AI finder: {label} — {len(found)} videos", found)
        except YouTubeError as e:
            st.error(str(e))
        except Exception as e:
            claude_error(e)

    if mode == "🔎 Keyword search":
        packs = st.multiselect("Keyword packs", list(KEYWORD_PACKS),
                               default=["AI Shorts (general)", "AI stories (Hindi / Indian)", "AI devotional / mythology"])
        custom = st.text_input("Extra keywords (comma separated)", placeholder="e.g. #aicricket, ai village life")
        queries = list(dict.fromkeys([q for p in packs for q in KEYWORD_PACKS[p]] +
                                     [q.strip() for q in custom.split(",") if q.strip()]))
        st.caption(f"{len(queries)} queries: {', '.join(queries) or '—'} · up to {len(queries) * int(a_pages) * 100:,} "
                   f"quota units (cached 6 h; {max(DAILY_QUOTA - storage.quota_used_today(), 0):,} left today)")
        if st.button("Find AI videos", type="primary", disabled=not queries):
            _run("keywords", lambda yt, p: discover(
                yt, queries, region_code, lang_code, days=a_days, duration=DURATIONS[a_fmt], pages=int(a_pages),
                max_subscribers=max_subs, min_vph=a_min_vph, exclude_tutorials=a_no_tut, progress=p))

    elif mode == "🌱 Similar to channels I like":
        seeds_text = st.text_area("AI channels you like — one per line (@handle, URL, or any of their video links)",
                                  height=90, key="seeds")
        per_seed = st.slider("Search queries per channel", 2, 6, 4,
                             help="Built from each channel's own hashtags and title words. 100 quota units each.")
        seeds = [s.strip() for s in seeds_text.splitlines() if s.strip()]
        if st.button("Find similar AI videos", type="primary", disabled=not seeds):
            yt = yt_client()
            if yt:
                try:
                    with st.spinner("Reading the seed channels…"):
                        qs, seed_info = seed_queries(yt, seeds, per_seed)
                    ss.seed_queries = qs
                    for s_ in seed_info:
                        storage.watch_add(s_["channel_id"], s_["channel"])
                except YouTubeError as e:
                    st.error(str(e))
                    qs = []
                if qs:
                    _run("similar to " + ", ".join(seeds)[:60], lambda yt_, p: discover(
                        yt_, qs, region_code, lang_code, days=a_days, duration=DURATIONS[a_fmt],
                        pages=int(a_pages), max_subscribers=max_subs, min_vph=a_min_vph,
                        exclude_tutorials=a_no_tut, progress=p))
        if ss.get("seed_queries"):
            st.caption("Queries built from your channels: " + ", ".join(ss.seed_queries)
                       + " · the seed channels were also added to your ⭐ watchlist.")

    else:
        wl = storage.watch_list()
        if not wl:
            st.info("Your watchlist is empty. Click ⭐ Watch on any result, or use 'Similar to channels I like'.")
        else:
            st.caption(f"{len(wl)} channels · about {len(wl) * 3} quota units per check")
            cols = st.columns(3)
            for i, w in enumerate(wl):
                if cols[i % 3].button(f"✖ {w['title']}", key=f"unwatch_{w['channel_id']}", help="Remove from watchlist"):
                    storage.watch_remove(w["channel_id"])
                    st.rerun()
            if st.button("Check watchlist", type="primary"):
                _run("watchlist", lambda yt, p: scan_channels(
                    yt, [w["channel_id"] for w in wl], days=a_days, min_vph=a_min_vph, progress=p))

    if ss.get("ai_found") is not None:
        st.divider()
        render_ai_results(ss.ai_found, ss.get("ai_funnel"), ss.get("ai_label", "results"))


# ============================================================================= ③ breakouts
with tab_br:
    st.markdown("Recent videos from **small channels** that got far more views than their size — "
                "the formats a newcomer can realistically copy.")
    c1, c2, c3 = st.columns(3)
    b_region = c1.selectbox("Region", list(REGIONS), key="b_region")
    b_lang = c2.selectbox("Language", ["Any"] + LANG_CHOICES, index=1 + LANG_CHOICES.index("Hindi"), key="b_lang")
    b_query = c3.text_input("Niche / keyword (optional)", placeholder="e.g. horror story, stock market, recipe")
    c4, c5, c6 = st.columns(3)
    b_days = c4.slider("Published in the last N days", 1, 30, 7)
    b_max_subs = c5.select_slider("Max channel subscribers", [10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000],
                                  value=250_000, format_func=lambda n: f"{n:,}")
    b_pages = c6.number_input("Search pages (100 quota units each)", 1, 5, 1)
    b_label = st.checkbox("Label styles with Claude", value=ai.available(), disabled=not ai.available(),
                          key="b_label")

    if st.button("Find breakouts", type="primary"):
        yt = yt_client()
        if yt:
            try:
                with st.spinner("Searching recent videos…"):
                    res = find_breakouts(
                        yt, REGIONS[b_region], NAME_TO_CODE.get(b_lang) if b_lang != "Any" else None,
                        days=b_days, query=b_query or None, pages=int(b_pages), max_subscribers=b_max_subs)
                if res and b_label:
                    with st.spinner("Claude is labelling styles…"):
                        classify_styles(res)
                ss.breakouts = res
                storage.history_save("breakouts", f"Breakouts {b_region}/{b_lang}/{b_query or 'any'}", res)
            except YouTubeError as e:
                st.error(str(e))
            except Exception as e:
                claude_error(e)

    res = ss.get("breakouts")
    if res is not None:
        if not res:
            st.info("No breakout videos found — try more days, a higher subscriber limit, or another keyword.")
        else:
            styles = pd.Series([r.get("style") for r in res if r.get("style")]).value_counts()
            if len(styles):
                st.markdown("**Breakout formats:** " + ", ".join(f"{s} ({n})" for s, n in styles.head(8).items()))
            for r in res[:25]:
                a, b, c = st.columns([1, 4, 1])
                if r["thumbnail"]:
                    a.image(r["thumbnail"])
                b.markdown(
                    f"**[{r['title']}]({r['url']})**  \n{r['channel']} · {r['subscribers']:,} subs · "
                    f"**{r['views']:,} views** · ratio **{r['breakout_ratio']}×** · "
                    f"{r['format']} {fmt_seconds(r['duration_s'])} · {language_name(r['language'])}"
                    + (f"  \n_{r['style']}_" if r.get("style") else "")
                )
                if c.button("Add →", key=f"br_{r['video_id']}"):
                    add_to_analysis(r["channel_id"], r["channel"])

# ============================================================================= ④ analyse
with tab_an:
    refs_text = st.text_area(
        "Channels to analyse — one per line. Two or more are blended into one shared formula.",
        value="\n".join(ss.to_analyse), height=110,
        placeholder="@handle, channel URL, channel ID (UC…), or any video URL from the channel",
    )
    c1, c2, c3 = st.columns(3)
    n_videos = c1.slider("Recent videos per channel", 10, 100, 30, step=5)
    n_transcripts = c2.slider("Transcripts of top videos", 0, 6, 3,
                              help="Transcripts reveal the spoken sequence (hook → segments → CTA).")
    n_comments = c3.slider("Read comments of top videos", 0, 6, 3,
                           help="1 quota unit per 100 comments. Finds what viewers ask for.")
    refs = [r.strip() for r in refs_text.splitlines() if r.strip()]

    if st.button("Analyse", type="primary", disabled=not refs):
        yt = yt_client()
        if yt:
            analyses, errors = [], []
            prog = st.progress(0.0)
            for i, ref in enumerate(refs):
                try:
                    with st.spinner(f"Analysing {ref}…"):
                        analyses.append(analyze_channel(yt, ref, n_videos=n_videos, transcripts=n_transcripts,
                                                        comment_videos=n_comments, workdir=WORKDIR, tz=tz))
                except (YouTubeError, RuntimeError) as e:
                    errors.append(f"{ref}: {e}")
                prog.progress((i + 1) / len(refs))
            for e in errors:
                st.error(e)
            if analyses:
                ss.analysis = combine_analyses(analyses)
                ss.pop("conversation", None)
                ss.pop("master", None)
                storage.history_save("analysis", ss.analysis["channel"]["title"], ss.analysis)
                ss.to_analyse = refs

    an = ss.get("analysis")
    if an:
        ch = an["channel"]
        st.subheader(f"{ch['title']}" + (f"  ·  [{ch.get('handle') or 'open'}]({ch['url']})" if an.get("mode") != "blend" else ""))
        if an.get("mode") == "blend":
            st.dataframe(pd.DataFrame(an["sources"]), width="stretch", hide_index=True)
        m = st.columns(5)
        m[0].metric("Subscribers", f"{ch['subscribers']:,}" if ch.get("subscribers") else "—")
        m[1].metric("Uploads / week", an["cadence"]["videos_per_week"] or "—")
        m[2].metric("Median views", f"{int(an['performance']['median_views'] or 0):,}")
        m[3].metric("Median length (long)", an["formats"]["median_long_duration"] or "—")
        m[4].metric("Main language", next(iter(an["languages"]), "—"))

        a, b = st.columns(2)
        with a:
            tzname = an["cadence"].get("timezone") or "UTC"
            st.markdown("#### Upload rhythm")
            st.write(f"Best days: {', '.join(f'{d} ({n})' for d, n in an['cadence']['top_weekdays'])}")
            hours = an["cadence"].get("top_upload_hours") or an["cadence"].get("top_upload_hours_utc", [])
            st.write(f"Best hours ({tzname}): {', '.join(f'{h}:00 ({n})' for h, n in hours)}")
            st.write(f"Format mix: {an['formats']['mix']} · Shorts median: {an['formats']['median_short_duration'] or '—'}")
            st.markdown("#### Title pattern — winners vs rest")
            st.dataframe(pd.DataFrame({"Top 25%": an["titles"]["top_quartile"],
                                       "Others": an["titles"]["rest"]}),
                         width="stretch")
        with b:
            st.markdown("#### Top-performing videos")
            for v in an["top_videos"][:8]:
                who = f" ({v['channel']})" if an.get("mode") == "blend" else ""
                st.markdown(f"- [{v['title']}]({v['url']}){who} — {v['views_per_day']:,} views/day "
                            f"· {fmt_seconds(v['duration_s'])}")
            label = "shared by 2+ channels" if an.get("mode") == "blend" else "frequent"
            st.markdown(f"#### Title words ({label})")
            st.write(", ".join(w for w, _ in an["titles"]["frequent_words"]) or "—")
            st.markdown(f"#### Tags ({label})")
            st.write(", ".join(t for t, _ in an["tags"][:20]) or "—")

        if an["thumbnail_files"]:
            st.markdown("#### Thumbnails of the best videos")
            st.image([p for p in an["thumbnail_files"] if Path(p).exists()], width=220)

        aud = an.get("audience")
        if aud:
            st.markdown(f"#### 💬 What the audience wants ({aud['comments_read']} comments read)")
            insights = aud.get("insights") or {}
            if an.get("mode") == "blend":
                insights = {}
                for name, ins in (aud.get("insights_per_channel") or {}).items():
                    if ins and "error" not in ins:
                        for k2, v2 in ins.items():
                            if isinstance(v2, list):
                                insights.setdefault(k2, []).extend(v2)
            if insights and "error" not in insights:
                i1, i2 = st.columns(2)
                i1.markdown("**Requested topics**\n\n" + "\n".join(f"- {t}" for t in insights.get("requested_topics", [])[:10]))
                i1.markdown("**Unanswered questions**\n\n" + "\n".join(f"- {t}" for t in insights.get("unanswered_questions", [])[:8]))
                i2.markdown("**What viewers love**\n\n" + "\n".join(f"- {t}" for t in insights.get("what_viewers_love", [])[:8]))
                i2.markdown("**Complaints**\n\n" + "\n".join(f"- {t}" for t in insights.get("complaints", [])[:6]))
                if insights.get("audience_profile"):
                    st.caption("Audience: " + str(insights["audience_profile"]))
            elif insights.get("error"):
                st.caption(f"Comment insights unavailable: {insights['error']}")
            with st.expander(f"Viewer requests found by keyword ({len(aud['viewer_requests'])})"):
                for r in aud["viewer_requests"]:
                    st.markdown(f"- {r['text'][:300]}  _(👍 {r['likes']})_")

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
        st.info("Next: open tab ⑤ to add screenshots, a reference video and your brief.")

# ============================================================================= ⑤ master prompt
with tab_mp:
    an = ss.get("analysis")
    if not an:
        st.info("Analyse a channel in tab ④ first, load one from ⑥ History, or upload a saved analysis JSON.")
        up = st.file_uploader("Saved analysis JSON", type=["json"])
        if up:
            ss.analysis = json.load(up)
            st.rerun()
        if ss.get("master"):
            st.divider()
            st.markdown(ss.master)
            st.download_button("Download master prompt (.md)", ss.master, file_name="master_prompt.md")
    else:
        st.markdown(f"Building a master prompt from **{an['channel']['title']}**.")

        # ---- visual references
        st.markdown("#### 1. Visual references")
        st.caption("Thumbnails of the top videos are included automatically. For a precise match of the "
                   "on-screen sequence, add screenshots of key moments: the first frame / hook, the title "
                   "card, typical shots, captions/graphics, and the end screen.")
        uploads = st.file_uploader("Screenshots / extra thumbnails",
                                   type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        include_thumbs = st.checkbox("Include the auto-downloaded thumbnails", value=True)

        with st.expander("🎞️ Analyse a reference video file (shot cuts, pacing, keyframes)"):
            st.caption("Use a video you are allowed to use — your own, or one the creator shared with you. "
                       "The app does not download videos from YouTube.")
            vfile = st.file_uploader("Video file", type=["mp4", "mov", "mkv", "webm", "avi"])
            vpath = st.text_input("…or a path on this PC", placeholder=r"F:\Videos\reference.mp4")
            vmax = st.slider("Keyframes to extract", 6, 24, 14)
            if st.button("Analyse video", disabled=not (vfile or vpath)):
                src = Path(vpath.strip().strip('"')) if vpath else None
                if vfile:
                    src = WORKDIR / "videos" / Path(vfile.name).name
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_bytes(vfile.getbuffer())
                try:
                    with st.spinner("Detecting cuts and extracting keyframes…"):
                        ss.video_analysis = analyze_video_file(src, WORKDIR / "frames" / src.stem, max_frames=vmax)
                except Exception as e:
                    st.error(str(e))
            va = ss.get("video_analysis")
            if va:
                k = st.columns(4)
                k[0].metric("Duration", fmt_seconds(va["duration_s"]))
                k[1].metric("Cuts / minute", va["cuts_per_minute"] or "—")
                k[2].metric("Avg shot (first 30 s)", f"{va['hook_avg_shot_s']} s" if va["hook_avg_shot_s"] else "—")
                k[3].metric("Avg shot (rest)", f"{va['body_avg_shot_s']} s" if va["body_avg_shot_s"] else "—")
                st.image([f["path"] for f in va["frames"]], caption=[fmt_seconds(f["time_s"]) for f in va["frames"]],
                         width=160)
                ss.use_video = st.checkbox("Include this video analysis and its keyframes", value=True)

        # ---- brief
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
            "production_tools": c2.text_input("Tools you use",
                                              placeholder="e.g. ChatGPT, Midjourney, ElevenLabs, CapCut, Canva",
                                              help="A ready-to-use pack is written for each tool you list."),
            "notes": st.text_area("Anything else (what you like about the reference, constraints, budget…)"),
        }
        use_trend = st.checkbox("Include the trending summary for this language (from tab ①)",
                                value=bool(ss.get("trending_lang")), disabled=not ss.get("trending_lang"))

        missing = [k for k in ("language", "niche_or_topics", "production_tools") if not brief[k]]
        if missing:
            st.caption("Tip: fill in " + ", ".join(m.replace('_', ' ') for m in missing) + " for a more tailored result.")

        if st.button("Generate master prompt", type="primary"):
            up_dir = WORKDIR / "uploads" / an["channel"]["id"]
            up_dir.mkdir(parents=True, exist_ok=True)
            up_paths = []
            for f in uploads or []:
                p = up_dir / Path(f.name).name
                p.write_bytes(f.getbuffer())
                up_paths.append(p)
            va = ss.get("video_analysis") if ss.get("use_video") else None
            frame_paths = [f["path"] for f in va["frames"]] if va else []
            images = collect_images(*up_paths, *frame_paths,
                                    *(an["thumbnail_files"] if include_thumbs else []))
            trend = None
            if use_trend and ss.get("trending_lang"):
                trend = {k: v for k, v in ss.trending_lang.items() if k != "hottest_videos"}

            if ai.available():
                try:
                    with st.spinner(f"Claude is studying the data and {len(images)} image(s)… "
                                    "this can take a few minutes."):
                        ss.conversation, ss.master = start_conversation(an, images, brief, trend, va)
                    storage.history_save("prompt", f"{an['channel']['title']} → {brief['niche_or_topics'] or 'master prompt'}",
                                         {"markdown": ss.master, "brief": brief, "source": an["channel"]["title"]})
                except Exception as e:
                    claude_error(e)
            else:
                st.warning("No Anthropic key — generating the template version (no image analysis).")
                ss.master = generate_template(an, brief)
                ss.pop("conversation", None)
                storage.history_save("prompt", f"{an['channel']['title']} (template)",
                                     {"markdown": ss.master, "brief": brief, "source": an["channel"]["title"]})

        if ss.get("master"):
            st.divider()
            st.markdown(ss.master)
            st.download_button("Download master prompt (.md)", ss.master,
                               file_name=f"master_prompt_{an['channel']['id']}.md")

            if ss.get("conversation"):
                st.markdown("#### ✏️ Refine")
                st.caption("e.g. 'make the hook shorter', 'adapt everything for 60-second Shorts', "
                           "'write it for a Tamil audience', 'more humour'")
                instr = st.text_area("Your change", key="refine_text")
                if st.button("Apply change", disabled=not instr.strip()):
                    try:
                        with st.spinner("Claude is revising…"):
                            ss.conversation, ss.master = refine(ss.conversation, instr.strip())
                        storage.history_save("prompt", f"{an['channel']['title']} → refined: {instr.strip()[:50]}",
                                             {"markdown": ss.master, "source": an["channel"]["title"]})
                        st.rerun()
                    except Exception as e:
                        claude_error(e)
                n_edits = sum(1 for m in ss.conversation if m["role"] == "user") - 1
                if n_edits:
                    st.caption(f"{n_edits} refinement(s) applied — the document above is the latest version.")

# ============================================================================= ⑥ history
with tab_hi:
    kind_label = {"analysis": "Channel analyses", "prompt": "Master prompts", "ai_finder": "AI finder searches",
                  "breakouts": "Breakout searches"}
    kind = st.radio("Show", list(kind_label), format_func=kind_label.get, horizontal=True)
    items = storage.history_list(kind)
    if not items:
        st.info("Nothing saved yet — results are stored automatically when you run them.")
    else:
        choice = st.selectbox("Saved item", items, format_func=lambda it: f"{it['when']} — {it['title']}")
        body = storage.history_get(choice["id"])
        h1, h2 = st.columns([1, 5])
        if h2.button("🗑️ Delete", key=f"del_{choice['id']}"):
            storage.history_delete(choice["id"])
            st.rerun()
        if kind == "analysis":
            if h1.button("Load", type="primary"):
                ss.analysis = body
                ss.pop("conversation", None)
                ss.pop("master", None)
                st.success("Loaded — see tabs ④ and ⑤.")
            st.json(body, expanded=False)
        elif kind == "prompt":
            if h1.button("Load", type="primary"):
                ss.master = body["markdown"]
                ss.pop("conversation", None)
                st.success("Loaded into tab ⑤ (refinement needs a fresh generation).")
            st.markdown(body["markdown"])
        elif kind == "ai_finder":
            if h1.button("Load", type="primary"):
                ss.ai_found = body
                st.success("Loaded into tab ②.")
            st.dataframe(pd.DataFrame(body)[["title", "channel", "views_per_hour", "ai_verdict", "ai_niche", "url"]]
                         if body else pd.DataFrame(), width="stretch")
        else:
            if h1.button("Load", type="primary"):
                ss.breakouts = body
                st.success("Loaded into tab ③.")
            st.dataframe(pd.DataFrame(body)[["title", "channel", "subscribers", "views", "breakout_ratio", "url"]]
                         if body else pd.DataFrame(), width="stretch")
