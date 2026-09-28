"""Step 3: turn the channel analysis (+ your screenshots) into a master prompt.

With ANTHROPIC_API_KEY (or an `ant auth login` profile) Claude studies the
data and the images and writes the master prompt. Without credentials a
deterministic template version is produced from the numbers alone.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

MODEL = "claude-opus-5"
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 20

SYSTEM = """You are a YouTube content strategist and video-production director.
You receive a data analysis of a successful channel (cadence, formats, title
patterns, chapter structures, transcripts of its best videos) and, when
available, its thumbnails and screenshots of its videos.

Your job is to reverse-engineer the *repeatable format* behind its success and
write a MASTER PROMPT the user can paste into an AI assistant or video tool to
produce new videos that feel like they belong to the same trending genre.

Rules:
- Capture structure, pacing, tone, visual grammar and packaging — not content.
  The output must lead to original videos: never reuse the channel's name,
  logo, catchphrases, characters, scripts, music or exact titles, and never
  tell the user to impersonate the creator.
- Ground every claim in the supplied data or images. When something is a guess
  (e.g. editing style inferred from one screenshot) say so briefly.
- Be concrete: seconds, word counts, shot types, colours, font styles, on-screen
  text rules, B-roll ratios, CTA placement.
"""

OUTPUT_SPEC = """Write your answer in Markdown with exactly these sections:

## 1. Format DNA
What this channel really is in 5-8 bullets: niche, audience, language/register,
promise to the viewer, why it trends now.

## 2. Video Blueprint (the exact sequence)
A timestamped beat sheet for the dominant format (and a second one for Shorts
if the channel uses them): e.g. 0:00-0:05 hook, 0:05-0:20 setup, ... end screen.
For each beat: purpose, what is said, what is shown, on-screen text, typical
duration, transitions/sound cues.

## 3. Packaging Rules
Title formulas (with fill-in-the-blank templates and 5 fresh example titles on
new topics), thumbnail recipe (layout, subject, face/emotion, text word count,
colours, contrast), description + tags template, upload cadence/day/time.

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

## 6. Variations & Next Steps
3 topic ideas that fit the format, and what extra material (screens, audio
samples, more channels) would sharpen the analysis.
"""


def _image_blocks(paths: list[Path]) -> tuple[list[dict], list[str]]:
    blocks, skipped = [], []
    for p in paths[:MAX_IMAGES]:
        mt = IMAGE_TYPES.get(p.suffix.lower())
        if not mt or not p.is_file():
            skipped.append(f"{p} (unsupported)")
            continue
        data = p.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            skipped.append(f"{p} (over 5 MB)")
            continue
        blocks.append({"type": "text", "text": f"Image: {p.name}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                   "data": base64.standard_b64encode(data).decode()}})
    skipped += [f"{p} (limit {MAX_IMAGES} images)" for p in paths[MAX_IMAGES:]]
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


def generate_with_claude(
    analysis: dict,
    images: list[Path],
    brief: dict,
    model: str = MODEL,
    trending_context: dict | None = None,
) -> str:
    import anthropic

    client = anthropic.Anthropic()
    img_blocks, skipped = _image_blocks(images)

    content: list[dict] = [
        {"type": "text", "text": "<channel_analysis>\n"
         + json.dumps(analysis, ensure_ascii=False, indent=1, default=str)
         + "\n</channel_analysis>"},
    ]
    if trending_context:
        content.append({"type": "text", "text": "<trending_context>\n"
                        + json.dumps(trending_context, ensure_ascii=False, indent=1, default=str)
                        + "\n</trending_context>"})
    if img_blocks:
        content.append({"type": "text", "text": "Thumbnails and screenshots from this channel's videos "
                        "(file names starting with thumb_ are thumbnails of its top videos; others were "
                        "supplied by the user, e.g. frames from a trending video):"})
        content += img_blocks
    content.append({"type": "text", "text": "<creator_brief>\n"
                    + json.dumps(brief, ensure_ascii=False, indent=1)
                    + "\n</creator_brief>\n\n" + OUTPUT_SPEC})

    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        # Re-run on a fallback model if a safety classifier declines the request.
        extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {getattr(message, 'stop_details', None)}")
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += "\n\n> Note: output was truncated (max_tokens reached)."
    if skipped:
        text += "\n\n> Images not sent: " + ", ".join(skipped)
    return text


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

    return f"""# Master prompt (template mode — run with ANTHROPIC_API_KEY for a richer, vision-based version)

Reference channel: {ch['title']} ({ch['url']}) — {analysis['sample_size']} recent videos analysed.

```text
You are the head writer and director of a YouTube channel called {{CHANNEL_NAME}}
in the same genre as "{ch['title']}". Create a brand-new, original video about
{{TOPIC}} in {lang}. Target length: {{DURATION}} (reference median: {duration}).
Primary format: {main_fmt} video. Publishing rhythm to plan for: about
{cad['videos_per_week']} videos/week, best days {', '.join(d for d, _ in cad['top_weekdays'])}.

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

Do not copy the reference channel's name, catchphrases, scripts or branding —
match its structure and energy only.
```
"""
