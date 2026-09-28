"""Claude helpers: style classification, language refinement, comment insights.

All functions need ANTHROPIC_API_KEY (or an `ant auth login` profile).
Call `available()` first and fall back to the heuristics when it is False.
"""

from __future__ import annotations

import json
import os
from typing import Any

MODEL = "claude-opus-5"
# Re-run on a fallback model if a safety classifier declines the request.
_FALLBACK = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

PRESENTER_TYPES = ["face_on_camera", "faceless_voiceover", "ai_generated", "animation",
                   "screen_recording", "text_and_music", "clips_compilation", "mixed", "unknown"]


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _client():
    import anthropic
    return anthropic.Anthropic()


def _check(message) -> None:
    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {getattr(message, 'stop_details', None)}")


def json_call(system: str, content: Any, schema: dict, effort: str = "low", max_tokens: int = 16000) -> dict:
    """Single request whose answer is guaranteed to match `schema`."""
    message = _client().beta.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
        **_FALLBACK,
    )
    _check(message)
    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text)


def stream_text(system: str, messages: list[dict], effort: str = "high", max_tokens: int = 64000) -> str:
    """Long-form answer; streamed so big outputs don't hit HTTP timeouts."""
    with _client().beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        messages=messages,
        **_FALLBACK,
    ) as stream:
        message = stream.get_final_message()
    _check(message)
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += "\n\n> Note: output was truncated (max_tokens reached)."
    return text


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------- 2. channel style
_STYLE_SYSTEM = """You label YouTube channels by the kind of content they make, so a new
creator can see which *formats* are trending. YouTube's own categories
("Entertainment", "People & Blogs") are too coarse; describe the actual format.

For each channel return:
- style: 3-7 words, format first, e.g. "faceless AI horror story Shorts",
  "Hindi stock-market explainer with charts", "Telugu movie trailer / studio",
  "prank vlog family channel", "cricket highlights compilation".
  Mention the language only when it is part of the niche.
- presenter: one of the allowed values.
- is_big_media: true for TV networks, film studios, music labels, news
  broadcasters and celebrity/official channels a newcomer cannot copy.
Base it only on the titles, tags and category given."""

_STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "channels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "style": {"type": "string"},
                    "presenter": {"type": "string", "enum": PRESENTER_TYPES},
                    "is_big_media": {"type": "boolean"},
                },
                "required": ["channel_id", "style", "presenter", "is_big_media"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["channels"],
    "additionalProperties": False,
}


def classify_channel_styles(channels: list[dict]) -> dict[str, dict]:
    """channels: [{channel_id, channel, category, titles: [...], tags: [...]}] -> {channel_id: labels}."""
    out: dict[str, dict] = {}
    for batch in _batches(channels, 40):
        payload = json.dumps(batch, ensure_ascii=False)
        data = json_call(_STYLE_SYSTEM, f"<channels>\n{payload}\n</channels>", _STYLE_SCHEMA)
        for c in data["channels"]:
            out[c["channel_id"]] = {k: c[k] for k in ("style", "presenter", "is_big_media")}
    return out


# ---------------------------------------------------------------- 4. language
_LANG_SYSTEM = """Identify the spoken language of each YouTube video from its title and
description snippet. Return an ISO 639-1 code (e.g. hi, mr, ne, ur, en, ta, bn).
Tell Hindi from Marathi and Nepali (all Devanagari) by vocabulary. Hindi or
other Indian languages written in Latin letters ("Hinglish": "kya baat hai",
"bhai log") are that Indian language, not English. Use "en" only for English."""

_LANG_SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"video_id": {"type": "string"}, "language": {"type": "string"}},
                "required": ["video_id", "language"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["videos"],
    "additionalProperties": False,
}


def refine_languages(videos: list[dict]) -> dict[str, str]:
    """videos: [{video_id, title, description}] -> {video_id: language code}."""
    out: dict[str, str] = {}
    for batch in _batches(videos, 80):
        payload = json.dumps(batch, ensure_ascii=False)
        data = json_call(_LANG_SYSTEM, f"<videos>\n{payload}\n</videos>", _LANG_SCHEMA)
        for v in data["videos"]:
            out[v["video_id"]] = v["language"].lower().split("-")[0]
    return out


# ---------------------------------------------------------------- 6. comments
_COMMENTS_SYSTEM = """You read viewer comments from a YouTube channel's best videos and pull
out what the audience wants. Quote or paraphrase briefly, keep each item short,
merge duplicates, and order by how often/strongly it comes up. Write in English
even if the comments are in another language."""

_COMMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "requested_topics": {"type": "array", "items": {"type": "string"}},
        "unanswered_questions": {"type": "array", "items": {"type": "string"}},
        "what_viewers_love": {"type": "array", "items": {"type": "string"}},
        "complaints": {"type": "array", "items": {"type": "string"}},
        "audience_profile": {"type": "string"},
    },
    "required": ["requested_topics", "unanswered_questions", "what_viewers_love",
                 "complaints", "audience_profile"],
    "additionalProperties": False,
}


def comment_insights(comments: list[dict]) -> dict:
    """comments: [{video, text, likes}] -> structured audience insights."""
    payload = json.dumps(comments[:400], ensure_ascii=False)
    return json_call(_COMMENTS_SYSTEM, f"<comments>\n{payload}\n</comments>", _COMMENTS_SCHEMA,
                     effort="medium")
