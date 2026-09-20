import unittest

from youtube_clipper.video.trimming import (
    normalize_trim,
    trim_keyframes,
    trim_timeline,
)


class TrimmingTests(unittest.TestCase):
    def test_trim_must_keep_one_second(self):
        with self.assertRaisesRegex(ValueError, "one second"):
            normalize_trim(2, 2.5, 10)

    def test_trim_stays_inside_video(self):
        self.assertEqual(normalize_trim(-5, 20, 10), (0.0, 10.0))

    def test_timeline_preserves_original_source_mapping(self):
        timeline = [
            {"source_start": 10, "source_end": 20, "output_start": 0},
            {"source_start": 30, "source_end": 40, "output_start": 10},
        ]
        self.assertEqual(trim_timeline(timeline, 5, 16), [
            {"source_start": 15.0, "source_end": 20.0, "output_start": 0},
            {"source_start": 30.0, "source_end": 36.0, "output_start": 5},
        ])

    def test_keyframes_shift_to_trimmed_timeline(self):
        frames = [
            {"time": 0, "center_x": 0.2, "center_y": 0.5, "zoom": 1},
            {"time": 10, "center_x": 0.8, "center_y": 0.5, "zoom": 2},
            {"time": 20, "center_x": 0.5, "center_y": 0.5, "zoom": 1},
        ]
        trimmed = trim_keyframes(frames, 5, 15)
        self.assertEqual(trimmed[0]["time"], 0)
        self.assertAlmostEqual(trimmed[0]["center_x"], 0.5)
        self.assertEqual(trimmed[1]["time"], 5)


if __name__ == "__main__":
    unittest.main()
