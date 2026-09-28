"""Thin client for the YouTube Data API v3.

Only read-only, API-key endpoints are used. Get a key at
https://console.cloud.google.com/apis/credentials and enable
"YouTube Data API v3" for the project.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Iterator

import requests

from . import storage

API_BASE = "https://www.googleapis.com/youtube/v3"
DAILY_QUOTA = 10_000
# Quota cost per request (https://developers.google.com/youtube/v3/determine_quota_cost).
UNIT_COST = {"search": 100}
# How long cached responses stay fresh, in seconds. Trending moves fast; the rest barely changes.
CACHE_TTL = {"videos:mostPopular": 3600, "search": 6 * 3600, "commentThreads": 12 * 3600}
DEFAULT_TTL = 6 * 3600


class YouTubeError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None,
                 use_cache: bool = True):
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not self.api_key:
            raise YouTubeError(
                "YouTube API key missing. Enter it in the sidebar or set YOUTUBE_API_KEY."
            )
        self.session = session or requests.Session()
        self.use_cache = use_cache

    # ------------------------------------------------------------------ low level
    def _get(self, resource: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        cache_key = resource + "?" + json.dumps(params, sort_keys=True)
        ttl = CACHE_TTL.get(f"{resource}:{params.get('chart')}", CACHE_TTL.get(resource, DEFAULT_TTL))
        if self.use_cache:
            cached = storage.cache_get(cache_key, ttl)
            if cached is not None:
                return cached

        resp = self.session.get(f"{API_BASE}/{resource}", params={**params, "key": self.api_key}, timeout=30)
        storage.quota_add(resource, UNIT_COST.get(resource, 1))
        if resp.status_code != 200:
            try:
                msg = resp.json()["error"]["message"]
            except Exception:
                msg = resp.text[:300]
            raise YouTubeError(f"{resource}: HTTP {resp.status_code}: {msg}")
        body = resp.json()
        if self.use_cache:
            storage.cache_put(cache_key, body)
        return body

    def _paged(self, resource: str, limit: int, **params: Any) -> Iterator[dict]:
        fetched = 0
        token = None
        while fetched < limit:
            data = self._get(
                resource, pageToken=token, maxResults=min(50, limit - fetched), **params
            )
            items = data.get("items", [])
            for item in items:
                yield item
                fetched += 1
                if fetched >= limit:
                    return
            token = data.get("nextPageToken")
            if not token or not items:
                return

    # ------------------------------------------------------------------ endpoints
    def trending(self, region: str = "US", category_id: str | None = None, limit: int = 50) -> list[dict]:
        """Most popular videos for a region (YouTube's trending chart)."""
        return list(
            self._paged(
                "videos",
                limit,
                part="snippet,contentDetails,statistics,status",
                chart="mostPopular",
                regionCode=region,
                videoCategoryId=category_id,
            )
        )

    def categories(self, region: str = "US") -> dict[str, str]:
        data = self._get("videoCategories", part="snippet", regionCode=region)
        return {c["id"]: c["snippet"]["title"] for c in data.get("items", [])}

    def videos(self, ids: Iterable[str]) -> list[dict]:
        ids = list(ids)
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = self._get(
                "videos",
                part="snippet,contentDetails,statistics,status",
                id=",".join(ids[i : i + 50]),
            )
            out.extend(data.get("items", []))
        return out

    def channels(self, ids: Iterable[str]) -> list[dict]:
        ids = list(ids)
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = self._get(
                "channels",
                part="snippet,statistics,contentDetails,brandingSettings",
                id=",".join(ids[i : i + 50]),
            )
            out.extend(data.get("items", []))
        return out

    def resolve_channel_id(self, ref: str) -> str:
        """Accepts a channel ID, @handle, or a youtube.com channel/handle/video URL."""
        ref = ref.strip()
        m = re.search(r"(UC[0-9A-Za-z_-]{22})", ref)
        if m:
            return m.group(1)
        m = re.search(r"(?:youtube\.com/)?(@[\w.\-]+)", ref)
        if m:
            data = self._get("channels", part="id", forHandle=m.group(1))
            if data.get("items"):
                return data["items"][0]["id"]
            raise YouTubeError(f"No channel found for handle {m.group(1)}")
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([0-9A-Za-z_-]{11})", ref)
        if m:
            vids = self.videos([m.group(1)])
            if vids:
                return vids[0]["snippet"]["channelId"]
        data = self._get("search", part="snippet", q=ref, type="channel", maxResults=1)
        if data.get("items"):
            return data["items"][0]["snippet"]["channelId"]
        raise YouTubeError(f"Could not resolve channel: {ref}")

    def channel_uploads(self, channel_id: str, limit: int = 30) -> list[dict]:
        """Most recent uploads of a channel, with full video details."""
        ch = self.channels([channel_id])
        if not ch:
            raise YouTubeError(f"Channel not found: {channel_id}")
        uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ids = [
            it["contentDetails"]["videoId"]
            for it in self._paged("playlistItems", limit, part="contentDetails", playlistId=uploads)
        ]
        return self.videos(ids)

    def top_comments(self, video_id: str, limit: int = 100) -> list[dict]:
        """Most relevant top-level comments of a video (empty if comments are off)."""
        try:
            items = list(self._paged("commentThreads", limit, part="snippet", videoId=video_id,
                                     order="relevance", textFormat="plainText"))
        except YouTubeError:
            return []  # comments disabled / members-only
        out = []
        for it in items:
            c = it["snippet"]["topLevelComment"]["snippet"]
            out.append({"text": c.get("textDisplay", ""), "likes": int(c.get("likeCount", 0)),
                        "replies": int(it["snippet"].get("totalReplyCount", 0))})
        return out

    def search_videos(
        self,
        query: str | None = None,
        region: str | None = None,
        language: str | None = None,
        published_after: str | None = None,
        order: str = "viewCount",
        limit: int = 50,
        duration: str | None = None,
    ) -> list[dict]:
        """Search videos (costs 100 quota units per page — use sparingly)."""
        ids = [
            it["id"]["videoId"]
            for it in self._paged(
                "search",
                limit,
                part="id",
                type="video",
                q=query,
                regionCode=region,
                relevanceLanguage=language,
                publishedAfter=published_after,
                order=order,
                videoDuration=duration,  # "short" (<4 min), "medium" (4-20), "long" (>20)
            )
        ]
        return self.videos(ids)
