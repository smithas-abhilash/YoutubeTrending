"""Shared helpers: ISO-8601 durations, language detection, text features."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

_DURATION_RE = re.compile(
    r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?"
)


def parse_duration(iso: str | None) -> int:
    """'PT1H2M3S' -> 3723 seconds."""
    if not iso:
        return 0
    m = _DURATION_RE.fullmatch(iso)
    if not m:
        return 0
    d, h, mi, s = (int(m.group(k) or 0) for k in "dhms")
    return d * 86400 + h * 3600 + mi * 60 + s


def fmt_seconds(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


TIMEZONES = ["Asia/Kolkata", "UTC", "America/New_York", "America/Los_Angeles", "Europe/London",
             "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney"]


def to_local(dt: datetime, tz_name: str) -> datetime:
    from zoneinfo import ZoneInfo  # Windows needs the `tzdata` package for this
    return dt.astimezone(ZoneInfo(tz_name))


def hours_since(ts: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return max((now - parse_time(ts)).total_seconds() / 3600, 1.0)


# Unicode script -> most likely language code. Scripts shared by several
# languages (Latin, Cyrillic, Arabic, Devanagari) map to a best guess; the
# API's defaultAudioLanguage/defaultLanguage fields take precedence anyway.
_SCRIPT_LANG = {
    "DEVANAGARI": "hi",
    "BENGALI": "bn",
    "GURMUKHI": "pa",
    "GUJARATI": "gu",
    "ORIYA": "or",
    "TAMIL": "ta",
    "TELUGU": "te",
    "KANNADA": "kn",
    "MALAYALAM": "ml",
    "SINHALA": "si",
    "THAI": "th",
    "HANGUL": "ko",
    "HIRAGANA": "ja",
    "KATAKANA": "ja",
    "CJK": "zh",
    "ARABIC": "ar",
    "HEBREW": "he",
    "CYRILLIC": "ru",
    "GREEK": "el",
    "GEORGIAN": "ka",
    "ARMENIAN": "hy",
    "ETHIOPIC": "am",
    "KHMER": "km",
    "LAO": "lo",
    "MYANMAR": "my",
}

LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati",
    "or": "Odia", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
    "mr": "Marathi", "ur": "Urdu", "si": "Sinhala", "th": "Thai", "ko": "Korean",
    "ja": "Japanese", "zh": "Chinese", "ar": "Arabic", "he": "Hebrew", "ru": "Russian",
    "el": "Greek", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German",
    "it": "Italian", "id": "Indonesian", "tr": "Turkish", "vi": "Vietnamese",
    "ka": "Georgian", "hy": "Armenian", "am": "Amharic", "km": "Khmer", "lo": "Lao",
    "my": "Burmese", "uk": "Ukrainian", "pl": "Polish", "nl": "Dutch", "fil": "Filipino",
    "unknown": "Unknown",
}


def _script_of(ch: str) -> str | None:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for script in _SCRIPT_LANG:
        if name.startswith(script):
            return script
    if "LATIN" in name:
        return "LATIN"
    return None


def language_from_metadata(snippet: dict) -> str | None:
    """Language the uploader declared, if any."""
    for key in ("defaultAudioLanguage", "defaultLanguage"):
        lang = snippet.get(key)
        if lang and lang not in ("zxx", "und"):
            return lang.split("-")[0].lower()
    return None


def detect_language(snippet: dict) -> str:
    """Best-effort language code for a video snippet (metadata first, then script)."""
    declared = language_from_metadata(snippet)
    if declared:
        return declared
    text = f"{snippet.get('title', '')} {snippet.get('description', '')[:300]}"
    counts = Counter(s for s in map(_script_of, text) if s)
    if not counts:
        return "unknown"
    non_latin = {k: v for k, v in counts.items() if k != "LATIN"}
    # A title with a meaningful share of a non-Latin script belongs to that language,
    # even if it also has English words ("Hindi title | Full Episode").
    if non_latin:
        script, n = max(non_latin.items(), key=lambda kv: kv[1])
        if n >= 0.2 * sum(counts.values()):
            return _SCRIPT_LANG[script]
    return "en" if counts.get("LATIN") else "unknown"


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF]"
)
_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

STOPWORDS = set(
    """the and for with you your this that from are was were have has how what why when
    who will can not but all out new its our get got just into more most than then them
    they video full part official live episode vs""".split()
)


def title_features(title: str) -> dict:
    letters = [c for c in title if c.isalpha()]
    upper = sum(1 for c in letters if c.isupper())
    return {
        "chars": len(title),
        "words": len(title.split()),
        "has_emoji": bool(_EMOJI_RE.search(title)),
        "has_number": bool(re.search(r"\d", title)),
        "has_question": "?" in title,
        "has_exclaim": "!" in title,
        "has_brackets": bool(re.search(r"[\[\(\|]", title)),
        "has_hashtag": "#" in title,
        "caps_ratio": round(upper / len(letters), 2) if letters else 0.0,
    }


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text) if w.lower() not in STOPWORDS]


_CHAPTER_RE = re.compile(r"^\s*[\(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\)\]]?\s*[-–—:|]?\s*(.+?)\s*$")


def parse_chapters(description: str) -> list[dict]:
    """Extract '0:00 Intro' style chapter markers from a description."""
    chapters = []
    for line in description.splitlines():
        m = _CHAPTER_RE.match(line)
        if not m:
            continue
        parts = [int(p) for p in m.group(1).split(":")]
        sec = 0
        for p in parts:
            sec = sec * 60 + p
        chapters.append({"start": sec, "title": m.group(2)})
    # YouTube requires chapters to start at 0:00 with at least 3 entries.
    if len(chapters) >= 3 and chapters[0]["start"] == 0:
        return chapters
    return []
