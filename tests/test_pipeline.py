"""Offline tests: a fake YouTube API drives the whole pipeline (no keys, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ytrend import ai, storage, trending, utils
from ytrend.channel import analyze_channel, combine_analyses
from ytrend.master_prompt import TOOL_GUIDES, generate_template, tool_pack_spec
from ytrend.youtube import YouTubeClient

NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _video(vid, channel_id, title, hours_ago, views, duration="PT8M30S", lang=None, desc="", tags=None):
    sn = {"title": title, "description": desc, "channelId": channel_id, "channelTitle": f"Chan {channel_id}",
          "publishedAt": _iso(hours_ago), "categoryId": "24", "tags": tags or ["story", "horror"],
          "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}}}
    if lang:
        sn["defaultAudioLanguage"] = lang
    return {"id": vid, "snippet": sn, "contentDetails": {"duration": duration},
            "statistics": {"viewCount": str(views), "likeCount": str(views // 20), "commentCount": str(views // 200)}}


CHAPTERS = "0:00 Hook\n0:15 Setup\n2:30 Story\n7:40 Twist\n8:10 Outro"
VIDEOS = {
    "v1": _video("v1", "UCaaaaaaaaaaaaaaaaaaaaaa", "सबसे डरावनी कहानी | Real Horror Story", 20, 900_000, desc=CHAPTERS),
    "v2": _video("v2", "UCaaaaaaaaaaaaaaaaaaaaaa", "3 भूतिया गांव 😱 #horror", 50, 400_000, duration="PT55S"),
    "v3": _video("v3", "UCbbbbbbbbbbbbbbbbbbbbbb", "How I Built a $1M App?", 30, 250_000, lang="en"),
    "v4": _video("v4", "UCbbbbbbbbbbbbbbbbbbbbbb", "Budget phone review", 200, 50_000, lang="en-US"),
    "v5": _video("v5", "UCaaaaaaaaaaaaaaaaaaaaaa", "Haunted highway ki kahani", 400, 120_000, desc=CHAPTERS),
    "v6": _video("v6", "UCaaaaaaaaaaaaaaaaaaaaaa", "Raat 3 baje", 600, 80_000),
}
CHANNELS = {
    "UCaaaaaaaaaaaaaaaaaaaaaa": {"subscriberCount": "40000"},
    "UCbbbbbbbbbbbbbbbbbbbbbb": {"subscriberCount": "2000000"},
}


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        res = url.rsplit("/", 1)[-1]
        self.calls.append(res)
        if res == "videoCategories":
            return FakeResp({"items": [{"id": "24", "snippet": {"title": "Entertainment"}}]})
        if res == "videos" and params.get("chart") == "mostPopular":
            return FakeResp({"items": list(VIDEOS.values())})
        if res == "videos":
            return FakeResp({"items": [VIDEOS[i] for i in params["id"].split(",") if i in VIDEOS]})
        if res == "channels":
            ids = [params["forHandle"]] if "forHandle" in params else params["id"].split(",")
            items = []
            for cid in ids:
                cid = "UCaaaaaaaaaaaaaaaaaaaaaa" if cid.startswith("@") else cid
                items.append({"id": cid, "snippet": {"title": f"Chan {cid}", "publishedAt": _iso(9999)},
                              "statistics": {**CHANNELS[cid], "viewCount": "1", "videoCount": "6"},
                              "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}})
            return FakeResp({"items": items})
        if res == "playlistItems":
            cid = "UC" + params["playlistId"][2:]
            ids = [v["id"] for v in VIDEOS.values() if v["snippet"]["channelId"] == cid]
            return FakeResp({"items": [{"contentDetails": {"videoId": i}} for i in ids]})
        if res == "search":
            return FakeResp({"items": [{"id": {"videoId": i}} for i in VIDEOS]})
        if res == "commentThreads":
            return FakeResp({"items": [
                {"snippet": {"totalReplyCount": 2, "topLevelComment": {"snippet": {
                    "textDisplay": "Please make a video on haunted forts!", "likeCount": 50}}}},
                {"snippet": {"totalReplyCount": 0, "topLevelComment": {"snippet": {
                    "textDisplay": "Great story", "likeCount": 5}}}},
            ]})
        return FakeResp({"error": {"message": "unknown"}}, 404)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def client(cache=True):
    return YouTubeClient("k", session=FakeSession(), use_cache=cache)


def test_utils():
    assert utils.parse_duration("PT1H2M3S") == 3723
    assert utils.detect_language({"title": "सबसे डरावनी कहानी"}) == "hi"
    assert utils.detect_language({"title": "Hello", "defaultAudioLanguage": "ta"}) == "ta"
    assert len(utils.parse_chapters(CHAPTERS)) == 5
    assert utils.to_local(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "Asia/Kolkata").hour == 5


def test_trending_summary_and_cache():
    yt = client()
    rows = trending.collect_trending(yt, ["IN"], 50)
    assert len(rows) == 6
    assert {r["lang_source"] for r in rows} == {"metadata", "script"}
    s = trending.summarize(rows)
    assert s["languages"][0]["language"] in ("hi", "en")
    calls = len(yt.session.calls)
    trending.collect_trending(yt, ["IN"], 50)
    assert len(yt.session.calls) == calls, "second fetch should come from the cache"
    assert storage.quota_used_today() == calls


def test_styles_and_language_refinement(monkeypatch):
    rows = trending.collect_trending(client(), ["IN"], 50)
    monkeypatch.setattr(ai, "classify_channel_styles", lambda chans: {
        c["channel_id"]: {"style": "faceless horror stories", "presenter": "faceless_voiceover",
                          "is_big_media": c["channel_id"].startswith("UCb")} for c in chans})
    monkeypatch.setattr(ai, "refine_languages", lambda vids: {v["video_id"]: "hi" for v in vids})
    changed = trending.refine_languages(rows)
    assert changed >= 1  # "Haunted highway ki kahani" was English by script, Hindi per Claude
    trending.classify_styles(rows)
    s = trending.summarize(rows, exclude_big_media=True)
    assert all(not r.get("is_big_media") for l in s["languages"] for r in l["top_channels"])
    assert s["overall_styles"][0][0] == "faceless horror stories"


def test_breakouts():
    res = trending.find_breakouts(client(), "IN", "hi", days=7, max_subscribers=500_000)
    assert res and all(r["subscribers"] <= 500_000 for r in res)
    assert res[0]["breakout_ratio"] >= res[-1]["breakout_ratio"]


def test_snapshots_and_trends():
    rows = trending.collect_trending(client(), ["IN"], 50)
    storage.snapshot_save(["IN"], rows)
    snaps = storage.snapshots_load(["IN"])
    assert len(snaps) == 1
    table = {"2026-09-01": {"A": 50.0, "B": 50.0}, "2026-09-08": {"A": 20.0, "B": 60.0, "C": 20.0}}
    rf = trending.rising(table)
    assert rf["rising"][0][0] in ("B", "C") and rf["fading"][0] == ("A", -30.0)
    assert trending.trend_counts(snaps, by="format")


def test_channel_analysis_blend_and_template(tmp_path):
    yt = client()
    a = analyze_channel(yt, "@horror", n_videos=10, transcripts=0, comment_videos=2, workdir=None)
    assert a["cadence"]["timezone"] == "Asia/Kolkata"
    assert a["audience"]["viewer_requests"][0]["text"].startswith("Please make")
    assert a["chapters"]["pct_with_chapters"] > 0
    b = analyze_channel(yt, "UCbbbbbbbbbbbbbbbbbbbbbb", n_videos=10, transcripts=0, comment_videos=0)
    blend = combine_analyses([a, b])
    assert blend["mode"] == "blend" and len(blend["sources"]) == 2
    assert blend["sample_size"] == a["sample_size"] + b["sample_size"]
    md = generate_template(blend, {"language": "Hindi", "production_tools": "CapCut, ElevenLabs"})
    assert "{TOPIC}" in md and "CapCut" in md and "haunted forts" in md
    json.dumps(blend, default=str)  # history needs it serialisable


def test_history_roundtrip():
    i = storage.history_save("prompt", "x", {"markdown": "# hi"})
    assert storage.history_list("prompt")[0]["id"] == i
    assert storage.history_get(i)["markdown"] == "# hi"
    storage.history_delete(i)
    assert storage.history_list("prompt") == []


def test_tool_packs():
    spec = tool_pack_spec("Midjourney, ElevenLabs and capcut")
    assert all(TOOL_GUIDES[k] in spec for k in ("midjourney", "elevenlabs", "capcut"))
    assert tool_pack_spec("") == ""


def test_frames(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    path = tmp_path / "clip.mp4"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (320, 180))
    colours = [(0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 255, 255)]
    for i in range(25 * 20):  # 20 s, a hard cut every 5 s
        frame = np.full((180, 320, 3), colours[(i // 125) % 4], np.uint8)
        cv2.putText(frame, str(i), (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        w.write(frame)
    w.release()
    from ytrend.frames import analyze_video_file
    res = analyze_video_file(path, tmp_path / "frames", max_frames=8)
    assert res["shots"] == 4, res["shot_timeline"]
    assert res["cuts_per_minute"] == 9.0  # 3 cuts in 20 s
    assert res["frames"] and all(Path(f["path"]).exists() for f in res["frames"])


def test_ai_finder(monkeypatch):
    from ytrend import ai_finder
    monkeypatch.setitem(VIDEOS["v2"], "status", {"containsSyntheticMedia": True})
    monkeypatch.setitem(VIDEOS["v3"]["snippet"], "tags", ["sora ai", "veo 3"])
    found, funnel = ai_finder.discover(client(), ["ai story", "ai video"], "IN", "hi", days=3)
    assert funnel["search results"] == 12 and funnel["unique videos"] == 6
    assert len(found) == 6 and all(r["queries"] == ["ai story", "ai video"] for r in found)
    assert found[0]["views_per_hour"] >= found[-1]["views_per_hour"]
    by_id = {r["video_id"]: r for r in found}
    assert "uploader disclosed synthetic media" in by_id["v2"]["ai_signals"]
    assert "sora" in by_id["v3"]["ai_reason"] and by_id["v3"]["ai_verdict"] == "likely_ai"
    assert by_id["v4"]["ai_verdict"] == "unclear"

    monkeypatch.setattr(ai, "detect_ai_videos", lambda vids, use_thumbnails=True: {
        v["video_id"]: {"verdict": "ai_generated" if v["video_id"] in ("v1", "v2") else "not_ai",
                        "confidence": 90, "niche": "AI Hindi horror story", "reason": "waxy faces"}
        for v in vids})
    ai_finder.verify_with_claude(found)
    niches = ai_finder.summarize_niches(found)
    assert [n["niche"] for n in niches] == ["AI Hindi horror story"] and niches[0]["videos"] == 2


def test_ai_terms_do_not_match_plain_words():
    from ytrend.ai_finder import metadata_signals
    assert metadata_signals({"title": "Said the captain", "description": "", "tags": []}) == []
    assert metadata_signals({"title": "Kling AI dragon", "description": "", "tags": []})


def test_tutorials_seeds_and_watchlist(monkeypatch):
    from ytrend import ai_finder
    assert ai_finder.is_tutorial({"title": "AI video kaise banaye free me", "description": ""})
    assert not ai_finder.is_tutorial({"title": "Hanuman ji ki kahani #aikahani", "description": ""})
    monkeypatch.setitem(VIDEOS["v4"]["snippet"], "title", "How to make AI videos free")
    found, funnel = ai_finder.discover(client(), ["ai"], None, None, min_vph=1)
    assert "v4" not in {r["video_id"] for r in found} and funnel["after removing tutorials"] == 5

    qs, seeds = ai_finder.seed_queries(client(), ["@horror"], per_seed=3)
    assert seeds[0]["channel_id"] == "UCaaaaaaaaaaaaaaaaaaaaaa"
    assert "#horror" in qs and len(qs) <= 3

    storage.watch_add("UCaaaaaaaaaaaaaaaaaaaaaa", "Chan A")
    storage.watch_add("UCaaaaaaaaaaaaaaaaaaaaaa", "Chan A")
    assert len(storage.watch_list()) == 1
    rows, funnel = ai_finder.scan_channels(client(), [w["channel_id"] for w in storage.watch_list()], days=7)
    assert {r["video_id"] for r in rows} == {"v1", "v2"}  # v5/v6 are older than 7 days
    storage.watch_remove("UCaaaaaaaaaaaaaaaaaaaaaa")
    assert storage.watch_list() == []
