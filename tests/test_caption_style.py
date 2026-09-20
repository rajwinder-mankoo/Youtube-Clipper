import unittest
from types import SimpleNamespace

from youtube_clipper.video.captions import caption_style, normalize_caption_style


class CaptionStyleTests(unittest.TestCase):
    def test_style_values_are_normalized(self):
        style = normalize_caption_style({
            "font_name": "Montserrat",
            "font_size": "52",
            "text_color": "#f4d35e",
            "outline_color": "#101010",
            "outline": "4",
            "shadow": 2,
            "position": "middle",
            "margin": "120",
        })
        self.assertEqual(style["font_name"], "Montserrat")
        self.assertEqual(style["font_size"], 52)
        self.assertEqual(style["text_color"], "#F4D35E")
        self.assertEqual(style["position"], "middle")

    def test_invalid_colors_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "six-digit color"):
            normalize_caption_style({"text_color": "white"})

    def test_unsafe_font_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "font"):
            normalize_caption_style({"font_name": r"Arial{\\bad}"})

    def test_legacy_zero_margin_uses_safe_default(self):
        color = SimpleNamespace(r=255, g=255, b=255)
        style = SimpleNamespace(
            fontname="Arial", fontsize=20, primarycolor=color,
            outlinecolor=SimpleNamespace(r=0, g=0, b=0), outline=2,
            shadow=0, alignment=2, marginv=0,
        )
        subtitles = SimpleNamespace(styles={"Default": style})
        self.assertEqual(caption_style(subtitles)["margin"], 150)


if __name__ == "__main__":
    unittest.main()
