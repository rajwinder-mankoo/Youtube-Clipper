"""
Real-world source and scene detector for YT Auto Bot.

The detector combines:
  1. Original YouTube metadata when the input came from a URL.
  2. Filename / sidecar URL clues for local videos.
  3. Distinctive transcript quotes searched on the public web via DDGS.
  4. Optional OCR from sampled frames when pytesseract + Tesseract are available.
  5. Heuristic entity scoring and source-type classification.

It deliberately returns a confidence score and evidence list instead of
pretending that source identification is certain.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from ddgs import DDGS
except Exception:  # pragma: no cover - optional at runtime
    DDGS = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None


STOPWORDS = {
    "about", "after", "again", "against", "almost", "because", "before",
    "being", "could", "did", "does", "doing", "during", "each", "from",
    "have", "having", "into", "just", "more", "most", "much", "never",
    "only", "other", "really", "should", "some", "than", "that", "their",
    "there", "these", "they", "think", "this", "those", "through", "very",
    "want", "what", "when", "where", "which", "while", "with", "would",
    "your", "you", "them", "then", "were", "will", "been", "here", "know",
    "like", "make", "made", "thing", "things", "something", "someone", "look",
    "come", "going", "said", "tell", "take", "good", "well", "okay", "yeah",
    "yes", "right", "really", "actually", "maybe", "because", "cannot", "can't",
}

GENERIC_RESULT_WORDS = {
    "official", "clip", "clips", "scene", "scenes", "episode", "episodes",
    "shorts", "short", "video", "videos", "quotes", "quote", "explained",
    "meaning", "lyrics", "transcript", "wiki", "fandom", "imdb", "youtube",
    "watch", "full", "best", "moments", "moment", "reaction", "review",
    # Runtime/marketing filler that recurs across countless unrelated
    # compilation-style video titles (e.g. "X - 1 Hour Special", "Y Compilation
    # HD"). These words are common to nearly every long-form clip compilation
    # regardless of the actual show/movie, so letting them compete as source-
    # title candidates lets them win purely by repetition. This is the actual
    # root cause behind the "every web series becomes 'hour'" bug: the
    # candidate list treated these exactly like a real title.
    "hour", "hours", "hr", "hrs", "minute", "minutes", "min", "mins",
    "compilation", "compilations", "special", "specials", "marathon",
    "montage", "mashup", "recap", "edit", "edits", "cut", "remastered",
    "remaster", "hd", "4k", "version", "versions", "vol", "volume",
    "part", "parts", "mix", "throwback", "rewind", "tribute", "funniest",
    "hilarious", "top", "ultimate", "latest", "new", "trending", "viral",
}

# Pure duration/quantity phrases ("Hour", "1 Hour", "2 Hours", "30 Minutes")
# are never a show/movie title on their own. Rejected outright as a
# candidate regardless of GENERIC_RESULT_WORDS membership -- this also
# catches numbered forms ("2 Hours") that the bare-word denylist above does not.
DURATION_PHRASE_RE = re.compile(
    r"^\d*\s*(hour|hours|hr|hrs|minute|minutes|min|mins)$", re.I
)

SOURCE_TYPE_RULES = {
    "webseries": {
        "series", "tv series", "television", "episode", "season", "sitcom",
        "web series", "tv show", "series finale", "american television",
    },
    "movie": {
        "movie", "film", "motion picture", "feature film", "filmography",
    },
    "anime": {"anime", "manga", "episode", "season", "crunchyroll"},
    "sports": {
        "nba", "nfl", "fifa", "uefa", "premier league", "ipl", "cricket",
        "football", "basketball", "tennis", "formula 1", "f1", "ufc", "wwe",
        "match", "goal", "highlights", "championship",
    },
    "news": {"news", "breaking", "report", "press conference", "politics", "reuters"},
    "podcast": {"podcast", "episode", "interview", "guest"},
    "music_video": {"music video", "official video", "song", "lyrics", "album"},
    "creator_video": {"creator", "vlog", "mrbeast", "podcast"},
}


def normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def strip_urls(text: Any) -> str:
    """Remove URLs before they ever reach transcript/scene text that later
    feeds keyword extraction. Stripping only the final joined string is not
    enough -- a URL gets tokenized into fragments like "https"/"www" by
    extract_keywords() in seo_generator.py before a final pass would see
    it as a URL shape at all, so it must be removed here, upstream.
    """
    return normalize(URL_RE.sub("", normalize(text)))


def safe_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_sidecar_metadata(video_path: Path) -> dict:
    candidates = [
        video_path.with_suffix(video_path.suffix + ".metadata.json"),
        video_path.with_suffix(video_path.suffix + ".url.txt"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".json":
                return json.loads(path.read_text(encoding="utf-8"))
            return {"webpage_url": path.read_text(encoding="utf-8", errors="ignore").strip()}
        except Exception:
            pass
    return {}


def compact_source_metadata() -> dict:
    """DEPRECATED fallback only.

    Earlier versions of this bot round-tripped YouTube metadata through a
    single unscoped file (cache/youtube_source_metadata.json) that this
    function always read, regardless of which video was actually being
    processed. That meant a local video run (or any run after a YouTube
    download) silently inherited the metadata -- including the title -- of
    whichever YouTube video was downloaded *last*, not the video actually
    being processed. This was the root cause of every source being
    identified from stale, unrelated web-search evidence (see the "hour"
    bug notes in detect_video_source()).

    detect_video_source() no longer calls this; main.py now passes
    source_metadata explicitly. This function is kept only so old cached
    data doesn't break an import, and it deliberately does NOT read the old
    global file anymore.
    """
    return {}


def transcript_text(transcript_data: list[dict]) -> str:
    return normalize(" ".join(s.get("text", "") for s in transcript_data))


def transcript_words(transcript_data: list[dict]) -> list[str]:
    words = []
    for seg in transcript_data:
        for word in seg.get("words", []):
            text = normalize(word.get("text", ""))
            if text:
                words.append(text)
    return words


def distinctive_quotes(text: str, max_quotes: int = 4) -> list[str]:
    """Pick short, information-dense phrases that are useful for web search."""
    sentences = re.split(r"(?<=[.!?])\s+", normalize(text))
    candidates: list[tuple[float, str]] = []
    for sentence in sentences:
        words = re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", sentence)
        if not 7 <= len(words) <= 24:
            continue
        informative = [w.lower() for w in words if w.lower() not in STOPWORDS]
        score = len(set(informative)) * 2 + min(len(words), 18) * 0.4
        if any(w.lower() in {"office", "dwight", "michael", "walter", "jesse", "eleven"} for w in words):
            score += 5
        candidates.append((score, " ".join(words)))

    # Also build sliding phrases when Whisper did not segment punctuation well.
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", text)
    for i in range(0, max(0, len(tokens) - 10), 7):
        chunk = tokens[i:i + 14]
        if len(chunk) < 9:
            continue
        informative = [w.lower() for w in chunk if w.lower() not in STOPWORDS]
        score = len(set(informative)) * 1.8
        candidates.append((score, " ".join(chunk)))

    result = []
    seen = set()
    for _, phrase in sorted(candidates, key=lambda x: x[0], reverse=True):
        key = re.sub(r"[^a-z0-9]", "", phrase.lower())
        if len(key) < 25 or key in seen:
            continue
        seen.add(key)
        result.append(phrase)
        if len(result) >= max_quotes:
            break
    return result


def sample_ocr(video_path: Path, samples: int = 6) -> list[str]:
    if cv2 is None or pytesseract is None:
        return []

    # Allow a normal Windows Tesseract install without forcing a hard dependency.
    if os.name == "nt" and not getattr(pytesseract.pytesseract, "tesseract_cmd", ""):
        common = [
            r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
            r"C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
        ]
        for candidate in common:
            if Path(candidate).exists():
                pytesseract.pytesseract.tesseract_cmd = candidate
                break

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if fps else 0
    times = [duration * i / max(1, samples - 1) for i in range(samples)]
    found: list[str] = []

    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        try:
            # Crop the top and bottom a little less aggressively than the center;
            # logos and subtitles frequently live there.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=1.35, fy=1.35, interpolation=cv2.INTER_CUBIC)
            text = pytesseract.image_to_string(gray, config="--psm 6")
            text = normalize(text)
            if text and len(text) >= 4:
                found.append(text[:500])
        except Exception:
            continue

    cap.release()
    # Deduplicate near-identical OCR strings.
    unique = []
    seen = set()
    for text in found:
        key = re.sub(r"[^a-z0-9]", "", text.lower())
        if key and key not in seen:
            seen.add(key)
            unique.append(text)
    return unique[:8]


def web_search(queries: list[str], max_results_per_query: int = 6) -> list[dict]:
    if DDGS is None:
        return []

    results: list[dict] = []
    seen = set()
    try:
        with DDGS() as ddgs:
            for query in queries:
                query = normalize(query)
                if len(query) < 8:
                    continue
                try:
                    rows = ddgs.text(query, max_results=max_results_per_query)
                except Exception:
                    continue
                for row in rows or []:
                    title = normalize(row.get("title", ""))
                    body = normalize(row.get("body", ""))
                    href = normalize(row.get("href", ""))
                    key = (title.lower(), href.lower())
                    if not title and not body:
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({"title": title, "snippet": body, "url": href, "query": query})
                # Avoid hammering the public search service.
                time.sleep(0.25)
    except Exception:
        return results
    return results[:30]


def clean_candidate(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"\s*[|–—]\s*(IMDb|Fandom|Wikipedia|YouTube|Netflix|HBO|Wiki).*?$", "", text, flags=re.I)
    text = re.sub(r"\s+-\s+(Official|Clip|Scene|Episode|Quotes?).*$", "", text, flags=re.I)
    return text.strip(" -|:;")


def candidate_phrases(result: dict) -> list[str]:
    title = clean_candidate(result.get("title", ""))
    snippet = normalize(result.get("snippet", ""))
    candidates = []

    # Parenthesized media names: The Office (American TV series)
    m = re.match(r"^(.{2,80}?)\s*\((?:TV|film|series|movie|American|British|anime)", title, re.I)
    if m:
        candidates.append(m.group(1).strip())

    # Extract one contiguous capitalized span rather than every possible
    # substring. The old regex generated fragments such as "Met Your" from
    # "How I Met Your Mother", allowing a fragment to out-score the real title.
    for m in re.finditer(
        r"\b(?:[A-Z][\w'’-]+(?:\s+(?:[A-Z][\w'’-]+|I|How|The|A|An|Of|And|In|On|To|Your|My|Your|Met|You|Me)){1,7})\b",
        title,
    ):
        phrase = normalize(m.group(0))
        if len(phrase) >= 4 and not DURATION_PHRASE_RE.match(phrase):
            candidates.append(phrase)

    # Quoted phrases in snippets can be useful evidence, but keep them as
    # complete phrases rather than splitting them into dialogue fragments.
    for m in re.finditer(r'"([^"]{3,70})"', snippet):
        phrase = normalize(m.group(1))
        if not DURATION_PHRASE_RE.match(phrase):
            candidates.append(phrase)

    return list(dict.fromkeys(c for c in candidates if c))

def score_candidates(results: list[dict], filename: str, source_metadata: dict) -> tuple[str, float, list[dict]]:
    scores: defaultdict[str, float] = defaultdict(float)
    evidence: defaultdict[str, list[dict]] = defaultdict(list)

    source_title = normalize(source_metadata.get("title", ""))
    source_desc = normalize(source_metadata.get("description", ""))
    source_tags = source_metadata.get("tags", []) or []
    for seed in [source_title, source_desc] + [str(x) for x in source_tags[:15]]:
        if seed:
            key = clean_candidate(seed)
            if len(key) >= 3:
                scores[key] += 8

    filename_base = re.sub(r"[_\-.]+", " ", Path(filename).stem)
    for result in results:
        candidates = candidate_phrases(result)
        haystack = f"{result.get('title','')} {result.get('snippet','')}".lower()
        for candidate in candidates:
            c = clean_candidate(candidate)
            if len(c) < 3 or len(c) > 80:
                continue
            low = c.lower()
            if low in GENERIC_RESULT_WORDS:
                continue
            score = 1.0
            # Repeated appearance across independent queries is valuable.
            if source_title and low in source_title.lower():
                score += 6
            if low in filename_base.lower():
                score += 5
            if any(word in haystack for word in ["episode", "season", "tv series", "film", "movie", "anime"]):
                score += 2
            scores[c] += score
            evidence[c].append(result)

    if not scores:
        # Fall back to a YouTube source title if it exists.
        if source_title:
            return clean_candidate(source_title), 0.72, []
        return "", 0.0, []

    # A bare single-word candidate ("Hour", "Special", "Marathon"...) is weak
    # evidence of a real show/movie title on its own -- it recurs across
    # countless unrelated compilation-style videos and can otherwise
    # out-vote the real name purely by repetition across many DDGS results.
    # This is the structural fix for that class of bug (of which "hour" is
    # one instance): require independent corroboration -- either the exact
    # word appears in this video's own source metadata/filename (in which
    # case it was already scored via the seed loop above), or it stays
    # heavily discounted. Multi-word phrases are much stronger evidence
    # already and are left alone.
    for candidate in list(scores.keys()):
        if " " in candidate:
            continue
        corroborated = (
            (source_title and candidate.lower() in source_title.lower())
            or candidate.lower() in filename_base.lower()
        )
        if not corroborated:
            scores[candidate] *= 0.2

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = max(0.0, best_score - second)
    confidence = min(0.97, 0.35 + best_score / 30.0 + margin / 25.0)
    if best_score < 3:
        confidence *= 0.65

    best_evidence = evidence[best][:8]
    return best, confidence, best_evidence


def classify_source(title: str, results: list[dict], source_metadata: dict) -> str:
    blob = " ".join([
        title,
        normalize(source_metadata.get("title", "")),
        normalize(source_metadata.get("description", "")),
        " ".join(str(x) for x in source_metadata.get("tags", [])[:20]),
        " ".join(normalize(r.get("title", "")) for r in results[:20]),
        " ".join(normalize(r.get("snippet", "")) for r in results[:20]),
    ]).lower()

    scores = {}
    for source_type, terms in SOURCE_TYPE_RULES.items():
        scores[source_type] = sum(1 for term in terms if term in blob)

    if title:
        # A named series with episode/season evidence is most likely a webseries.
        if scores.get("webseries", 0) >= 2:
            return "webseries"
        if scores.get("anime", 0) >= 2:
            return "anime"
        if scores.get("movie", 0) >= 2:
            return "movie"

    best_type = max(scores, key=scores.get) if scores else "unknown"
    return best_type if scores.get(best_type, 0) >= 2 else "unknown"


def extract_episode(text_blob: str) -> dict:
    blob = normalize(text_blob)
    season = None
    episode = None
    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,2})\b", blob, re.I)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    if season is None:
        m = re.search(r"season\s+(\d{1,2}).{0,25}(?:episode|ep)\s*(\d{1,2})", blob, re.I)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    return {"season": season, "episode": episode}


def likely_characters(results: list[dict], source_title: str, min_mentions: int = 2) -> list[str]:
    """Only return names with real corroboration across independent results.

    A name mentioned exactly once in one search result is frequently a
    reviewer, actor, or unrelated proper noun picked up by the naive
    two-capitalized-word regex below -- not a confirmed character in the
    clip. Requiring the same name to recur across at least min_mentions
    independent results is a much weaker guarantee than verifying cast
    lists, but it is meaningfully better than emitting every incidental
    capitalized name pair, and it means an uncorroborated guess is dropped
    (characters = []) rather than fabricated.
    """
    names = Counter()
    for result in results:
        text = f"{result.get('title','')} {result.get('snippet','')}"
        # Conservative two-token proper-name extraction.
        for m in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", text):
            name = f"{m.group(1)} {m.group(2)}"
            if name.lower() == source_title.lower():
                continue
            names[name] += 1
    return [name for name, count in names.most_common(8) if count >= min_mentions]


def scene_summary(transcript_data: list[dict], results: list[dict], source_title: str) -> str:
    text = transcript_text(transcript_data)
    # Prefer a concise beginning because clips are selected from the transcript,
    # but retain web evidence when it gives explicit scene context.
    sentences = [normalize(x) for x in re.split(r"(?<=[.!?])\s+", text) if normalize(x)]
    base = " ".join(sentences[:3])[:500]
    web_bits = []
    for result in results[:5]:
        snippet = normalize(result.get("snippet", ""))
        if source_title.lower() in snippet.lower() or source_title.lower() in result.get("title", "").lower():
            if snippet and snippet not in web_bits:
                web_bits.append(snippet[:240])
    if web_bits:
        return strip_urls(f"{base} {' '.join(web_bits[:2])}")[:800]
    return strip_urls(base)


def confidence_tier(confidence: float) -> str:
    """Coarse HIGH/MEDIUM/LOW label so downstream code and logs never have
    to guess what a raw float means. Thresholds match source_is_usable()'s
    0.55 cutoff in seo_generator.py.
    """
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.55:
        return "medium"
    return "low"


def detect_video_source(
    video_path: Path,
    transcript_data: list[dict],
    source_url: str = "",
    source_metadata: dict | None = None,
    output_path: Path | None = None,
) -> dict:
    video_path = Path(video_path)
    # source_metadata is passed explicitly by the caller (main.py) for the
    # CURRENT video only -- e.g. the compact yt-dlp metadata dict returned
    # directly from download_youtube(), or {} for a local video. This is
    # deliberately NOT read from any shared/global file: an earlier version
    # read a single unscoped cache/youtube_source_metadata.json here, which
    # let one run's title/description/tags leak into a later, unrelated
    # run (the root cause of the "every series identified as 'hour'" bug --
    # see detect_video_source's module-level notes near compact_source_metadata).
    source_metadata = dict(source_metadata or {})
    sidecar = load_sidecar_metadata(video_path)
    source_metadata = {**sidecar, **source_metadata}
    if source_url:
        source_metadata.setdefault("webpage_url", source_url)

    text = transcript_text(transcript_data)
    quotes = distinctive_quotes(text)
    ocr = sample_ocr(video_path)

    queries = []
    if source_metadata.get("title"):
        queries.append(f'"{normalize(source_metadata["title"])}"')
    if source_metadata.get("webpage_url") and source_metadata.get("title"):
        queries.append(f'"{normalize(source_metadata["title"])}" cast episode')
    for quote in quotes[:3]:
        queries.append(f'"{quote}" TV movie episode scene')
    for ocr_text in ocr[:2]:
        words = re.findall(r"[A-Za-z][A-Za-z0-9'’-]{2,}", ocr_text)
        useful = [w for w in words if w.lower() not in STOPWORDS]
        if len(useful) >= 2:
            queries.append('"' + " ".join(useful[:10]) + '" show movie')
    if not queries:
        queries.append(f'"{video_path.stem.replace("_", " ")}" video')

    results = web_search(queries[:6])
    title, confidence, evidence = score_candidates(results, video_path.name, source_metadata)

    # Original YouTube metadata is strong evidence, but its title may be a
    # clip/uploader title rather than the underlying show or movie. Prefer a
    # web-verified entity when one exists; otherwise keep the original title as
    # a lower-confidence fallback.
    metadata_title = clean_candidate(normalize(source_metadata.get("title", "")))
    if metadata_title and source_url and (not title or confidence < 0.55):
        title = metadata_title
        confidence = max(confidence, 0.68)

    blob = " ".join([
        title,
        metadata_title,
        text[:2000],
        " ".join(ocr),
        " ".join(r.get("title", "") for r in results),
        " ".join(r.get("snippet", "") for r in results),
    ])
    ep = extract_episode(blob)
    source_type = classify_source(title, results, source_metadata)
    tier = confidence_tier(confidence)
    # Don't attach character names to a source we're not even confident we
    # identified correctly -- an uncorroborated show name plus uncorroborated
    # character names compounds into fabricated-looking SEO. Better to emit
    # characters = [] here (per the "do not hallucinate characters" requirement)
    # than guess on top of a guess.
    characters = likely_characters(results, title) if tier != "low" else []

    # SEO keywords must come from source/entity evidence, not arbitrary words
    # spoken in the transcript. Dialogue is still retained separately in
    # transcript_quotes and scene_summary.
    keyword_blob = " ".join([
        title,
        metadata_title,
        normalize(source_metadata.get("description", "")),
        " ".join(str(x) for x in source_metadata.get("tags", [])[:20]),
        " ".join(normalize(r.get("title", "")) for r in results[:10]),
        " ".join(normalize(r.get("snippet", "")) for r in results[:10]),
    ])
    keywords = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9'’-]{3,}", keyword_blob.lower()):
        if token in STOPWORDS or token in GENERIC_RESULT_WORDS:
            continue
        if token not in keywords:
            keywords.append(token)
        if len(keywords) >= 20:
            break

    evidence_out = []
    for item in evidence:
        evidence_out.append({
            "title": item.get("title", ""),
            "snippet": item.get("snippet", "")[:500],
            "url": item.get("url", ""),
            "query": item.get("query", ""),
        })

    context = {
        "source_type": source_type,
        "title": title,
        "confidence": round(float(confidence), 3),
        "confidence_tier": confidence_tier(confidence),
        "source_url": source_metadata.get("webpage_url") or source_url,
        "source_channel": source_metadata.get("channel") or source_metadata.get("uploader", ""),
        "source_description": normalize(source_metadata.get("description", ""))[:2000],
        "source_tags": source_metadata.get("tags", [])[:50],
        "season": ep["season"],
        "episode": ep["episode"],
        "characters": characters,
        "scene_summary": scene_summary(transcript_data, results, title),
        "transcript_quotes": quotes,
        "ocr_clues": ocr,
        "keywords": keywords,
        "evidence": evidence_out,
        "detection_method": [
            "youtube_metadata" if source_metadata.get("title") else None,
            "web_search" if results else None,
            "ocr" if ocr else None,
            "transcript_quotes" if quotes else None,
        ],
    }
    context["detection_method"] = [x for x in context["detection_method"] if x]

    if output_path:
        safe_json(Path(output_path), context)
    return context
