import unittest

from modules.srt_transform_engine import SrtTransformConfig, SrtTransformEngine


class SrtTransformEngineTests(unittest.TestCase):
    def test_clean_hallucinations_filters_credit_and_noise_tags(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': 'Transcription by CastingWords'},
            {'start': 1.0, 'end': 2.0, 'text': '[Music]'},
            {'start': 2.0, 'end': 3.0, 'text': '正常字幕内容'},
        ]

        cleaned = engine.clean_hallucinations(cues)

        self.assertEqual([cue['text'] for cue in cleaned], ['正常字幕内容'])

    def test_resolve_overlaps_merges_continuation_text(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=40, max_lines=2))
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'hello world'},
            {'start': 1.8, 'end': 3.0, 'text': 'world again'},
        ]

        resolved = engine.resolve_overlaps(cues, total_duration_s=4.0)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]['text'], 'hello world again')
        self.assertAlmostEqual(resolved[0]['end'], 3.0)

    def test_resolve_overlaps_keeps_minimum_visible_duration(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': 'first'},
            {'start': 0.8, 'end': 1.6, 'text': 'second'},
        ]

        resolved = engine.resolve_overlaps(cues, total_duration_s=2.0)

        self.assertEqual(len(resolved), 2)
        self.assertGreaterEqual(resolved[0]['end'] - resolved[0]['start'], 0.05)
        self.assertGreaterEqual(resolved[1]['end'] - resolved[1]['start'], 0.05)
        self.assertLessEqual(resolved[0]['end'], resolved[1]['start'])

    def test_remove_rollup_duplicates_drops_micro_cues_without_losing_text(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.320, 'end': 3.669, 'text': 'This is a $100 million fishing'},
            {'start': 3.669, 'end': 3.679, 'text': 'This is a $100 million fishing'},
            {
                'start': 3.679,
                'end': 6.470,
                'text': 'This is a $100 million fishing operation. It includes three super',
            },
            {'start': 6.470, 'end': 6.480, 'text': 'operation. It includes three super'},
            {
                'start': 6.480,
                'end': 9.669,
                'text': 'operation. It includes three super yachts, private jets, helicopters, and',
            },
        ]

        cleaned = engine.remove_rollup_duplicates(cues)

        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]['text'], 'This is a $100 million fishing')
        self.assertEqual(
            cleaned[1]['text'],
            'This is a $100 million fishing operation. It includes three super',
        )
        self.assertEqual(
            cleaned[2]['text'],
            'operation. It includes three super yachts, private jets, helicopters, and',
        )

    def test_split_long_cue_uses_visual_units_for_cjk_text(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=6, max_lines=2))
        cue = {
            'start': 0.0,
            'end': 6.0,
            'text': '这是第一句话。这是第二句话。这是第三句话。',
        }

        split = engine.split_long_cue(cue)

        self.assertGreater(len(split), 1)
        self.assertEqual(split[0]['start'], 0.0)
        self.assertEqual(split[-1]['end'], 6.0)


if __name__ == '__main__':
    unittest.main()
