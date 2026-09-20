"""
Regression test for:
  - metadata isolation between two different clips/sources (no cross-contamination)
  - the "hour" bug (generic/duration words must not win source identification)

Run: python3 test_isolation.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from youtube_clipper.metadata.seo import generate_metadata
from youtube_clipper.publishing.policy import publishing_action, unattended_upload_enabled
from youtube_clipper.publishing.payload import build_video_insert_body

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILED = []

def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILED.append(label)


# ------------------------------------------------------------------
# TEST 1: two completely different clips/sources must never share fields
# ------------------------------------------------------------------
print("\n=== TEST 1: Cross-contamination (different sources) ===")

Path("cache").mkdir(exist_ok=True)
Path("cache/source_context_alpha.json").write_text("""
{
  "source_type": "webseries", "title": "Series Alpha", "confidence": 0.9,
  "season": 1, "episode": 1, "characters": ["Alpha One", "Alpha Two"],
  "scene_summary": "Alpha One confronts Alpha Two about the missing files.",
  "keywords": ["alpha", "confrontation", "office"], "evidence": []
}
""", encoding="utf-8")

Path("cache/source_context_beta.json").write_text("""
{
  "source_type": "movie", "title": "Series Beta", "confidence": 0.9,
  "season": null, "episode": null, "characters": ["Beta One", "Beta Two"],
  "scene_summary": "Beta One and Beta Two escape the collapsing building.",
  "keywords": ["beta", "escape", "action"], "evidence": []
}
""", encoding="utf-8")

clip_a = {"start": 0, "end": 20, "text": "Alpha One confronts Alpha Two about the files.",
          "source_context_file": "cache/source_context_alpha.json"}
clip_b = {"start": 0, "end": 20, "text": "Beta One and Beta Two escape the building.",
          "source_context_file": "cache/source_context_beta.json"}

transcript_a = [{"text": clip_a["text"], "words": []}]
transcript_b = [{"text": clip_b["text"], "words": []}]

meta_a = generate_metadata(clip_a, 1, transcript_a, Path("."), privacy_status="private")
meta_b = generate_metadata(clip_b, 1, transcript_b, Path("."), privacy_status="private")

check("Clip A title mentions Series Alpha", "Alpha" in meta_a["title"])
check("Clip A title does NOT mention Series Beta", "Beta" not in meta_a["title"])
check("Clip B title mentions Series Beta", "Beta" in meta_b["title"])
check("Clip B title does NOT mention Series Alpha", "Alpha" not in meta_b["title"])

check("Clip A tags contain Alpha One", any("Alpha One" in t for t in meta_a["tags"]))
check("Clip A tags do NOT contain any Beta character", not any("Beta" in t for t in meta_a["tags"]))
check("Clip B tags contain Beta One", any("Beta One" in t for t in meta_b["tags"]))
check("Clip B tags do NOT contain any Alpha character", not any("Alpha" in t for t in meta_b["tags"]))

check("Clip A description does not mention Beta", "Beta" not in meta_a["description"])
check("Clip B description does not mention Alpha", "Alpha" not in meta_b["description"])


# ------------------------------------------------------------------
# TEST 2: two clips from the SAME series still get distinct, scene-specific SEO
# ------------------------------------------------------------------
print("\n=== TEST 2: Same series, different scenes/characters ===")

Path("cache/source_context_x.json").write_text("""
{
  "source_type": "webseries", "title": "Series X", "confidence": 0.9,
  "season": 2, "episode": 5, "characters": ["Character A"],
  "scene_summary": "Character A gives a passionate speech about honesty.",
  "keywords": ["honesty", "speech"], "evidence": []
}
""", encoding="utf-8")
Path("cache/source_context_x2.json").write_text("""
{
  "source_type": "webseries", "title": "Series X", "confidence": 0.9,
  "season": 2, "episode": 6, "characters": ["Character B"],
  "scene_summary": "Character B gets caught in an embarrassing lie.",
  "keywords": ["lie", "embarrassing"], "evidence": []
}
""", encoding="utf-8")

clip_x1 = {"start": 0, "end": 20, "text": "Character A gives a passionate speech about honesty right now.",
           "source_context_file": "cache/source_context_x.json"}
clip_x2 = {"start": 0, "end": 20, "text": "Character B gets caught in an embarrassing lie today.",
           "source_context_file": "cache/source_context_x2.json"}

meta_x1 = generate_metadata(clip_x1, 1, [{"text": clip_x1["text"], "words": []}], Path("."), privacy_status="private")
meta_x2 = generate_metadata(clip_x2, 2, [{"text": clip_x2["text"], "words": []}], Path("."), privacy_status="private")

check("Both clips share the series name", "Series X" in meta_x1["title"] and "Series X" in meta_x2["title"])
check("Clip X1 tags mention Character A", any("Character A" in t for t in meta_x1["tags"]))
check("Clip X2 tags mention Character B", any("Character B" in t for t in meta_x2["tags"]))
check("Clip X1 tags do NOT mention Character B", not any("Character B" in t for t in meta_x1["tags"]))
check("Clip X2 tags do NOT mention Character A", not any("Character A" in t for t in meta_x2["tags"]))
check("Descriptions differ between the two clips", meta_x1["description"] != meta_x2["description"])


# ------------------------------------------------------------------
# TEST 3: source URL must never leak into public metadata
# ------------------------------------------------------------------
print("\n=== TEST 3: Source URL isolation ===")
Path("cache/source_context_url.json").write_text("""
{
  "source_type": "webseries", "title": "Series URL Test", "confidence": 0.9,
  "season": 1, "episode": 1, "characters": [],
  "scene_summary": "Check out https://www.youtube.com/watch?v=abc123 for more like this.",
  "keywords": ["test"], "evidence": []
}
""", encoding="utf-8")
clip_url = {"start": 0, "end": 10, "text": "This is a normal line of dialogue.",
            "source_context_file": "cache/source_context_url.json"}
meta_url = generate_metadata(clip_url, 1, [{"text": clip_url["text"], "words": []}], Path("."), privacy_status="private")

check("No URL in title", "http" not in meta_url["title"] and "www." not in meta_url["title"])
check("No URL in description", "http" not in meta_url["description"] and "www." not in meta_url["description"])
check("No URL in any tag", not any(("http" in t or "www." in t) for t in meta_url["tags"]))


# ------------------------------------------------------------------
# TEST 4: the "hour" bug -- generic/duration words must never win identification
# ------------------------------------------------------------------
print("\n=== TEST 4: 'hour' / duration-word regression ===")
from youtube_clipper.metadata.source_detector import (
    DURATION_PHRASE_RE,
    GENERIC_RESULT_WORDS,
    score_candidates,
)

check('"hour" is in the noise-word filter', "hour" in GENERIC_RESULT_WORDS)
check('"hours" is in the noise-word filter', "hours" in GENERIC_RESULT_WORDS)
check('DURATION_PHRASE_RE matches "Hour"', bool(DURATION_PHRASE_RE.match("Hour")))
check('DURATION_PHRASE_RE matches "2 Hours"', bool(DURATION_PHRASE_RE.match("2 Hours")))
check('DURATION_PHRASE_RE does NOT match "The Office"', not DURATION_PHRASE_RE.match("The Office"))

# Simulate a search-result batch dominated by compilation-title noise (as if
# stale metadata had biased the queries) plus one genuine, less-repeated
# real title, across THREE different fake "sources" -- exactly the "every
# series becomes hour" scenario described in the bug report.
def fake_results(real_title, real_mentions=2, noise_mentions=8):
    results = []
    for _ in range(noise_mentions):
        results.append({"title": f"Best Clips - {real_mentions + 3} Hour Compilation HD",
                         "snippet": "Watch the full hour long compilation now.", "url": "", "query": ""})
    for _ in range(real_mentions):
        results.append({"title": f"{real_title} (TV series) - Full Scene",
                         "snippet": f"A clip from {real_title} discussed online.", "url": "", "query": ""})
    return results

for fake_source_name in ["Series Gamma", "Series Delta", "Series Epsilon"]:
    results = fake_results(fake_source_name)
    title, confidence, _ = score_candidates(results, "randomfile.mp4", {})
    check(f'"{fake_source_name}" scenario does NOT resolve to "hour"', title.strip().lower() != "hour")
    check(f'"{fake_source_name}" scenario does NOT resolve to a bare duration phrase',
          not DURATION_PHRASE_RE.match(title.strip()))


# ------------------------------------------------------------------
# TEST 5: upload safety and publishing modes
# ------------------------------------------------------------------
print("\n=== TEST 5: Upload policy safety ===")

check("Unattended upload is disabled by default", not unattended_upload_enabled(None))
check("Explicit unattended upload opt-in works", unattended_upload_enabled("1"))
check("Explicit false overrides a true configured default", not unattended_upload_enabled("0", True))
check("Private mode keeps every upload private", publishing_action("private", 0, 2) == ("private", False))
check("Public mode publishes every upload", publishing_action("public", 0, 2) == ("public", False))
check("Scheduled mode schedules older uploads", publishing_action("scheduled", 0, 2) == ("private", True))
check("Scheduled mode publishes only the newest upload now", publishing_action("scheduled", 1, 2) == ("public", False))

scheduled_metadata = {
    "title": "Example",
    "description": "Example description",
    "tags": ["example"],
    "categoryId": "22",
    "defaultLanguage": "en",
    "privacyStatus": "private",
}
scheduled_time = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)
scheduled_body = build_video_insert_body(scheduled_metadata, scheduled_time)
check("API payload preserves private scheduling", scheduled_body["status"] == {
    "privacyStatus": "private",
    "selfDeclaredMadeForKids": False,
    "publishAt": "2030-01-02T03:04:00Z",
})


# ------------------------------------------------------------------
# TEST 6: manual framing geometry
# ------------------------------------------------------------------
print("\n=== TEST 6: Manual framing geometry ===")
from youtube_clipper.video.framing import (
    crop_geometry,
    interpolate_framing,
    normalize_keyframes,
)

check(
    "Landscape zoom 1 keeps the complete source height",
    crop_geometry(1920, 1080, 0.5, 0.5, 1.0) == (656, 0, 608, 1080),
)
check(
    "Landscape crop is clamped at the left edge",
    crop_geometry(1920, 1080, 0.0, 0.5, 1.0) == (0, 0, 608, 1080),
)
check(
    "A native 9:16 source remains completely visible at zoom 1",
    crop_geometry(1080, 1920, 0.5, 0.5, 1.0) == (0, 0, 1080, 1920),
)
zoomed = crop_geometry(1920, 1080, 0.5, 0.5, 2.0)
check("Zoom 2 halves both crop dimensions", zoomed[2:] == (304, 540))
try:
    crop_geometry(1920, 1080, "nan", 0.5, 1.0)
    rejected_invalid_framing = False
except ValueError:
    rejected_invalid_framing = True
check("Invalid framing values are rejected", rejected_invalid_framing)
keyframes = normalize_keyframes([
    {"time": 0, "center_x": 0.2, "center_y": 0.5, "zoom": 1},
    {"time": 5, "center_x": 0.8, "center_y": 0.5, "zoom": 2},
])
midpoint = interpolate_framing(keyframes, 2.5)
check("Keyframe interpolation moves horizontally", abs(midpoint[0] - 0.5) < 0.0001)
check("Keyframe interpolation changes zoom", abs(midpoint[2] - 1.5) < 0.0001)
check("Keyframes hold their final position", interpolate_framing(keyframes, 8) == (0.8, 0.5, 2.0))
deduped_keyframes = normalize_keyframes([
    {"time": 2, "center_x": 0.2, "center_y": 0.5, "zoom": 1},
    {"time": 2, "center_x": 0.9, "center_y": 0.5, "zoom": 1},
])
check("Duplicate keyframe times use the latest edit", deduped_keyframes[-1]["center_x"] == 0.9)
check("A starting keyframe is inserted automatically", deduped_keyframes[0]["time"] == 0)


# ------------------------------------------------------------------
# TEST 7: tag quality and diversity
# ------------------------------------------------------------------
print("\n=== TEST 7: Tag quality and diversity ===")
from youtube_clipper.metadata.seo import tags_are_near_duplicates
from youtube_clipper.publishing.youtube import validate_and_normalize_tags as validate_upload_tags

Path("cache/source_context_clickbait.json").write_text("""
{
  "source_type": "webseries",
  "title": "INSANE Details You Probably Missed In The TV Show",
  "confidence": 0.93,
  "characters": ["Jordan Lee"],
  "keywords": ["insane", "details", "mystery", "detective", "evidence"]
}
""", encoding="utf-8")
clickbait_clip = {
    "start": 0,
    "end": 20,
    "text": "Jordan finally finds the evidence hidden in the room.",
    "source_context_file": "cache/source_context_clickbait.json",
}
clickbait_meta = generate_metadata(
    clickbait_clip,
    1,
    [{"text": clickbait_clip["text"], "words": []}],
    Path("."),
    privacy_status="private",
)
quality_tags = clickbait_meta["tags"]
check("Clickbait source title is not used as a tag", not any("probably missed" in tag.casefold() for tag in quality_tags))
check("Noise words are excluded from standalone tags", not any(tag.casefold() in {"insane", "details", "missed", "probably"} for tag in quality_tags))
check("Evidence-backed character remains available", "Jordan Lee" in quality_tags)
check("Discovery tag remains available", "YouTube Shorts" in quality_tags)
check("Tag list stays compact", len(quality_tags) <= 15)
check(
    "No near-duplicate tag variants remain",
    not any(
        tags_are_near_duplicates(left, right)
        for index, left in enumerate(quality_tags)
        for right in quality_tags[index + 1:]
    ),
)
upload_tags = validate_upload_tags(
    ["Series Alpha", "Series Alpha clips", "Series Alpha scenes", "YouTube Shorts", "TV clips"],
    allowed_phrases=["Series Alpha", "YouTube Shorts"],
)
check("Final upload guard removes repetitive variants", upload_tags == ["Series Alpha", "YouTube Shorts", "TV clips"])


print()
print("=" * 60)
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED:")
    for f in FAILED:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
