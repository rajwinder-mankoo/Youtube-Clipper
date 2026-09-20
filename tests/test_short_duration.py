import unittest

from youtube_clipper.video.framing import normalize_keyframes
from youtube_clipper.video.limits import (
    MAX_SHORT_DURATION_SECONDS,
    cap_intervals,
)


class ShortDurationTests(unittest.TestCase):
    def test_limit_is_strictly_under_one_minute(self):
        self.assertLess(MAX_SHORT_DURATION_SECONDS, 60)

    def test_intervals_are_trimmed_to_duration_limit(self):
        intervals = [(10, 40), (45, 80), (90, 100)]
        capped = cap_intervals(intervals)
        duration = sum(end - start for start, end in capped)
        self.assertEqual(capped, [(10.0, 40.0), (45.0, 74.0)])
        self.assertEqual(duration, MAX_SHORT_DURATION_SECONDS)

    def test_short_intervals_are_unchanged(self):
        self.assertEqual(cap_intervals([(2, 12), (15, 20)]), [(2.0, 12.0), (15.0, 20.0)])

    def test_keyframes_cannot_extend_past_render_limit(self):
        frames = normalize_keyframes(
            [{"time": 90, "center_x": 0.5, "center_y": 0.5, "zoom": 1}],
            duration=MAX_SHORT_DURATION_SECONDS,
        )
        self.assertEqual(frames[-1]["time"], MAX_SHORT_DURATION_SECONDS)


if __name__ == "__main__":
    unittest.main()
