"""Step 3: turn the channel analysis (+ screenshots) into a master prompt.

With ANTHROPIC_API_KEY (or an `ant auth login` profile) Claude studies the
data and the images and writes the master prompt, plus ready-to-use packs
for the tools the creator uses, and can refine it in a chat. Without
credentials a deterministic template version is built from the numbers.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from . import ai

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 30

SYSTEM = """You are a YouTube content strategist and video-production director.
You receive a data analysis of one successful channel, or a blend of several
channels in the same niche (cadence, formats, title patterns, chapter
structures, transcripts of the best videos, viewer comments), optionally a
shot-by-shot analysis of a reference video file, and images: thumbnails,
screenshots and keyframes.

Your job is to reverse-engineer the *repeatable format* behind the success and
write a MASTER PROMPT the user can paste into an AI assistant or video tool to
produce new videos that feel like they belong to the same trending genre.

Rules:
- Capture structure, pacing, tone, visual grammar and packaging, not content.
  The output must lead to original videos: never reuse a reference channel's
  name, logo, catchphrases, characters, scripts, music or exact titles, and
  never tell the user to impersonate a creator.
- When several channels are blended, keep what they have in common and call
  out where they differ.
- Ground every claim in the supplied data or images. When something is a guess
  (e.g. editing style inferred from one screenshot) say so briefly.
- Be concrete: seconds, word counts, shot types, colours, font styles, on-screen
  text rules, B-roll ratios, cuts per minute, CTA placement.
- Use viewer comments to pick topics the audience is asking for.
"""

OUTPUT_SPEC = """Write your answer in Markdown with exactly these sections:

## 1. Format DNA
What this format really is in 5-8 bullets: niche, audience, language/register,
promise to the viewer, why it trends now.

## 2. Video Blueprint (the exact sequence)
A timestamped beat sheet for the dominant format (and a second one for Shorts
if Shorts are used): e.g. 0:00-0:05 hook, 0:05-0:20 setup, ... end screen.
For each beat: purpose, what is said, what is shown, on-screen text, typical
duration, cuts per beat, transitions/sound cues. If a shot analysis of a video
file is supplied, use its measured pacing.

## 3. Packaging Rules
Title formulas (fill-in-the-blank templates and 5 fresh example titles on new
topics), thumbnail recipe (layout, subject, face/emotion, text word count,
colours, contrast), description + tags template, upload cadence/day/time
(in the time zone given in the analysis).

## 4. Visual & Audio Style Guide
Camera/shot types, editing pace (cuts per minute), captions, graphics, colour
grade, music/SFX usage, voice-over style.

