"""Step 2: reverse-engineer a channel's content formula.

Collects the recent uploads of a channel and extracts the repeatable
"sequence" behind them: upload cadence, formats and lengths, title and
thumbnail patterns, chapter structure, and (optionally) the spoken
structure of the best videos from their transcripts.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import mean, median

import requests

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
    words,
)
from .youtube import YouTubeClient

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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


def analyze_channel(
    yt: YouTubeClient,
    channel_ref: str,
    n_videos: int = 30,
    transcripts: int = 3,
    workdir: Path | None = None,
) -> dict:
    channel_id = yt.resolve_channel_id(channel_ref)
    ch = yt.channels([channel_id])[0]
    vids = sorted((_video(v) for v in yt.channel_uploads(channel_id, n_videos)),
                  key=lambda v: v["published_at"])
    if not vids:
        raise RuntimeError("Channel has no public uploads to analyse.")

    # --- cadence
    times = [parse_time(v["published_at"]) for v in vids]
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
    weekday = Counter(WEEKDAYS[t.weekday()] for t in times)
    hour = Counter(t.hour for t in times)

    # --- formats
    fmt = Counter(v["format"] for v in vids)
    long_vids = [v for v in vids if v["format"] == "long"]
    short_vids = [v for v in vids if v["format"] == "short"]

    # --- performance: what separates winners from the rest?
    by_perf = sorted(vids, key=lambda v: -v["views_per_day"])
    k = max(1, len(vids) // 4)
    top, rest = by_perf[:k], by_perf[k:] or by_perf

    def title_profile(group: list[dict]) -> dict:
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
        "languages": {language_name(k): v for k, v in languages.most_common()},
        "cadence": {
            "videos_per_week": round(7 / median(gaps), 2) if gaps and median(gaps) > 0 else None,
            "median_gap_days": round(median(gaps), 2) if gaps else None,
            "top_weekdays": weekday.most_common(3),
            "top_upload_hours_utc": hour.most_common(3),
        },
        "formats": {
            "mix": dict(fmt),
            "median_long_duration": fmt_seconds(median(v["duration_s"] for v in long_vids)) if long_vids else None,
            "median_short_duration": fmt_seconds(median(v["duration_s"] for v in short_vids)) if short_vids else None,
        },
        "performance": {
            "median_views": median(v["views"] for v in vids),
            "median_views_per_day": median(v["views_per_day"] for v in vids),
            "median_engagement": median(v["engagement"] for v in vids),
            "top_quartile_formats": dict(Counter(v["format"] for v in top)),
            "top_quartile_median_duration": fmt_seconds(median(v["duration_s"] for v in top)),
        },
        "titles": {
            "top_quartile": title_profile(top),
            "rest": title_profile(rest),
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
            {k2: v[k2] for k2 in ("title", "url", "views", "views_per_day", "duration_s", "format", "published_at")}
            for v in top[:10]
        ],
        "deep_dive": deep,
        "thumbnail_files": thumbs,
    }
