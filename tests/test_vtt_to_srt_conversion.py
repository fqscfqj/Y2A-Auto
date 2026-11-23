import ast
import html
import pathlib
import re
import unittest


def _load_function(name):
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"
    source = module_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(module_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {"html": html, "re": re}
    exec(compile(isolated_module, str(module_path), "exec"), namespace)
    return namespace[name]


class VttToSrtConversionTests(unittest.TestCase):
    def test_converts_regular_vtt_blocks_and_cleans_tags(self):
        convert = _load_function("_convert_vtt_text_to_srt_text")
        vtt_text = """WEBVTT
Kind: captions
Language: en

caption-1
00:00:00.000 --> 00:00:02.000 align:start position:0%
Line 1
Line 2

NOTE This should be ignored

00:00:02.500 --> 00:00:04.000
<c.green>World</c>
"""

        srt_text = convert(vtt_text)

        self.assertEqual(
            srt_text,
            """1
00:00:00,000 --> 00:00:02,000
Line 1
Line 2

2
00:00:02,500 --> 00:00:04,000
World""",
        )

    def test_merges_youtube_auto_caption_pair_into_single_srt_cue(self):
        convert = _load_function("_convert_vtt_text_to_srt_text")
        vtt_text = """WEBVTT

00:00:00.000 --> 00:00:01.000 align:start position:0%
<c>Hel</c><00:00:00.500><c>lo</c>

00:00:01.000 --> 00:00:01.050 align:start position:0%
Hello

00:00:02.000 --> 00:00:03.000
Next line
"""

        srt_text = convert(vtt_text)

        self.assertEqual(
            srt_text,
            """1
00:00:00,000 --> 00:00:01,050
Hello

2
00:00:02,000 --> 00:00:03,000
Next line""",
        )


    def test_keeps_full_text_when_short_rollup_cue_follows_long_cue(self):
        convert = _load_function("_convert_vtt_text_to_srt_text")
        vtt_text = """WEBVTT

00:00:00.320 --> 00:00:03.669 align:start position:0%
<c>This is a $100 million fishing</c>

00:00:03.669 --> 00:00:03.679 align:start position:0%
This is a $100 million fishing

00:00:03.679 --> 00:00:06.470 align:start position:0%
<c>This is a $100 million fishing</c><00:00:04.000><c> operation. It includes three super</c>

00:00:06.470 --> 00:00:06.480 align:start position:0%
operation. It includes three super

00:00:06.480 --> 00:00:09.669 align:start position:0%
<c>operation. It includes three super yachts, private jets, helicopters, and</c>
"""

        srt_text = convert(vtt_text)

        self.assertEqual(
            srt_text,
            """1
00:00:00,320 --> 00:00:03,679
This is a $100 million fishing

2
00:00:03,679 --> 00:00:06,480
This is a $100 million fishing operation. It includes three super

3
00:00:06,480 --> 00:00:09,669
operation. It includes three super yachts, private jets, helicopters, and""",
        )


if __name__ == "__main__":
    unittest.main()
