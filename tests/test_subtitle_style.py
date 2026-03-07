import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.task_manager import TaskProcessor


class SubtitleStyleTests(unittest.TestCase):
    def test_landscape_style_remains_close_to_existing_defaults(self):
        style = TaskProcessor._build_streaming_ass_style(1920, 1080)

        self.assertEqual(style["PlayResX"], 1920)
        self.assertEqual(style["PlayResY"], 1080)
        self.assertAlmostEqual(style["FontSize"], 42.0)
        self.assertAlmostEqual(style["MarginV"], 60.0)
        self.assertAlmostEqual(style["MarginL"], 96.0)
        self.assertAlmostEqual(style["MarginR"], 96.0)
        self.assertEqual(style["Alignment"], 2)

    def test_portrait_style_uses_safe_zone_defaults(self):
        style = TaskProcessor._build_streaming_ass_style(1080, 1920)

        self.assertEqual(style["PlayResX"], 1080)
        self.assertEqual(style["PlayResY"], 1920)
        self.assertGreater(style["FontSize"], 50.0)
        self.assertGreater(style["MarginV"], 200.0)
        self.assertGreater(style["MarginL"], 100.0)
        self.assertGreater(style["MarginR"], 100.0)
        self.assertEqual(style["Alignment"], 2)
        self.assertAlmostEqual(style["FontSize"], 57.6, places=1)
        self.assertAlmostEqual(style["MarginV"], 259.2, places=1)

    def test_four_by_five_uses_portrait_branch(self):
        style = TaskProcessor._build_streaming_ass_style(1080, 1350)

        self.assertEqual(style["PlayResX"], 1080)
        self.assertEqual(style["PlayResY"], 1350)
        self.assertEqual(style["FontSize"], 44.0)
        self.assertAlmostEqual(style["MarginV"], 182.25, places=2)
        self.assertAlmostEqual(style["MarginL"], 118.8, places=1)
        self.assertAlmostEqual(style["MarginR"], 118.8, places=1)

    def test_ass_document_contains_portrait_safe_area_values(self):
        ass_content = TaskProcessor._build_default_ass_document(
            cues=[{"start": 0.0, "end": 1.5, "text": "hola mundo"}],
            font_family="Source Han Sans",
            video_width=1080,
            video_height=1920,
        )

        self.assertIn("PlayResX: 1080", ass_content)
        self.assertIn("PlayResY: 1920", ass_content)
        self.assertIn("Style: Default,Source Han Sans,57.6,", ass_content)
        self.assertIn(",3.46,1.15,2,119,119,259,1", ass_content)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,hola mundo", ass_content)

    def test_force_style_contains_portrait_safe_area_fields(self):
        force_style = TaskProcessor._build_subtitle_force_style(
            "Source Han Sans",
            1080,
            1920,
        )

        self.assertTrue(force_style.startswith("force_style='"))
        self.assertTrue(force_style.endswith("'"))

        payload = force_style[len("force_style='"):-1]
        style_map = {}
        for part in payload.split(","):
            key, value = part.split("=", 1)
            style_map[key] = value

        self.assertEqual(style_map["FontName"], "Source Han Sans")
        self.assertEqual(style_map["FontSize"], "57.6")
        self.assertEqual(style_map["Outline"], "3.46")
        self.assertEqual(style_map["Shadow"], "1.15")
        self.assertEqual(style_map["MarginL"], "119")
        self.assertEqual(style_map["MarginR"], "119")
        self.assertEqual(style_map["MarginV"], "259")
        self.assertEqual(style_map["Alignment"], "2")


if __name__ == "__main__":
    unittest.main()
