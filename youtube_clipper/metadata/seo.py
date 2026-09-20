"""Context-first YouTube Shorts metadata generator.

Metadata is driven primarily by the identified source/show context.  Transcript
text is used only for the title hook; it is deliberately NOT mined for SEO tags.
This prevents random spoken words from becoming public tags.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "so", "to", "of", "in", "on",
    "for", "with", "is", "it", "this", "that", "you", "i", "we", "they",
    "he", "she", "was", "are", "be", "as", "at", "from", "by", "about",
    "what", "why", "how", "when", "where", "who", "your", "my", "our",
    "their", "me", "us", "do", "does", "did", "have", "has", "had", "not",
    "no", "yes", "just", "very", "really", "like", "know", "think", "thing",
    "things", "can", "could", "would", "should", "will", "been", "being",
    "than", "then", "there", "here", "into", "also", "only", "more", "get",
    "got", "im", "ive", "hes", "shes", "thats", "dont", "cant", "wont", "youre",
    "its",
}

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

PROMOTIONAL_IDENTITY_RE = re.compile(
    r"\b(?:insane details?|details? you|you (?:probably )?missed|did you know|"
    r"things? you|must watch|watch (?:this|till)|best moments?|top \d+|"
    r"ending explained|explained ending|hidden details?|facts? you)\b",
    re.I,
)

TAG_VARIANT_WORDS = {
    "clip", "clips", "scene", "scenes", "short", "shorts", "moment",
    "moments", "quote", "quotes", "dialogue", "edit", "edits", "video",
    "videos", "show", "series",
}

SEO_NOISE_TAGS = {
    "insane", "details", "detail", "missed", "probably", "viral", "crazy",
    "unbelievable", "amazing", "shocking", "watch", "video",
}

TYPE_LABELS = {
    "webseries": "TV series",
    "movie": "movie",
    "anime": "anime",
    "sports": "sports",
    "news": "news",
    "podcast": "podcast",
    "music_video": "music video",
    "creator_video": "creator video",
    "unknown": "video",
}

CATEGORY_BY_TYPE = {
    "webseries": "24",
    "movie": "24",
    "anime": "24",
    "music_video": "10",
    "sports": "17",
    "news": "25",
    "podcast": "22",
    "creator_video": "22",
    "unknown": "22",
}


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_urls(text: str) -> str:
    return normalize(URL_RE.sub("", text))


def strip_urls_preserve(text: str) -> str:
    return URL_RE.sub("", str(text or "")).strip()


def clean_title(text: str) -> str:
    text = strip_urls(text).replace("\n", " ")
    return text[:100].rstrip(" -:;,|#") or "Interesting Moment"


def clean_tag(tag: str) -> str:
    tag = strip_urls(str(tag)).strip("#, ")
    tag = re.sub(r"[\r\n]+", " ", tag)
    tag = re.sub(r"\s+", " ", tag).strip(" ,;|")
    if not tag or len(tag) > 60 or '"' in tag:
        return ""
    if not re.search(r"[A-Za-z0-9]", tag):
        return ""
    if re.fullmatch(r"\d+(?:[.,]\d+)?", tag):
        return ""
    return tag


def youtube_tag_cost(tag: str, include_comma: bool = False) -> int:
    # YouTube's tags field has a 500-character limit. Spaces in a tag are
    # counted as if the tag were quoted, and commas separate tags.
    cost = len(normalize(tag)) + (2 if " " in normalize(tag) else 0)
    return cost + (1 if include_comma else 0)


def youtube_tags_character_count(tags: list[str]) -> int:
    return sum(youtube_tag_cost(tag, i > 0) for i, tag in enumerate(tags))


def tag_tokens(tag: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize(tag).casefold()))


def tags_are_near_duplicates(left: str, right: str) -> bool:
    """Detect keyword-stuffed variants such as 'Show', 'Show clips', 'Show scenes'."""
    a = tag_tokens(left)
    b = tag_tokens(right)
    if not a or not b:
        return False
    if a == b:
        return True
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    if smaller.issubset(larger) and (larger - smaller).issubset(TAG_VARIANT_WORDS):
        return True
    return len(a & b) / len(a | b) >= 0.75


def validate_and_normalize_tags(tags: list[str], max_chars: int = 470, max_tags: int = 15) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0

    for raw in tags:
        tag = clean_tag(raw)
        key = tag.casefold()
        if not tag or key in seen:
            continue
        if any(tags_are_near_duplicates(tag, existing) for existing in out):
            continue
        if len(tag.split()) == 1:
            word = tag.casefold()
            if word in STOPWORDS or word in SEO_NOISE_TAGS or len(word) < 4 or word.isdigit():
                continue
        cost = youtube_tag_cost(tag, bool(out))
        if total + cost > max_chars:
            continue
        seen.add(key)
        out.append(tag)
        total += cost
        if len(out) >= max_tags:
            break

    return out


def dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = clean_tag(item)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def source_context_for_clip(clip: dict, project_dir: Path) -> dict:
    path_value = clip.get("source_context_file")
    if not path_value:
        return {}
    path = Path(path_value)
    if not path.exists():
        path = project_dir / "cache" / path.name
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def source_confident(context: dict) -> bool:
    try:
        confidence = float(context.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return bool(normalize(context.get("title"))) and confidence >= 0.55


def extract_series_name(source_title: str) -> str:
    """Extract the underlying show/movie name from common detector titles."""
    title = normalize(source_title).strip('"')
    if not title:
        return ""

    # e.g. "Something | The Office"
    if "|" in title:
        right = normalize(title.split("|")[-1]).strip('"')
        if 2 <= len(right) <= 80:
            return right

    # e.g. "Best Moments - The Office" or "Season 5 - 2 Broke Girls"
    parts = re.split(r"\s+-\s+", title)
    if len(parts) > 1:
        candidate = normalize(parts[-1]).strip('"')
        if 2 <= len(candidate) <= 80:
            return candidate

    return title[:80]


def useful_series_name(series: str) -> bool:
    """Reject clickbait headlines that are not a stable show/movie identity."""
    value = normalize(series)
    if not value or PROMOTIONAL_IDENTITY_RE.search(value):
        return False
    words = re.findall(r"[A-Za-z0-9]+", value)
    return 1 <= len(words) <= 8 and len(value) <= 55


def words_for_clip(transcript: list[dict], start: float, end: float) -> list[dict]:
    words: list[dict] = []
    for segment in transcript:
        for word in segment.get("words", []):
            try:
                ws = float(word.get("start", 0))
                we = float(word.get("end", 0))
            except (TypeError, ValueError):
                continue
            if ws < end and we > start:
                words.append(word)
    return words


def clip_text_from_transcript(clip: dict, transcript: list[dict]) -> str:
    # This text is used for the title hook only. It is intentionally never fed
    # into the tag generator.
    text = strip_urls(clip.get("text", ""))
    if text:
        return text
    words = words_for_clip(
        transcript,
        float(clip.get("start", 0)),
        float(clip.get("end", 60)),
    )
    return normalize(" ".join(w.get("text", "") for w in words))


def title_hook_text(text: str) -> str:
    text = normalize(text)
    if not text:
        return "Interesting Moment"

    sentences = [
        normalize(x) for x in re.split(r"(?<=[.!?])\s+", text) if normalize(x)
    ]
    for sentence in sentences:
        if "?" in sentence and 20 <= len(sentence) <= 110:
            return re.sub(
                r"^(oh[, ]+|wait[, ]+|so[, ]+|uh[, ]+|um[, ]+)+",
                "",
                sentence,
                flags=re.I,
            ).strip()
    for sentence in sentences:
        if 25 <= len(sentence) <= 110:
            return re.sub(
                r"^(oh[, ]+|wait[, ]+|so[, ]+|uh[, ]+|um[, ]+)+",
                "",
                sentence,
                flags=re.I,
            ).strip()
    return text[:110]


def build_title(transcript_text: str, context: dict) -> str:
    hook = title_hook_text(transcript_text)
    series = extract_series_name(context.get("title", "")) if source_confident(context) else ""
    if not useful_series_name(series):
        series = ""

    # Always attach the identified series/show name. The hook comes from the
    # clip dialogue, but no title is invented from sentiment or assumptions.
    if series:
        return clean_title(f"{hook} | {series}")
    return clean_title(hook)


def _context_texts(context: dict) -> list[str]:
    """Return source-level evidence only, never transcript text."""
    values: list[str] = []
    for key in ("source_description", "source_channel"):
        value = normalize(context.get(key, ""))
        if value:
            values.append(value)
    for key in ("source_tags", "keywords"):
        for value in context.get(key, [])[:30]:
            value = normalize(value)
            if value:
                values.append(value)
    return values


def _has_context_term(term: str, context: dict) -> bool:
    term_tokens = set(re.findall(r"[a-z0-9]+", normalize(term).casefold()))
    if not term_tokens:
        return False
    evidence = " ".join(_context_texts(context)).casefold()
    evidence_tokens = set(re.findall(r"[a-z0-9]+", evidence))
    return term_tokens.issubset(evidence_tokens)


def build_context_tags(context: dict, channel_keywords: list[str] | None = None) -> list[str]:
    """Generate SEO tags from the larger source/show context only.

    No transcript words, OCR words, clip dialogue, or audiovisual scene terms
    are promoted into tags.  This is deliberate: tags should describe the
    source and its established entities, not arbitrary words spoken in one
    Short.
    """
    if not source_confident(context):
        # If source identity is uncertain, only use explicitly configured
        # channel keywords that are not generic filler. Do not mine dialogue.
        return validate_and_normalize_tags(channel_keywords or [])

    series = extract_series_name(context.get("title", ""))
    source_type = normalize(context.get("source_type", "unknown")).casefold()
    season = normalize(context.get("season", ""))
    episode = normalize(context.get("episode", ""))
    ranked: list[tuple[int, str]] = []

    def add(tag: str, score: int) -> None:
        tag = clean_tag(tag)
        if tag:
            ranked.append((score, tag))

    if series and useful_series_name(series):
        # Keep one strong source identity tag. Near-identical variants such as
        # "show clips", "show scenes", and "show moments" add no useful reach.
        add(series, 120)
        add(f"{series} clips", 118)

        if source_type == "webseries":
            # Only use genre labels when the source-level evidence supports
            # them. This avoids calling every webseries a sitcom/comedy.
            if _has_context_term("sitcom", context):
                add(f"{series} sitcom", 104)
            if _has_context_term("comedy", context):
                add(f"{series} comedy", 102)
        elif source_type == "anime":
            add(f"{series} anime", 104)
        elif source_type == "movie":
            add(f"{series} movie", 104)

        if season:
            add(f"{series} season {season}", 100)
            if episode:
                add(f"{series} season {season} episode {episode}", 98)

    # The detector only writes characters after source-level evidence has met
    # its reliability threshold. Keep them even if a clickbait source title is
    # rejected as the series identity.
    for character in context.get("characters", [])[:6]:
        character = normalize(character)
        if character:
            add(character, 96)
            if series and useful_series_name(series):
                add(f"{character} {series}", 94)

    # Detector keywords are source-level evidence. They add topical breadth
    # without mining arbitrary dialogue from the individual clip.
    for keyword in context.get("keywords", [])[:10]:
        add(keyword, 72)

    discovery_tags = {
        "webseries": ["YouTube Shorts", "TV clips"],
        "movie": ["YouTube Shorts", "movie clips"],
        "anime": ["YouTube Shorts", "anime clips"],
        "sports": ["YouTube Shorts", "sports highlights"],
        "news": ["YouTube Shorts", "news clips"],
        "podcast": ["YouTube Shorts", "podcast clips"],
        "music_video": ["YouTube Shorts", "music videos"],
        "creator_video": ["YouTube Shorts", "creator clips"],
        "unknown": ["YouTube Shorts"],
    }
    for tag in discovery_tags.get(source_type, discovery_tags["unknown"]):
        add(tag, 45)

    # Explicitly configured channel keywords are allowed, but only after the
    # source-context tags and never from the transcript.
    for keyword in channel_keywords or []:
        add(keyword, 30)

    ranked.sort(key=lambda item: -item[0])
    return validate_and_normalize_tags([tag for _, tag in ranked])


def hashtag(tag: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]", "", normalize(tag))
    return f"#{value}" if 3 <= len(value) <= 35 else ""


def build_description(series: str, tags: list[str]) -> str:
    hashes: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        h = hashtag(tag)
        if h and h.casefold() not in seen:
            seen.add(h.casefold())
            hashes.append(h)
    return f'Series - "{series}"\n\n#tags -\n' + " ".join(hashes)


def generate_metadata(
    clip: dict,
    index: int,
    transcript: list[dict],
    project_dir: Path,
    channel_keywords: list[str] | None = None,
    default_channel_keywords: list[str] | None = None,
    privacy_status: str = "private",
) -> dict:
    transcript_text = clip_text_from_transcript(clip, transcript)
    context = source_context_for_clip(clip, project_dir)

    # Compatibility with older selection files.
    if not context and clip.get("source_title"):
        context = {
            "title": clip.get("source_title", ""),
            "source_type": clip.get("source_type", "unknown"),
            "confidence": clip.get("source_confidence", 0.0),
        }

    series = extract_series_name(context.get("title", "")) if source_confident(context) else ""
    if not useful_series_name(series):
        series = ""
    title = build_title(transcript_text, context)
    configured_keywords = (
        channel_keywords
        if channel_keywords
        else (default_channel_keywords or [])
    )
    tags = build_context_tags(context, configured_keywords)

    # If a high-confidence source exists, the series itself is always the first
    # tag. If source confidence is low, don't fabricate a series tag.
    if series and not tags:
        tags = validate_and_normalize_tags([series])

    description = build_description(series, tags) if series else "#tags -\n" + " ".join(
        hashtag(tag) for tag in tags if hashtag(tag)
    )

    return {
        "title": strip_urls(title),
        "description": strip_urls_preserve(description),
        "tags": [strip_urls(tag) for tag in tags if strip_urls(tag)],
        "tag_character_count": youtube_tags_character_count(tags),
        "categoryId": CATEGORY_BY_TYPE.get(
            normalize(context.get("source_type", "unknown")), "22"
        ),
        "defaultLanguage": "en",
        "privacyStatus": privacy_status,
        "source_clip": index,
        # Internal traceability only. The uploader must not place this context
        # into the YouTube API request body.
        "keywords": tags,
        "source_context": context,
    }
