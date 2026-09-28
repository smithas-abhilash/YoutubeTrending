"""Thin client for the YouTube Data API v3.

Only read-only, API-key endpoints are used. Get a key at
https://console.cloud.google.com/apis/credentials and enable
"YouTube Data API v3" for the project.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Iterator

import requests

API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not self.api_key:
            raise YouTubeError(
                "YouTube API key missing. Set YOUTUBE_API_KEY or pass --youtube-key."
            )
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ low level
    def _get(self, resource: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self.api_key
        resp = self.session.get(f"{API_BASE}/{resource}", params=params, timeout=30)
        if resp.status_code != 200:
            try:
                msg = resp.json()["error"]["message"]
            except Exception:
                msg = resp.text[:300]
            raise YouTubeError(f"{resource}: HTTP {resp.status_code}: {msg}")
        return resp.json()

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
                part="snippet,contentDetails,statistics",
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
                part="snippet,contentDetails,statistics",
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

    def search_videos(
        self,
        query: str | None = None,
        region: str | None = None,
        language: str | None = None,
        published_after: str | None = None,
        order: str = "viewCount",
        limit: int = 50,
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
            )
        ]
        return self.videos(ids)