## 5. MASTER PROMPT
A single self-contained prompt inside a ```text code block that the user can
paste into any LLM or AI video pipeline. It must contain placeholders like
{TOPIC}, {LANGUAGE}, {DURATION}, {CHANNEL_NAME} and instruct the model to
output, in order: 3 title options, a thumbnail brief, the full script split
into the beats from section 2 with timestamps, a shot list / B-roll list per
beat, on-screen text per beat, and description + tags.

## 6. Topic Ideas From The Audience
8 topic ideas that fit the format, prioritising what viewers ask for in the
comments (say which comment trend each one answers).
"""

# How to write for each tool the creator may list. Matched case-insensitively.
TOOL_GUIDES = {
    "midjourney": "Midjourney: one prompt per scene, subject first, then style, lighting, lens, "
                  "colour palette; end with --ar 16:9 (or 9:16 for Shorts) --style raw --v 7; "
                  "keep a shared style suffix so scenes look consistent.",
    "leonardo": "Leonardo AI: one prompt per scene plus a negative prompt; name the model/preset "
                "(e.g. Phoenix, Cinematic), aspect ratio and a consistent character description.",
    "dall": "DALL-E / GPT image: one natural-language prompt per scene with aspect ratio and a "
            "repeated style sentence for consistency.",
    "ideogram": "Ideogram: one prompt per scene; put any on-image text in quotes; good for thumbnails.",
    "flux": "Flux: one detailed prompt per scene, style and lighting keywords, aspect ratio.",
    "elevenlabs": "ElevenLabs: the voice-over script only, split per beat, with suggested voice type, "
                  "stability/style settings, <break time=\"0.5s\" /> tags for pauses and CAPS or "
                  "italics notes for emphasis; keep each paragraph under 800 characters.",
    "capcut": "CapCut: an editing checklist per beat: clip order, transition names, text/caption "
              "template style, auto-captions settings, keyframe zooms, sound effects, beat sync, "
              "export settings (resolution, fps).",
    "premiere": "Premiere Pro: an edit decision list per beat: cuts, J/L cuts, adjustment layers, "
                "Essential Graphics titles, audio ducking, export preset.",
    "davinci": "DaVinci Resolve: edit list per beat plus a colour-grade recipe (nodes) and Fairlight audio notes.",
    "canva": "Canva: thumbnail build steps (template size 1280x720, layers, fonts, colours, effects) "
             "and any on-screen graphic templates.",
    "photoshop": "Photoshop: thumbnail layer-by-layer recipe (cut-out subject, stroke, glow, text styles).",
    "runway": "Runway: one text/image-to-video prompt per shot (camera move, subject action, "
              "duration 5-10 s, style), noting which keyframe image to start from.",
    "kling": "Kling: one image-to-video prompt per shot with camera movement and motion strength.",
    "pika": "Pika: one short prompt per shot with motion and camera direction.",
    "sora": "Sora / Veo: one shot prompt per beat describing subject, action, camera, lighting and length.",
    "veo": "Veo: one shot prompt per beat describing subject, action, camera, lighting, audio and length.",
    "heygen": "HeyGen: avatar script per scene, avatar framing, background, gestures, and where to "
              "cut to B-roll.",
    "synthesia": "Synthesia: scene-by-scene avatar script with on-screen text and background per scene.",
    "invideo": "InVideo AI: one complete prompt for the whole video (topic, length, language, voice, "
               "style, pacing, music, captions) plus edit commands to fix scenes.",
    "pictory": "Pictory: the script formatted as scenes (one sentence per scene) with visual keywords.",
    "chatgpt": "ChatGPT / Gemini / Claude: the master prompt as-is plus a follow-up prompt that turns "
               "the script into per-scene image prompts.",
    "suno": "Suno / Udio: a background-music prompt (genre, tempo BPM, mood, instruments, no vocals).",
}


def tool_pack_spec(tools_text: str) -> str:
    tools_text = (tools_text or "").lower()
    guides = [g for key, g in TOOL_GUIDES.items() if key in tools_text]
    if not tools_text.strip():
        return ""
    lines = "\n".join(f"- {g}" for g in guides) or "- (no known tool matched; infer sensible formats)"
    return f"""
## 7. Tool Packs
The creator uses: {tools_text}. For each tool, write a ready-to-use pack for
ONE example video on the first topic idea, following these formats:
{lines}
For tools not listed above, use their native prompt/settings format.
"""


def _image_blocks(paths: list[Path]) -> tuple[list[dict], list[str]]:
    blocks, skipped = [], []
    for p in paths[:MAX_IMAGES]:
        mt = IMAGE_TYPES.get(p.suffix.lower())
        if not mt or not p.is_file():
            skipped.append(f"{p.name} (unsupported)")
            continue
        data = p.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            skipped.append(f"{p.name} (over 5 MB)")
            continue
        blocks.append({"type": "text", "text": f"Image: {p.name}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                   "data": base64.standard_b64encode(data).decode()}})
    skipped += [f"{p.name} (limit {MAX_IMAGES} images)" for p in paths[MAX_IMAGES:]]
    return blocks, skipped


def collect_images(*sources: str | Path | None) -> list[Path]:
    """Accepts image files and/or directories of images."""
    out: list[Path] = []
    for s in sources:
        if not s:
            continue
        p = Path(s)
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_TYPES)
        elif p.is_file():
            out.append(p)
    # de-duplicate while preserving order
    seen: set[Path] = set()
    return [p for p in out if not (p.resolve() in seen or seen.add(p.resolve()))]


def _block(tag: str, obj) -> dict:
    return {"type": "text", "text": f"<{tag}>\n{json.dumps(obj, ensure_ascii=False, indent=1, default=str)}\n</{tag}>"}


def start_conversation(
    analysis: dict,
    images: list[Path],
    brief: dict,
    trending_context: dict | None = None,
    video_analysis: dict | None = None,
) -> tuple[list[dict], str]:
    """Generate the master prompt. Returns (conversation, markdown)."""
    img_blocks, skipped = _image_blocks(images)
    slim = {k: v for k, v in analysis.items() if k != "thumbnail_files"}

    content: list[dict] = [_block("channel_analysis", slim)]
    if trending_context:
        content.append(_block("trending_context", trending_context))
    if video_analysis:
        va = {k: v for k, v in video_analysis.items() if k != "frames"}
        content.append(_block("reference_video_shot_analysis", va))
    if img_blocks:
        content.append({"type": "text", "text": (
            "Images. thumb_* = thumbnails of the top videos; frame_MMmSSs = keyframes from the "
            "reference video file at that timestamp; anything else = screenshots supplied by the user.")})
        content += img_blocks
    content.append(_block("creator_brief", brief))
    content.append({"type": "text", "text": OUTPUT_SPEC + tool_pack_spec(brief.get("production_tools", ""))})
    # Cache the big first turn so refinement messages don't pay for it again.
    content[-1]["cache_control"] = {"type": "ephemeral"}

    conversation = [{"role": "user", "content": content}]
    text = ai.stream_text(SYSTEM, conversation)
    if skipped:
        text += "\n\n> Images not sent: " + ", ".join(skipped)
    conversation.append({"role": "assistant", "content": text})
    return conversation, text


def refine(conversation: list[dict], instruction: str) -> tuple[list[dict], str]:
    """Follow-up edit ("make the hook shorter", "adapt for Shorts")."""
    conversation = conversation + [{"role": "user", "content": (
        instruction + "\n\nReply with the full updated document (all sections), not just the changes.")}]
    text = ai.stream_text(SYSTEM, conversation)
    return conversation + [{"role": "assistant", "content": text}], text


def generate_template(analysis: dict, brief: dict) -> str:
    """Offline master prompt built purely from the numbers (no LLM)."""
    ch = analysis["channel"]
    cad = analysis["cadence"]
    fm = analysis["formats"]
    tp = analysis["titles"]["top_quartile"]
    lang = brief.get("language") or next(iter(analysis["languages"]), "English")
    main_fmt = max(fm["mix"], key=fm["mix"].get)
    duration = fm["median_long_duration"] if main_fmt == "long" else fm["median_short_duration"]

    chapter_seq = ""
    ex = analysis["chapters"]["examples"]
    if ex:
        chapter_seq = "\n".join(
            f"  - [{c['start'] // 60}:{c['start'] % 60:02d}] {c['title']}" for c in ex[-1]["chapters"]
        )
        chapter_seq = f"Reference beat sequence (from one of its videos):\n{chapter_seq}\n"

    style = []
    if tp["pct_number"] > 40: style.append("include a number")
    if tp["pct_emoji"] > 30: style.append("use 1-2 emojis")
    if tp["pct_question"] > 30: style.append("phrase as a question")
    if tp["pct_brackets_or_pipe"] > 30: style.append("add a bracketed or | separated qualifier")
    if tp["avg_caps_ratio"] > 0.4: style.append("use heavy CAPITALISATION on key words")

    requests_ = [r["text"] for r in (analysis.get("audience") or {}).get("viewer_requests", [])[:5]]
    requests_txt = ("\nViewers of this niche are asking for:\n" + "\n".join(f"  - {r[:120]}" for r in requests_)
                    + "\n") if requests_ else ""

    tools = brief.get("production_tools", "")
    tools_txt = ""
    if tools:
        guides = [g for key, g in TOOL_GUIDES.items() if key in tools.lower()]
        tools_txt = "6. Tool packs for: " + tools + "\n" + "\n".join(f"   - {g}" for g in guides) + "\n"

    return f"""# Master prompt (template mode — add an Anthropic key for the full, vision-based version)

Reference: {ch['title']} ({ch['url']}) — {analysis['sample_size']} recent videos analysed.

```text
You are the head writer and director of a YouTube channel called {{CHANNEL_NAME}}
in the same genre as "{ch['title']}". Create a brand-new, original video about
{{TOPIC}} in {lang}. Target length: {{DURATION}} (reference median: {duration}).
Primary format: {main_fmt} video. Publishing rhythm to plan for: about
{cad['videos_per_week']} videos/week, best days {', '.join(d for d, _ in cad['top_weekdays'])}.
{requests_txt}
{chapter_seq}
Output, in this order:
1. Three title options: ~{tp['avg_words']:.0f} words / {tp['avg_chars']:.0f} characters; {', '.join(style) or 'plain, clear phrasing'}.
   Proven words in this niche: {', '.join(w for w, _ in analysis['titles']['frequent_words'][:10])}.
2. Thumbnail brief: subject, facial emotion, 2-4 word overlay text, colours, layout.
3. Full script split into timestamped beats: HOOK (first 5 s), SETUP, main
   segments, PAYOFF, CTA/end screen. Keep the pacing of the reference beats.
4. For every beat: shot list / B-roll, on-screen text, music/SFX cue.
5. Description (first 2 lines hook + chapters) and 15 tags.
   Tag pool to draw from: {', '.join(t for t, _ in analysis['tags'][:15])}.
{tools_txt}
Do not copy the reference channel's name, catchphrases, scripts or branding —
match its structure and energy only.
```
"""
