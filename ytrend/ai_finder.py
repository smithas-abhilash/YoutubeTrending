"""Find AI-generated videos that are getting strong views per hour (VPH) right now.

YouTube's trending chart is dominated by studios, labels and big gamers, so AI
channels rarely appear there. Instead we search recent uploads with AI-video
keyword packs, rank them by VPH, and decide which are really AI-generated from
three signals: the uploader's synthetic-media disclosure, AI terms in the
metadata, and (optionally) Claude looking at the thumbnail.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from . import ai
from .trending import _video_row
from .youtube import YouTubeClient

# Each query costs 100 quota units per page of 50 results (cached for 6 hours).
KEYWORD_PACKS: dict[str, list[str]] = {
    "General AI video": ["ai generated video", "ai video", "made with ai", "ai animation"],
    "AI stories (Hindi / Indian)": ["ai kahani", "ai story hindi", "ai cartoon hindi", "moral story ai"],
    "AI Shorts: animals & babies": ["ai animals", "ai cat story", "ai baby", "ai dog rescue"],
    "AI history / what-if / POV": ["ai history", "what if ai", "pov you live in", "ai historical"],
    "AI horror & mystery": ["ai horror story", "ai scary", "ai mystery story"],
    "AI kids & cartoons": ["ai cartoon", "ai kids story", "ai nursery rhymes"],
    "AI music & songs": ["ai song", "ai music video", "suno ai song"],
    "AI tools named (Sora/Veo/Kling)": ["veo 3", "sora ai", "kling ai", "hailuo ai", "runway ai"],
}

# Words in title/tags/description that suggest AI-generated visuals.
_AI_TERMS = re.compile(
    r"(\bai\b|\ba\.i\.|artificial intelligence|ai[- ]generated|made with ai|#ai\w*|midjourney|"
    r"stable diffusion|\bflux\b|\bsora\b|\bveo ?\d?\b|\bkling\b|runway|\bpika\b|hailuo|minimax|"
    r"leonardo ai|dall-?e|\bsuno\b|\budio\b|elevenlabs|heygen|synthesia|invideo|pictory|"
    r"एआई|ए\.आई)",
    re.IGNORECASE,
)

AI_VERDICTS_SHOWN = ("ai_generated", "likely_ai")


def metadata_signals(row: dict) -> list[str]:
    """Reasons from metadata alone that point to AI-generated content."""
    reasons = []
    if row.get("synthetic_disclosure") is True:
        reasons.append("uploader disclosed synthetic media")
    text = " ".join([row["title"], row.get("description", ""), " ".join(row.get("tags", []))])
    hits = sorted({m.group(0).lower().strip() for m in _AI_TERMS.finditer(text)})
    if hits:
        reasons.append("AI terms: " + ", ".join(hits[:6]))
    return reasons


def discover(
    yt: YouTubeClient,
    queries: list[str],
    region: str | None,
    language: str | None,
    days: int = 3,
    duration: str | None = None,
    pages: int = 1,
    max_subscribers: int | None = None,
    progress=None,
) -> list[dict]:
    """Search each query for recent high-view videos and rank them by views per hour."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cats = yt.categories(region or "US")
    rows: dict[str, dict] = {}
    for i, q in enumerate(queries):
        for v in yt.search_videos(query=q, region=region, language=language, published_after=since,
                                  order="viewCount", limit=50 * pages, duration=duration):
            row = rows.setdefault(v["id"], {**_video_row(v, cats), "queries": []})
            row["queries"].append(q)
        if progress:
            progress((i + 1) / len(queries))
    out = list(rows.values())
    if not out:
        return []

    subs = {}
    for ch in yt.channels({r["channel_id"] for r in out}):
        st = ch.get("statistics", {})
        subs[ch["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    for r in out:
        r["subscribers"] = subs.get(r["channel_id"])
        r["breakout_ratio"] = round(r["views"] / max(r["subscribers"] or 0, 1000), 1)
        r["ai_signals"] = metadata_signals(r)
        # Until Claude looks at it, metadata decides.
        r["ai_verdict"] = "likely_ai" if r["ai_signals"] else "unclear"
        r["ai_confidence"] = 60 if r["ai_signals"] else 30
        r["ai_niche"] = None
        r["ai_reason"] = "; ".join(r["ai_signals"]) or "no AI terms in metadata"
    if max_subscribers:
        out = [r for r in out if r["subscribers"] is None or r["subscribers"] <= max_subscribers]
    out.sort(key=lambda r: -r["views_per_hour"])
    return out


def verify_with_claude(rows: list[dict], use_thumbnails: bool = True, limit: int = 120) -> None:
    """Replace the metadata guess with Claude's verdict for the top `limit` rows by VPH."""
    todo = rows[:limit]
    labels = ai.detect_ai_videos(todo, use_thumbnails=use_thumbnails)
    for r in todo:
        lab = labels.get(r["video_id"])
        if not lab:
            continue
        r["ai_verdict"] = lab["verdict"]
        r["ai_confidence"] = lab["confidence"]
        r["ai_niche"] = lab["niche"]
        r["ai_reason"] = lab["reason"]
        r["ai_checked"] = True


def niche_of(r: dict) -> str:
    if r.get("ai_niche"):
        return r["ai_niche"]
    return f"{r['queries'][0]} ({'Shorts' if r['format'] == 'short' else 'long'})"


def summarize_niches(rows: list[dict], min_videos: int = 1) -> list[dict]:
    """Group AI videos into niches, ranked by median views per hour."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["ai_verdict"] in AI_VERDICTS_SHOWN:
            groups[niche_of(r)].append(r)
    out = []
    for niche, vids in groups.items():
        if len(vids) < min_videos:
            continue
        best = max(vids, key=lambda v: v["views_per_hour"])
        subs = [v["subscribers"] for v in vids if v.get("subscribers") is not None]
        out.append({
            "niche": niche,
            "videos": len(vids),
            "channels": len({v["channel_id"] for v in vids}),
            "median_vph": round(median(v["views_per_hour"] for v in vids)),
            "best_vph": best["views_per_hour"],
            "shorts_pct": round(100 * sum(v["format"] == "short" for v in vids) / len(vids)),
            "median_subs": round(median(subs)) if subs else None,
            "best_video": best["title"],
            "best_url": best["url"],
        })
    out.sort(key=lambda n: (-n["median_vph"], -n["videos"]))
    return out
