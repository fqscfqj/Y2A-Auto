"""Focused robustness tests for AI segmentation input/output boundaries."""

import math
import unittest
from unittest.mock import MagicMock, patch

from modules.ai_segmentation import (
    AISegmenter,
    AISegmentationConfig,
    _Batch,
    _find_best_fallback_split,
    _parse_cues_response,
    _parse_index_ranges,
    build_batches,
    enforce_rhythm,
)
from modules.subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
    DetectedSpeechWindow,
)


class SegmentationRobustnessTests(unittest.TestCase):
    def _result(self, segment, start=0.0, end=2.0, metadata=None):
        return AsrTranscriptionResult(
            provider='test', response_format='json', timestamp_mode='segment',
            segments=[segment],
            window=DetectedSpeechWindow(
                start_s=start, end_s=end,
                ownership_start_s=start, ownership_end_s=end,
            ),
            metadata=metadata or {},
        )

    def test_config_rejects_nonfinite_and_unbounded_values(self):
        cfg = AISegmentationConfig.from_app_config({
            'AI_SEGMENTATION_MIN_CUE_DURATION_S': float('nan'),
            'AI_SEGMENTATION_MAX_CUE_DURATION_S': float('inf'),
            'AI_SEGMENTATION_MAX_CPS': float('inf'),
            'AI_SEGMENTATION_BATCH_WINDOW_S': float('inf'),
            'AI_SEGMENTATION_MAX_CHARS_PER_BATCH': 10**30,
            'AI_SEGMENTATION_MAX_RETRIES': 10**30,
            'AI_SEGMENTATION_CONTEXT_WINDOW': -10,
            'AI_SEGMENTATION_BOUNDARY_WINDOW': 10**30,
            'OPENAI_TIMEOUT_SECONDS': float('nan'),
            'AI_SEGMENTATION_REASONING_EFFORT': 'not-a-real-effort',
        })
        self.assertTrue(math.isfinite(cfg.min_cue_duration_s))
        self.assertTrue(math.isfinite(cfg.max_cue_duration_s))
        self.assertEqual(cfg.max_chars_per_batch, 20000)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.context_window, 0)
        self.assertEqual(cfg.boundary_window, 50)
        self.assertEqual(cfg.request_timeout_s, 600.0)
        self.assertEqual(cfg.reasoning_effort, 'auto')

    def test_incomplete_word_timestamps_fall_back_without_losing_segment_text(self):
        segment = AsrSegmentTiming(
            start_s=0.0, end_s=2.0, text='hello world',
            words=[
                AsrWordTiming(0.0, 0.8, 'hello'),
                AsrWordTiming(float('nan'), 1.8, 'world'),
            ],
        )
        batches = build_batches([self._result(segment)], 120.0, 4000)
        self.assertEqual(len(batches), 1)
        self.assertFalse(batches[0].has_word_timestamps)
        self.assertEqual(batches[0].words, [])
        self.assertEqual(batches[0].segments[0].text, 'hello world')

    def test_nonfinite_cue_times_are_rejected(self):
        parsed = {'cues': [
            {'start_s': float('nan'), 'end_s': 1.0, 'text': 'bad'},
            {'start_s': 0.0, 'end_s': 1.0, 'text': 'good'},
        ]}
        cues = _parse_cues_response(parsed, 0.0, 1.0, 1)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]['text'], 'good')

    def test_excessive_cue_array_is_rejected_atomically(self):
        cues = [{'start_s': i * 0.1, 'end_s': i * 0.1 + 0.05, 'text': str(i)} for i in range(20)]
        self.assertEqual(_parse_cues_response({'cues': cues}, 0.0, 3.0, 1), [])

    def test_index_ranges_require_exact_pairs_and_finite_numbers(self):
        with self.assertRaises(Exception):
            _parse_index_ranges('[[0, 1, 999]]', 2)
        with self.assertRaises(Exception):
            _parse_index_ranges('{"ranges":[{"indices":[0, 1, 2]}]}', 2)
        with self.assertRaises(Exception):
            _parse_index_ranges('[[0, "NaN"]]', 2)

    def test_srt_absolute_timestamps_are_not_offset_twice(self):
        result = self._result(
            AsrSegmentTiming(11.0, 12.0, 'absolute'),
            start=10.0, end=12.0,
            metadata={'timestamps_absolute': True},
        )
        from modules.ai_segmentation import _flatten_segments
        segments, _, _ = _flatten_segments([result])
        self.assertEqual((segments[0].start_s, segments[0].end_s), (11.0, 12.0))

    def test_overlapping_rhythm_cue_text_is_preserved(self):
        cfg = AISegmentationConfig(min_cue_duration_s=0.1, max_cue_duration_s=10.0, max_cps=100.0)
        cues = enforce_rhythm([
            AlignedSubtitleCue(0.0, 2.0, 'first'),
            AlignedSubtitleCue(1.0, 2.0, 'second'),
        ], cfg)
        self.assertEqual(len(cues), 1)
        self.assertIn('first', cues[0].text)
        self.assertIn('second', cues[0].text)

    def test_compound_cjk_fallback_split_is_reachable(self):
        text = '这是一段很长的文本因为需要继续说明更多内容'
        pos = _find_best_fallback_split(text)
        self.assertEqual(text[pos:pos + 2], '因为')

    def test_over_budget_batch_uses_baseline_without_ai_request(self):
        cfg = AISegmentationConfig(max_chars_per_batch=2)
        seg = AISegmenter(cfg, logger=MagicMock())
        batch = _Batch(
            segments=[AsrSegmentTiming(0.0, 2.0, 'too long')],
            time_start_s=0.0, time_end_s=2.0,
            has_word_timestamps=False,
        )
        with patch.object(seg, '_call_ai_segment_level', side_effect=AssertionError('AI call')):
            cues = seg._segment_batch_with_context(batch, 'test', 0, 1, [])
        self.assertEqual([cue.text for cue in cues], ['too long'])
        self.assertEqual(seg.last_degradation_stats['baseline'], 1)


if __name__ == '__main__':
    unittest.main()
