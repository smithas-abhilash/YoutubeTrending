"""Step 2: reverse-engineer a channel's content formula.

Collects the recent uploads of a channel and extracts the repeatable
"sequence" behind them: upload cadence, formats and lengths, title and
thumbnail patterns, chapter structure, the spoken structure of the best
videos (transcripts) and what the audience asks for (comments).
Several analyses can be blended into one shared formula.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from statistics import mean, median

import requests

from . import ai
from .trending import _best_thumb
from .utils import (
    detect_language,
    fmt_seconds,
    hours_since,
    language_name,
    parse_chapters,
    parse_duration,
    parse_time,
    title_features,
    to_local,
    words,
)
from .youtube import YouTubeClient

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Comments that ask for something: "please make a video on…", "next part", "can you…?"
_REQUEST_RE = re.compile(
    r"(please|pls|plz|make a video|next video|next part|part 2|can you|could you|do a video|"
    r"waiting for|request|kripya|bhai .* banao|video banao|\?)",
    re.IGNORECASE,
)


def _video(v: dict) -> dict:
    sn, st = v["snippet"], v.get("statistics", {})
    views = int(st.get("viewCount", 0))
    likes = int(st.get("likeCount", 0))
    comments = int(st.get("commentCount", 0))
    duration = parse_duration(v.get("contentDetails", {}).get("duration"))
    return {
        "video_id": v["id"],
        "title": sn["title"],
        "description": sn.get("description", ""),
        "published_at": sn["publishedAt"],
        "duration_s": duration,
        "format": "short" if 0 < duration <= 180 else "long",
        "views": views,
        "likes": likes,
        "comments": comments,
        "views_per_day": round(views / (hours_since(sn["publishedAt"]) / 24)),
        "engagement": round((likes + comments) / views, 4) if views else 0.0,
        "tags": sn.get("tags", []),
        "language": detect_language(sn),
        "thumbnail": _best_thumb(sn.get("thumbnails", {})),
        "chapters": parse_chapters(sn.get("description", "")),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    }


def fetch_transcript(video_id: str, languages: list[str] | None = None, max_chars: int = 8000) -> str | None:
    """Timestamped transcript condensed into ~20s blocks. Needs youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    langs = (languages or []) + ["en"]
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):  # < 1.0
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        else:  # >= 1.0
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=langs)
            segs = [{"start": s.start, "text": s.text} for s in fetched]
    except Exception:
        return None
    blocks: list[str] = []
    cur_start, cur_text = 0.0, []
    for s in segs:
        if s["start"] - cur_start >= 20 and cur_text:
            blocks.append(f"[{fmt_seconds(cur_start)}] {' '.join(cur_text)}")
            cur_start, cur_text = s["start"], []
        cur_text.append(s["text"].replace("\n", " "))
    if cur_text:
        blocks.append(f"[{fmt_seconds(cur_start)}] {' '.join(cur_text)}")
    text = "\n".join(blocks)
    if len(text) > max_chars:
        # Keep the opening (hook) and the ending (CTA); both matter most for structure.
        head = int(max_chars * 0.7)
        text = text[:head] + "\n[... middle omitted ...]\n" + text[-(max_chars - head):]
    return text


