"""Find AI-generated videos (mostly Shorts) that are getting strong views per hour.

YouTube's trending chart is dominated by studios, labels and big gamers and
barely includes Shorts, so AI channels rarely appear there. Three ways in:

- keyword search: hashtag-style AI queries over recent uploads,
- seed expansion: read the hashtags/title words of channels you like and search
  with those, which finds their competitors,
- watchlist: re-check the latest uploads of channels you saved (cheap on quota).

Each video is judged AI or not from the uploader's synthetic-media disclosure,
AI terms in the metadata, and optionally Claude looking at the thumbnail.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from . import ai
from .trending import _video_row
from .utils import hours_since, words
from .youtube import YouTubeClient

# Each query costs 100 quota units per page of 50 results (cached for 6 hours).
# Hashtag-style queries match how AI Shorts are actually titled.
KEYWORD_PACKS: dict[str, list[str]] = {
    "AI Shorts (general)": ["#aishorts", "#aivideo", "#aigenerated", "ai shorts"],
    "AI stories (Hindi / Indian)": ["#aikahani", "ai kahani", "ai story hindi", "ai cartoon kahani"],
    "AI animals & babies": ["#aianimals", "#aicat", "ai baby", "ai dog rescue"],
    "AI devotional / mythology": ["ai bhakti", "ai mahabharat", "ai ramayan", "ai hanuman"],
    "AI history / POV / what-if": ["ai pov", "#aihistory", "what if ai"],
    "AI horror & mystery": ["#aihorror", "ai horror story", "ai bhoot kahani"],
    "AI kids & cartoons": ["#aicartoon", "ai cartoon", "ai kids story"],
    "AI music & songs": ["ai song", "#aimusic", "#suno"],
}

# Words in title/tags/description that suggest AI-generated visuals.
_AI_TERMS = re.compile(
    r"(\bai\b|\ba\.i\.|artificial intelligence|ai[- ]generated|made with ai|#ai\w*|midjourney|"
    r"stable diffusion|\bflux\b|\bsora\b|\bveo ?\d?\b|\bkling\b|runway|\bpika\b|hailuo|minimax|"
    r"leonardo ai|dall-?e|\bsuno\b|\budio\b|elevenlabs|heygen|synthesia|invideo|pictory|"
    r"एआई|ए\.आई)",
    re.IGNORECASE,
)

# Videos *about* making AI videos (tutorials, tool reviews) rather than AI-made content.
_TUTORIAL = re.compile(
    r"(how to|tutorial|kaise bana|kaise banaye|kaise banate|banana sikh|step[- ]by[- ]step|"
    r"free ai tool|ai tools?\b.*(free|best|top)|best ai|prompt|course|full guide|earn money|"
    r"paise kama|monetiz|कैसे बनाएं|कैसे बनाये|सीखें)",
    re.IGNORECASE,
)

AI_VERDICTS_SHOWN = ("ai_generated", "likely_ai")
DURATIONS = {"Shorts (< 4 min)": "short", "Any": None, "Medium (4-20 min)": "medium", "Long (> 20 min)": "long"}


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


def is_tutorial(row: dict) -> bool:
    return bool(_TUTORIAL.search(row["title"] + " " + row.get("description", "")[:150]))


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enrich(yt: YouTubeClient, rows: list[dict]) -> None:
    """Add subscribers, breakout ratio and the metadata-based AI guess."""
    missing = {r["channel_id"] for r in rows if "subscribers" not in r}
    subs = {}
    if missing:
        for ch in yt.channels(missing):
            st = ch.get("statistics", {})
            subs[ch["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    for r in rows:
        if "subscribers" not in r:
            r["subscribers"] = subs.get(r["channel_id"])
        r["breakout_ratio"] = round(r["views"] / max(r["subscribers"] or 0, 1000), 1)
        r["ai_signals"] = metadata_signals(r)
        r["tutorial"] = is_tutorial(r)
        # Until Claude looks at it, metadata decides.
        r["ai_verdict"] = "likely_ai" if r["ai_signals"] else "unclear"
        r["ai_confidence"] = 60 if r["ai_signals"] else 30
        r["ai_niche"] = None
        r["ai_reason"] = "; ".join(r["ai_signals"]) or "no AI terms in metadata"


def _filter(rows: list[dict], funnel: dict, min_vph: int, max_subscribers: int | None,
            exclude_tutorials: bool) -> list[dict]:
    if exclude_tutorials:
        rows = [r for r in rows if not r["tutorial"]]
        funnel["after removing tutorials"] = len(rows)
    if max_subscribers:
        rows = [r for r in rows if r["subscribers"] is None or r["subscribers"] <= max_subscribers]
        funnel[f"channels ≤ {max_subscribers:,} subs"] = len(rows)
    rows = [r for r in rows if r["views_per_hour"] >= min_vph]
    funnel[f"≥ {min_vph:,} views/hour"] = len(rows)
    rows.sort(key=lambda r: -r["views_per_hour"])
    return rows


def discover(
    yt: YouTubeClient,
    queries: list[str],
    region: str | None,
    language: str | None,
    days: int = 3,
    duration: str | None = "short",
    pages: int = 1,
    max_subscribers: int | None = None,
    min_vph: int = 0,
    exclude_tutorials: bool = True,
    progress=None,
) -> tuple[list[dict], dict]:
    """Search each query for recent high-view videos. Returns (rows by VPH, funnel counts)."""
    cats = yt.categories(region or "US")
    rows: dict[str, dict] = {}
    raw = 0
    for i, q in enumerate(queries):
        for v in yt.search_videos(query=q, region=region, language=language, published_after=_since(days),
                                  order="viewCount", limit=50 * pages, duration=duration):
            raw += 1
            row = rows.setdefault(v["id"], {**_video_row(v, cats), "queries": []})
            row["queries"].append(q)
        if progress:
            progress((i + 1) / len(queries))
    funnel = {"search results": raw, "unique videos": len(rows)}
    out = list(rows.values())
    if not out:
        return [], funnel
    _enrich(yt, out)
    return _filter(out, funnel, min_vph, max_subscribers, exclude_tutorials), funnel


# ---------------------------------------------------------------- seed expansion
_HASHTAG = re.compile(r"#[^\s#]{3,30}", re.UNICODE)
_GENERIC_TAGS = {"#shorts", "#short", "#viral", "#trending", "#youtubeshorts", "#ytshorts", "#reels",
                 "#fyp", "#foryou", "#explore", "#subscribe", "#shortsfeed", "#viralshorts",
                 "#trendingshorts", "#shortvideo", "#youtube"}


def seed_queries(yt: YouTubeClient, seed_refs: list[str], per_seed: int = 4) -> tuple[list[str], list[dict]]:
    """Build search queries from what seed channels actually put in their titles."""
    queries: list[str] = []
    seeds = []
    for ref in seed_refs:
        cid = yt.resolve_channel_id(ref)
        vids = yt.channel_uploads(cid, 25)
        if not vids:
            continue
        title = vids[0]["snippet"]["channelTitle"]
        seeds.append({"channel_id": cid, "channel": title})
        tags: Counter = Counter()
        wordc: Counter = Counter()
        for v in vids:
            sn = v["snippet"]
            tags.update(t.lower() for t in _HASHTAG.findall(sn["title"] + " " + sn.get("description", "")[:300]))
            wordc.update(set(words(sn["title"])))
        picked = [t for t, _ in tags.most_common(20) if t not in _GENERIC_TAGS][: per_seed - 1]
        # Plus the two most common title words as a phrase, e.g. "ai kahani" or "cat rescue".
        top_words = [w for w, _ in wordc.most_common(2)]
        if top_words:
            picked.append(" ".join(top_words))
        queries += picked
    return list(dict.fromkeys(queries)), seeds


# ---------------------------------------------------------------- watchlist scan
def scan_channels(
    yt: YouTubeClient,
    channel_ids: list[str],
    days: int = 7,
    per_channel: int = 15,
    min_vph: int = 0,
    progress=None,
) -> tuple[list[dict], dict]:
    """Latest uploads of the given channels, ranked by views per hour (~3 units per channel)."""
    cats = yt.categories("US")
    out = []
    for i, cid in enumerate(channel_ids):
        try:
            vids = yt.channel_uploads(cid, per_channel)
        except Exception:
            continue
        for v in vids:
            if hours_since(v["snippet"]["publishedAt"]) <= days * 24:
                out.append({**_video_row(v, cats), "queries": ["watchlist"]})
        if progress:
            progress((i + 1) / len(channel_ids))
    funnel = {f"uploads in last {days} days": len(out)}
    if not out:
        return [], funnel
    _enrich(yt, out)
    return _filter(out, funnel, min_vph, None, exclude_tutorials=False), funnel


# ---------------------------------------------------------------- Claude + niches
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


def summarize_niches(rows: list[dict], include_unclear: bool = False) -> list[dict]:
    """Group AI videos into niches, ranked by median views per hour."""
    allowed = AI_VERDICTS_SHOWN + (("unclear",) if include_unclear else ())
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["ai_verdict"] in allowed:
            groups[niche_of(r)].append(r)
    out = []
    for niche, vids in groups.items():
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
