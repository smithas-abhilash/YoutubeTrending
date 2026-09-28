"""Step 1: find which *types* of channels are trending, grouped by language."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from .utils import detect_language, hours_since, language_name, parse_duration, words
from .youtube import YouTubeClient


def _video_row(v: dict, categories: dict[str, str]) -> dict:
    sn, st = v["snippet"], v.get("statistics", {})
    views = int(st.get("viewCount", 0))
    likes = int(st.get("likeCount", 0))
    comments = int(st.get("commentCount", 0))
    duration = parse_duration(v.get("contentDetails", {}).get("duration"))
    age_h = hours_since(sn["publishedAt"])
    return {
        "video_id": v["id"],
        "title": sn["title"],
        "channel_id": sn["channelId"],
        "channel": sn["channelTitle"],
        "category": categories.get(sn.get("categoryId", ""), sn.get("categoryId", "?")),
        "language": detect_language(sn),
        "published_at": sn["publishedAt"],
        "duration_s": duration,
        "format": "short" if 0 < duration <= 180 else "long",
        "views": views,
        "views_per_hour": round(views / age_h),
        "engagement": round((likes + comments) / views, 4) if views else 0.0,
        "tags": sn.get("tags", [])[:15],
        "thumbnail": _best_thumb(sn.get("thumbnails", {})),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    }


def _best_thumb(thumbs: dict) -> str | None:
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in thumbs:
            return thumbs[k]["url"]
    return None


def collect_trending(
    yt: YouTubeClient,
    regions: list[str],
    per_region: int = 50,
    category_id: str | None = None,
    language: str | None = None,
) -> list[dict]:
    rows: dict[str, dict] = {}
    for region in regions:
        cats = yt.categories(region)
        for v in yt.trending(region, category_id=category_id, limit=per_region):
            row = rows.setdefault(v["id"], {**_video_row(v, cats), "regions": []})
            row["regions"].append(region)
    out = list(rows.values())
    if language:
        out = [r for r in out if r["language"] == language.lower()]
    return out


def summarize(rows: list[dict], top: int = 10) -> dict:
    """Aggregate trending videos into language / category / channel-type insights."""
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_lang[r["language"]].append(r)

    languages = []
    for lang, vids in sorted(by_lang.items(), key=lambda kv: -len(kv[1])):
        cat_counts = Counter(v["category"] for v in vids)
        fmt_counts = Counter(v["format"] for v in vids)
        channels: dict[str, dict] = {}
        for v in vids:
            c = channels.setdefault(
                v["channel_id"],
                {"channel_id": v["channel_id"], "channel": v["channel"], "videos": 0,
                 "total_views": 0, "best_views_per_hour": 0, "categories": Counter()},
            )
            c["videos"] += 1
            c["total_views"] += v["views"]
            c["best_views_per_hour"] = max(c["best_views_per_hour"], v["views_per_hour"])
            c["categories"][v["category"]] += 1
        top_channels = sorted(
            channels.values(), key=lambda c: (c["videos"], c["best_views_per_hour"]), reverse=True
        )[:top]
        for c in top_channels:
            c["main_category"] = c.pop("categories").most_common(1)[0][0]
        keyword_counts = Counter(w for v in vids for w in words(v["title"]) + [t.lower() for t in v["tags"]])
        languages.append(
            {
                "language": lang,
                "language_name": language_name(lang),
                "trending_videos": len(vids),
                "share": round(len(vids) / len(rows), 3) if rows else 0,
                "top_categories": cat_counts.most_common(5),
                "format_mix": dict(fmt_counts),
                "median_duration_s": median(v["duration_s"] for v in vids),
                "median_views_per_hour": median(v["views_per_hour"] for v in vids),
                "top_keywords": keyword_counts.most_common(15),
                "top_channels": top_channels,
                "hottest_videos": sorted(vids, key=lambda v: -v["views_per_hour"])[:5],
            }
        )

    return {
        "total_videos": len(rows),
        "overall_categories": Counter(r["category"] for r in rows).most_common(10),
        "languages": languages,
    }