def download_thumbnails(videos: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for v in videos:
        if not v.get("thumbnail"):
            continue
        p = out_dir / f"thumb_{v['video_id']}.jpg"
        if not p.exists():
            try:
                r = requests.get(v["thumbnail"], timeout=30)
                r.raise_for_status()
                p.write_bytes(r.content)
            except requests.RequestException:
                continue
        paths.append(p)
    return paths


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def _title_profile(group: list[dict]) -> dict:
    feats = [title_features(v["title"]) for v in group]
    n = len(feats)
    return {
        "avg_chars": round(mean(f["chars"] for f in feats), 1),
        "avg_words": round(mean(f["words"] for f in feats), 1),
        "pct_emoji": _pct(sum(f["has_emoji"] for f in feats), n),
        "pct_number": _pct(sum(f["has_number"] for f in feats), n),
        "pct_question": _pct(sum(f["has_question"] for f in feats), n),
        "pct_exclaim": _pct(sum(f["has_exclaim"] for f in feats), n),
        "pct_brackets_or_pipe": _pct(sum(f["has_brackets"] for f in feats), n),
        "pct_hashtag": _pct(sum(f["has_hashtag"] for f in feats), n),
        "avg_caps_ratio": round(mean(f["caps_ratio"] for f in feats), 2),
    }


def _audience(yt: YouTubeClient, top: list[dict], n_videos: int) -> dict:
    comments = []
    for v in top[:n_videos]:
        for c in yt.top_comments(v["video_id"], limit=100):
            comments.append({"video": v["title"][:80], "text": c["text"][:400], "likes": c["likes"]})
    comments.sort(key=lambda c: -c["likes"])
    requests_ = [c for c in comments if _REQUEST_RE.search(c["text"])]
    insights = None
    if comments and ai.available():
        try:
            insights = ai.comment_insights(comments)
        except Exception as e:  # insights are a bonus; never fail the analysis for them
            insights = {"error": str(e)}
    return {
        "comments_read": len(comments),
        "top_comments": comments[:15],
        "viewer_requests": requests_[:25],
        "insights": insights,
    }


def analyze_channel(
    yt: YouTubeClient,
    channel_ref: str,
    n_videos: int = 30,
    transcripts: int = 3,
    comment_videos: int = 3,
    workdir: Path | None = None,
    tz: str = "Asia/Kolkata",
) -> dict:
    channel_id = yt.resolve_channel_id(channel_ref)
    ch = yt.channels([channel_id])[0]
    vids = sorted((_video(v) for v in yt.channel_uploads(channel_id, n_videos)),
                  key=lambda v: v["published_at"])
    if not vids:
        raise RuntimeError("Channel has no public uploads to analyse.")

    # --- cadence (in the viewer's time zone)
    times = [to_local(parse_time(v["published_at"]), tz) for v in vids]
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
    weekday = Counter(WEEKDAYS[t.weekday()] for t in times)
    hour = Counter(t.hour for t in times)

    # --- formats
    fmt = Counter(v["format"] for v in vids)
    long_vids = [v for v in vids if v["format"] == "long"]
    short_vids = [v for v in vids if v["format"] == "short"]
    long_med = median(v["duration_s"] for v in long_vids) if long_vids else None
    short_med = median(v["duration_s"] for v in short_vids) if short_vids else None

    # --- performance: what separates winners from the rest?
    by_perf = sorted(vids, key=lambda v: -v["views_per_day"])
    k = max(1, len(vids) // 4)
    top, rest = by_perf[:k], by_perf[k:] or by_perf

    title_words = Counter(w for v in vids for w in words(v["title"]))
    tag_counts = Counter(t.lower() for v in vids for t in v["tags"])
    desc_lines = Counter(
        line.strip() for v in vids for line in v["description"].splitlines() if len(line.strip()) > 15
    )
    boilerplate = [line for line, c in desc_lines.most_common(15) if c >= max(3, len(vids) // 3)]
    with_chapters = [v for v in vids if v["chapters"]]
    languages = Counter(v["language"] for v in vids)

    # --- deep dive on the best performers
    lang_pref = [languages.most_common(1)[0][0]]
    deep = []
    for v in top[: max(transcripts, 0)]:
        deep.append(
            {
                "video_id": v["video_id"],
                "title": v["title"],
                "duration": fmt_seconds(v["duration_s"]),
                "views": v["views"],
                "chapters": v["chapters"],
                "description_head": v["description"][:600],
                "transcript": fetch_transcript(v["video_id"], lang_pref),
            }
        )

    thumbs: list[str] = []
    if workdir:
        thumbs = [str(p) for p in download_thumbnails(top[:6], workdir / "thumbnails")]

    sn, st = ch["snippet"], ch.get("statistics", {})
    return {
        "channel": {
            "id": channel_id,
            "title": sn["title"],
            "handle": sn.get("customUrl"),
            "description": sn.get("description", "")[:1000],
            "country": sn.get("country"),
            "created": sn.get("publishedAt"),
            "subscribers": int(st.get("subscriberCount", 0)) if not st.get("hiddenSubscriberCount") else None,
            "total_views": int(st.get("viewCount", 0)),
            "total_videos": int(st.get("videoCount", 0)),
            "keywords": ch.get("brandingSettings", {}).get("channel", {}).get("keywords"),
            "url": f"https://www.youtube.com/channel/{channel_id}",
        },
        "sample_size": len(vids),
        "languages": {language_name(k2): v for k2, v in languages.most_common()},
        "cadence": {
            "timezone": tz,
            "videos_per_week": round(7 / median(gaps), 2) if gaps and median(gaps) > 0 else None,
            "median_gap_days": round(median(gaps), 2) if gaps else None,
            "top_weekdays": weekday.most_common(3),
            "top_upload_hours": hour.most_common(3),
        },
        "formats": {
            "mix": dict(fmt),
            "median_long_duration_s": long_med,
            "median_short_duration_s": short_med,
            "median_long_duration": fmt_seconds(long_med) if long_med else None,
            "median_short_duration": fmt_seconds(short_med) if short_med else None,
        },
        "performance": {
            "median_views": median(v["views"] for v in vids),
            "median_views_per_day": median(v["views_per_day"] for v in vids),
            "median_engagement": median(v["engagement"] for v in vids),
            "top_quartile_formats": dict(Counter(v["format"] for v in top)),
            "top_quartile_median_duration": fmt_seconds(median(v["duration_s"] for v in top)),
        },
        "titles": {
            "top_quartile": _title_profile(top),
            "rest": _title_profile(rest),
            "frequent_words": title_words.most_common(20),
            "examples_top": [v["title"] for v in top[:10]],
            "examples_recent": [v["title"] for v in vids[-10:]],
        },
        "tags": tag_counts.most_common(25),
        "description_boilerplate": boilerplate,
        "chapters": {
            "pct_with_chapters": _pct(len(with_chapters), len(vids)),
            "examples": [
                {"title": v["title"], "chapters": v["chapters"]} for v in with_chapters[-3:]
            ],
        },
        "top_videos": [
            {**{k2: v[k2] for k2 in ("title", "url", "views", "views_per_day", "duration_s", "format",
                                     "published_at")}, "channel": sn["title"]}
            for v in top[:10]
        ],
        "deep_dive": deep,
        "audience": _audience(yt, top, comment_videos) if comment_videos else None,
        "thumbnail_files": thumbs,
    }


# ---------------------------------------------------------------- 5. blend channels
def _merge_counts(pairs_lists: list[list], top: int) -> list:
    c: Counter = Counter()
    for pairs in pairs_lists:
        for key, n in pairs:
            c[key] += n
    return c.most_common(top)


def _shared_counts(pairs_lists: list[list], top: int) -> list:
    """Items that appear in at least two channels, ranked by how many channels use them."""
    presence: Counter = Counter()
    for pairs in pairs_lists:
        presence.update({key for key, _ in pairs})
    min_channels = 2 if len(pairs_lists) > 1 else 1
    return [(k, n) for k, n in presence.most_common() if n >= min_channels][:top]


def _med(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def combine_analyses(analyses: list[dict]) -> dict:
    """Blend several channel analyses into one analysis with the same shape."""
    if len(analyses) == 1:
        return analyses[0]
    names = [a["channel"]["title"] for a in analyses]
    per = max(2, 10 // len(analyses))

    langs: Counter = Counter()
    for a in analyses:
        langs.update(a["languages"])
    mix: Counter = Counter()
    for a in analyses:
        mix.update(a["formats"]["mix"])
    long_med = _med([a["formats"].get("median_long_duration_s") for a in analyses])
    short_med = _med([a["formats"].get("median_short_duration_s") for a in analyses])

    def avg_profile(key: str) -> dict:
        profiles = [a["titles"][key] for a in analyses]
        return {f: round(mean(p[f] for p in profiles), 2) for f in profiles[0]}

    audiences = [a["audience"] for a in analyses if a.get("audience")]
    audience = None
    if audiences:
        comments = sorted((c for au in audiences for c in au["top_comments"]), key=lambda c: -c["likes"])
        requests_ = [r for au in audiences for r in au["viewer_requests"]]
        audience = {
            "comments_read": sum(au["comments_read"] for au in audiences),
            "top_comments": comments[:20],
            "viewer_requests": requests_[:40],
            "insights_per_channel": {a["channel"]["title"]: a["audience"].get("insights")
                                     for a in analyses if a.get("audience")},
        }

    return {
        "mode": "blend",
        "channel": {
            "id": "blend_" + "_".join(a["channel"]["id"][-6:] for a in analyses),
            "title": "Blend: " + " + ".join(names),
            "handle": None,
            "description": "",
            "subscribers": None,
            "url": analyses[0]["channel"]["url"],
        },
        "sources": [
            {"title": a["channel"]["title"], "url": a["channel"]["url"],
             "subscribers": a["channel"].get("subscribers"), "sample_size": a["sample_size"],
             "videos_per_week": a["cadence"]["videos_per_week"], "format_mix": a["formats"]["mix"]}
            for a in analyses
        ],
        "sample_size": sum(a["sample_size"] for a in analyses),
        "languages": dict(langs.most_common()),
        "cadence": {
            "timezone": analyses[0]["cadence"].get("timezone"),
            "videos_per_week": _med([a["cadence"]["videos_per_week"] for a in analyses]),
            "median_gap_days": _med([a["cadence"]["median_gap_days"] for a in analyses]),
            "top_weekdays": _merge_counts([a["cadence"]["top_weekdays"] for a in analyses], 3),
            "top_upload_hours": _merge_counts(
                [a["cadence"].get("top_upload_hours") or a["cadence"].get("top_upload_hours_utc", [])
                 for a in analyses], 3),
        },
        "formats": {
            "mix": dict(mix),
            "median_long_duration_s": long_med,
            "median_short_duration_s": short_med,
            "median_long_duration": fmt_seconds(long_med) if long_med else None,
            "median_short_duration": fmt_seconds(short_med) if short_med else None,
        },
        "performance": {
            "median_views": _med([a["performance"]["median_views"] for a in analyses]),
            "median_views_per_day": _med([a["performance"]["median_views_per_day"] for a in analyses]),
            "median_engagement": _med([a["performance"]["median_engagement"] for a in analyses]),
        },
        "titles": {
            "top_quartile": avg_profile("top_quartile"),
            "rest": avg_profile("rest"),
            "frequent_words": _shared_counts([a["titles"]["frequent_words"] for a in analyses], 20),
            "examples_top": [t for a in analyses for t in a["titles"]["examples_top"][:per]],
            "examples_recent": [t for a in analyses for t in a["titles"]["examples_recent"][:per]],
        },
        "tags": _shared_counts([a["tags"] for a in analyses], 25),
        "description_boilerplate": [],
        "chapters": {
            "pct_with_chapters": round(mean(a["chapters"]["pct_with_chapters"] for a in analyses), 1),
            "examples": [ex for a in analyses for ex in a["chapters"]["examples"][-1:]],
        },
        "top_videos": sorted((v for a in analyses for v in a["top_videos"][:per]),
                             key=lambda v: -v["views_per_day"]),
        "deep_dive": [d for a in analyses for d in a["deep_dive"][:2]],
        "audience": audience,
        "thumbnail_files": [t for a in analyses for t in a["thumbnail_files"][:3]],
    }
