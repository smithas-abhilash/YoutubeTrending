"""Step 1: find which *types* of channels are trending, grouped by language."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from . import ai
from .utils import (
    detect_language,
    hours_since,
    language_from_metadata,
    language_name,
    parse_duration,
    words,
)
from .youtube import YouTubeClient


def _best_thumb(thumbs: dict) -> str | None:
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in thumbs:
            return thumbs[k]["url"]
    return None


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
        "description": sn.get("description", "")[:200],
        "channel_id": sn["channelId"],
        "channel": sn["channelTitle"],
        "category": categories.get(sn.get("categoryId", ""), sn.get("categoryId", "?")),
        "language": detect_language(sn),
        "lang_source": "metadata" if language_from_metadata(sn) else "script",
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


def collect_trending(
    yt: YouTubeClient,
    regions: list[str],
    per_region: int = 50,
    category_id: str | None = None,
) -> list[dict]:
    rows: dict[str, dict] = {}
    for region in regions:
        cats = yt.categories(region)
        for v in yt.trending(region, category_id=category_id, limit=per_region):
            row = rows.setdefault(v["id"], {**_video_row(v, cats), "regions": []})
            row["regions"].append(region)
    return list(rows.values())


def filter_language(rows: list[dict], language: str | None) -> list[dict]:
    return [r for r in rows if r["language"] == language.lower()] if language else rows


# ---------------------------------------------------------------- AI enrichment
def refine_languages(rows: list[dict]) -> int:
    """Ask Claude for the language of videos that had no language metadata. Returns #changed."""
    todo = [r for r in rows if r.get("lang_source") == "script"]
    if not todo:
        return 0
    labels = ai.refine_languages(
        [{"video_id": r["video_id"], "title": r["title"], "description": r["description"]} for r in todo]
    )
    changed = 0
    for r in todo:
        new = labels.get(r["video_id"])
        if new and new != r["language"]:
            r["language"] = new
            changed += 1
        r["lang_source"] = "claude"
    return changed


def classify_styles(rows: list[dict]) -> None:
    """Attach style / presenter / is_big_media labels to every row (by channel)."""
    by_channel: dict[str, dict] = {}
    for r in rows:
        c = by_channel.setdefault(r["channel_id"], {
            "channel_id": r["channel_id"], "channel": r["channel"], "category": r["category"],
            "titles": [], "tags": set(),
        })
        if len(c["titles"]) < 5:
            c["titles"].append(r["title"])
        c["tags"].update(r["tags"][:8])
    payload = [{**c, "tags": sorted(c["tags"])[:20]} for c in by_channel.values()]
    labels = ai.classify_channel_styles(payload)
    for r in rows:
        lab = labels.get(r["channel_id"], {})
        r["style"] = lab.get("style", r["category"])
        r["presenter"] = lab.get("presenter", "unknown")
        r["is_big_media"] = lab.get("is_big_media", False)


# ---------------------------------------------------------------- summary
def summarize(rows: list[dict], top: int = 10, exclude_big_media: bool = False) -> dict:
    """Aggregate trending videos into language / category / channel-type insights."""
    if exclude_big_media:
        rows = [r for r in rows if not r.get("is_big_media")]
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_lang[r["language"]].append(r)

    languages = []
    for lang, vids in sorted(by_lang.items(), key=lambda kv: -len(kv[1])):
        cat_counts = Counter(v["category"] for v in vids)
        style_counts = Counter(v["style"] for v in vids if v.get("style"))
        presenter_counts = Counter(v["presenter"] for v in vids if v.get("presenter"))
        fmt_counts = Counter(v["format"] for v in vids)
        channels: dict[str, dict] = {}
        for v in vids:
            c = channels.setdefault(
                v["channel_id"],
                {"channel_id": v["channel_id"], "channel": v["channel"], "videos": 0,
                 "total_views": 0, "best_views_per_hour": 0, "categories": Counter(),
                 "style": v.get("style"), "is_big_media": v.get("is_big_media", False)},
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
                "top_styles": style_counts.most_common(8),
                "presenters": presenter_counts.most_common(),
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
        "overall_styles": Counter(r["style"] for r in rows if r.get("style")).most_common(10),
        "languages": languages,
    }


# ---------------------------------------------------------------- 1. breakout finder
def find_breakouts(
    yt: YouTubeClient,
    region: str | None,
    language: str | None,
    days: int = 7,
    query: str | None = None,
    pages: int = 1,
    max_subscribers: int = 500_000,
) -> list[dict]:
    """Recent videos that got far more views than their channel's size would predict.

    Costs 100 quota units per page of 50 search results (+ a few units for details).
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    videos = yt.search_videos(query=query, region=region, language=language,
                              published_after=since, order="viewCount", limit=50 * pages)
    if not videos:
        return []
    cats = yt.categories(region or "US")
    rows = [_video_row(v, cats) for v in videos]
    subs = {}
    for ch in yt.channels({r["channel_id"] for r in rows}):
        st = ch.get("statistics", {})
        subs[ch["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))

    out = []
    for r in rows:
        s = subs.get(r["channel_id"])
        if s is None or s > max_subscribers:
            continue
        r["subscribers"] = s
        # views per subscriber; floor subs at 1k so brand-new channels don't dominate on noise
        r["breakout_ratio"] = round(r["views"] / max(s, 1000), 1)
        out.append(r)
    out.sort(key=lambda r: -r["breakout_ratio"])
    return out


# ---------------------------------------------------------------- 3. trends over time
def trend_counts(snapshots: list[dict], by: str = "category", language: str | None = None) -> dict:
    """{day: {label: share_of_videos}} for charting how content types rise and fade."""
    table: dict[str, dict[str, float]] = {}
    for snap in snapshots:
        rows = [r for r in snap["rows"] if not language or r.get("language") == language]
        if not rows:
            continue
        counts = Counter((r.get(by) or "—") for r in rows)
        table[snap["day"]] = {k: round(100 * v / len(rows), 1) for k, v in counts.items()}
    return table


def rising(table: dict[str, dict[str, float]], top: int = 5) -> dict[str, list]:
    """Compare the first and last day in the table."""
    days = sorted(table)
    if len(days) < 2:
        return {"rising": [], "fading": []}
    first, last = table[days[0]], table[days[-1]]
    delta = {k: round(last.get(k, 0) - first.get(k, 0), 1) for k in set(first) | set(last)}
    ordered = sorted(delta.items(), key=lambda kv: kv[1])
    return {"rising": [kv for kv in reversed(ordered) if kv[1] > 0][:top],
            "fading": [kv for kv in ordered if kv[1] < 0][:top]}
