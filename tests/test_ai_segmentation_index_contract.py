"""索引契约 / 时间戳吸附 / 出口归一化的回归测试。

对应 ai_segmentation.py 的 7 项改动：
- _fill_gap_ranges 改为硬契约校验（缺口/乱序/重叠/尾部未覆盖一律判非法）
- _cues_from_index_ranges 越界整批拒绝（不再 warning + continue）
- _parse_cues_response 增加输入段边界吸附、覆盖率与时长上界校验
- _assert_text_coverage 文本覆盖率闸门（两级 AI 出口共用）
- 置信度分层、出口归一化 _normalize_output_cues、降级统计 last_degradation_stats
"""

import logging
import unittest
from unittest.mock import MagicMock, patch

from modules.ai_segmentation import (
    AISegmentationConfig,
    AISegmentationError,
    AISegmenter,
    _assert_text_coverage,
    _Batch,
    _cues_from_index_ranges,
    _fill_gap_ranges,
    _normalize_output_cues,
    _parse_cues_response,
)
from modules.subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
    DetectedSpeechWindow,
)


def _make_word(text, start, end):
    return AsrWordTiming(start_s=start, end_s=end, text=text)


def _make_segment(text, start, end, words=None):
    return AsrSegmentTiming(start_s=start, end_s=end, text=text, words=words or [])


def _make_result(segments, win_start, win_end, timestamp_mode='word'):
    return AsrTranscriptionResult(
        provider='whisper',
        response_format='verbose_json',
        timestamp_mode=timestamp_mode,
        segments=segments,
        window=DetectedSpeechWindow(
            start_s=win_start, end_s=win_end,
            ownership_start_s=win_start, ownership_end_s=win_end,
        ),
    )


def _cue(start, end, text, **kw):
    return AlignedSubtitleCue(start_s=start, end_s=end, text=text, **kw)


# ---------------------------------------------------------------------------
# D1：_fill_gap_ranges 硬契约校验
# ---------------------------------------------------------------------------

class IndexRangeContractTests(unittest.TestCase):
    def test_gap_rejected(self):
        # [(0,5),(7,9)] 词数 10：词 6 缺失 → 旧实现塌缩成 [(0,9)]，现在必须拒绝
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(0, 5), (7, 9)], 10)

    def test_out_of_order_rejected(self):
        # [(1,5),(7,10),(0,0)] 词数 11：排序后为 (0,0)(1,5)(7,10)，缺词 6 且
        # 首段虽为 0 但后续非连续 → 违反契约判非法。旧实现会产出时间重叠/文本重复的区间。
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(1, 5), (7, 10), (0, 0)], 11)

    def test_reversed_but_contiguous_coverage_accepted(self):
        # 顺序颠倒但连续完整覆盖：排序后合法，按序原样返回（排序本身不算违约）
        self.assertEqual(_fill_gap_ranges([(3, 4), (0, 2)], 5), [(0, 2), (3, 4)])

    def test_overlap_rejected(self):
        # [(0,5),(5,9)] 词数 10：重叠且不连续（应为 (0,4),(5,9)）
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(0, 5), (5, 9)], 10)

    def test_tail_gap_rejected(self):
        # [(0,5)] 词数 10：尾部未覆盖 → 旧实现拉成 [(0,9)]
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(0, 5)], 10)

    def test_head_not_zero_rejected(self):
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(1, 9)], 10)

    def test_start_greater_than_end_rejected(self):
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([(0, 4), (8, 5), (9, 9)], 10)

    def test_empty_rejected(self):
        with self.assertRaises(AISegmentationError):
            _fill_gap_ranges([], 10)

    def test_exact_cover_returned_verbatim(self):
        ranges = [(0, 9)]
        self.assertEqual(_fill_gap_ranges(ranges, 10), [(0, 9)])

    def test_exact_cover_multi_segment_returned_verbatim(self):
        ranges = [(0, 2), (3, 4), (5, 9)]
        self.assertEqual(_fill_gap_ranges(ranges, 10), [(0, 2), (3, 4), (5, 9)])

    def test_unsorted_but_contiguous_is_accepted_sorted(self):
        # 连续覆盖但顺序颠倒：排序后合法 → 原样（有序）返回，不修补
        self.assertEqual(_fill_gap_ranges([(3, 4), (0, 2)], 5), [(0, 2), (3, 4)])


# ---------------------------------------------------------------------------
# D2：_cues_from_index_ranges 越界整批拒绝
# ---------------------------------------------------------------------------

class CuesFromIndexRangesContractTests(unittest.TestCase):
    def test_end_out_of_bounds_raises(self):
        words = [_make_word('hello', 0, 0.5)]
        with self.assertRaises(AISegmentationError):
            _cues_from_index_ranges([(0, 5)], words, 'whisper')

    def test_negative_start_raises(self):
        words = [_make_word('hello', 0, 0.5)]
        with self.assertRaises(AISegmentationError):
            _cues_from_index_ranges([(-1, 0)], words, 'whisper')

    def test_valid_range_ok_and_confidence_layered(self):
        words = [_make_word('hello', 0, 0.5), _make_word('world', 0.5, 1.0)]
        cues = _cues_from_index_ranges([(0, 1)], words, 'whisper')
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].timing_source, 'ai')
        self.assertAlmostEqual(cues[0].alignment_confidence, 0.90, places=6)

    def test_empty_text_still_skipped(self):
        words = [_make_word('', 0, 0.5)]
        self.assertEqual(_cues_from_index_ranges([(0, 0)], words, 'whisper'), [])


# ---------------------------------------------------------------------------
# D4：_assert_text_coverage 覆盖率闸门
# ---------------------------------------------------------------------------

class AssertTextCoverageTests(unittest.TestCase):
    def test_low_coverage_raises(self):
        src = '你好世界这是一段很长的原文内容'
        out = [_cue(0, 1, '你好')]  # 覆盖率远低于 0.9
        with self.assertRaises(AISegmentationError):
            _assert_text_coverage(src, out, context='test')

    def test_new_non_punctuation_chars_raise(self):
        # 输出完整覆盖原文，但新增了原文不存在的字 "错"
        src = '你好世界'
        out = [_cue(0, 1, '你好世界错')]
        with self.assertRaises(AISegmentationError):
            _assert_text_coverage(src, out, context='test')

    def test_only_added_punctuation_passes(self):
        src = '你好世界今天天气不错'
        out = [
            _cue(0, 1, '你好，世界！'),
            _cue(1, 2, '今天天气不错。'),
        ]
        _assert_text_coverage(src, out, context='test')  # 不应抛错

    def test_exact_text_passes(self):
        src = 'hello world'
        _assert_text_coverage(src, [_cue(0, 1, 'hello world')], context='test')

    def test_empty_source_passes(self):
        _assert_text_coverage('', [], context='test')

    def test_logger_receives_warning_on_reject(self):
        logger = MagicMock()
        with self.assertRaises(AISegmentationError):
            _assert_text_coverage('你好世界', [_cue(0, 1, '你好世界坏')],
                                  context='test', logger=logger)
        self.assertTrue(logger.warning.called)


# ---------------------------------------------------------------------------
# D6：_normalize_output_cues 出口归一化
# ---------------------------------------------------------------------------

class NormalizeOutputCuesTests(unittest.TestCase):
    def test_unsorted_becomes_sorted(self):
        cues = [_cue(5, 6, 'b'), _cue(1, 2, 'a'), _cue(3, 4, 'c')]
        out = _normalize_output_cues(cues)
        self.assertEqual([c.text for c in out], ['a', 'c', 'b'])
        self.assertEqual([c.start_s for c in out], sorted(c.start_s for c in out))

    def test_overlap_removed(self):
        cues = [_cue(0, 2, 'a'), _cue(1.5, 3, 'b')]
        out = _normalize_output_cues(cues)
        self.assertEqual(len(out), 2)
        self.assertGreaterEqual(out[1].start_s, out[0].end_s)
        for i in range(1, len(out)):
            self.assertGreaterEqual(out[i].start_s, out[i - 1].end_s)

    def test_fully_swallowed_cue_text_is_absorbed_not_lost(self):
        # b 完全落在 a 内 -> 时间轴不可用，但**文本不得丢失**：并入相邻 cue。
        cues = [_cue(0, 5, 'a'), _cue(2, 3, 'b')]
        out = _normalize_output_cues(cues)
        self.assertEqual(len(out), 1)
        self.assertIn('a', out[0].text)
        self.assertIn('b', out[0].text)
        self.assertLessEqual(out[0].end_s, 5.0)

    def test_out_of_range_clamped(self):
        cues = [_cue(-5, 100, 'a')]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(out[0].start_s, 0.0)
        self.assertLessEqual(out[0].end_s, 10.0)

    def test_empty_text_dropped(self):
        cues = [_cue(0, 1, 'a'), _cue(1, 2, '   ')]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual([c.text for c in out], ['a'])

    def test_invalid_duration_keeps_text_in_fallback_cue(self):
        # 全部 cue 时间轴都不可用 -> 输出一条兜底 cue 承载文本，而不是整批丢内容。
        cues = [_cue(0, 0, 'zero'), _cue(1, 0.5, 'inverted')]
        out = _normalize_output_cues(cues)
        self.assertEqual(len(out), 1)
        self.assertIn('zero', out[0].text)
        self.assertIn('inverted', out[0].text)
        self.assertEqual(out[0].timing_source, 'orphan_text_fallback')

    def test_invalid_cue_text_is_absorbed_into_next_valid_cue(self):
        """时间戳不可用的 cue 的文本必须并入下一条，而不是连文本一起丢掉。"""
        cues = [_cue(0, 0, 'lost text'), _cue(1, 2, 'kept text')]
        out = _normalize_output_cues(cues)
        self.assertEqual(len(out), 1)
        self.assertIn('lost text', out[0].text)
        self.assertIn('kept text', out[0].text)
        self.assertAlmostEqual(out[0].start_s, 1.0, places=6)

    def test_invalid_cue_text_goes_to_the_temporally_nearest_cue(self):
        """吸收必须选**时间上最近**的合法 cue，且同条内语序与时间轴一致。

        缺陷（收集阶段先挂起、消费阶段给「下一条」）：5.0s 的碎片被挂到 0–1s 的
        cue 上 —— 时间上最远，且同一条字幕里「后发生的文本排在前」。
        """
        cues = [_cue(0, 1, 'alpha'), _cue(2, 3, 'bravo'), _cue(5, 5, 'late-line-5s')]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].text, 'alpha')
        self.assertEqual(out[1].text, 'bravo late-line-5s')
        self.assertAlmostEqual(out[1].start_s, 2.0, places=6)

    def test_fragment_before_the_only_valid_cue_is_prepended(self):
        """碎片在目标之前时前置，保持「文本顺序 = 时间顺序」。"""
        cues = [_cue(0, 0, 'early'), _cue(5, 6, 'later')]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, 'early later')

    def test_multiple_fragments_keep_chronological_order(self):
        cues = [_cue(0, 1, 'A'), _cue(3, 3, 'X-late'), _cue(4, 4, 'Y-later')]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, 'A X-late Y-later')

    def test_absorbed_text_does_not_mutate_caller_objects(self):
        """归一化不得就地改写调用方传入的 cue（可能被上游继续复用）。

        两条路径都要覆盖：吸收在遍历中发生（下一条合法 cue），以及遍历结束后
        仍在等待安置的文本落到「最后一条合法 cue」——后者此前用
        ``normalized[-1].text = ...`` 直接改写了调用方对象（且该对象会被
        ``all_cues`` / 上下文窗口继续引用）。
        """
        cues = [_cue(0, 0, 'lost text'), _cue(1, 2, 'kept text')]
        callers = list(cues)
        _normalize_output_cues(cues)
        self.assertEqual(callers[1].text, 'kept text')

        # 末条被 total_duration 钳成零长 → 文本只能落到前一条（可复现的就地改写路径）
        tail = [_cue(0, 5, 'first-valid'), _cue(10, 12, 'clamped-last')]
        out = _normalize_output_cues(tail, total_duration_s=10.0)
        self.assertEqual(tail[0].text, 'first-valid')
        self.assertIn('clamped-last', out[0].text)
        self.assertIsNot(out[0], tail[0])

    def test_fully_dropped_batch_is_reported_not_silent(self):
        logger = MagicMock()
        cues = [_cue(0, 0, 'only text')]
        out = _normalize_output_cues(cues, logger=logger)
        self.assertEqual(len(out), 1)
        self.assertTrue(logger.warning.called)

    def test_confidence_preserved(self):
        cues = [_cue(0, 1, 'a', timing_source='ai', alignment_confidence=0.9)]
        out = _normalize_output_cues(cues, total_duration_s=10.0)
        self.assertEqual(out[0].timing_source, 'ai')
        self.assertAlmostEqual(out[0].alignment_confidence, 0.9, places=6)


# ---------------------------------------------------------------------------
# D3：_parse_cues_response 边界吸附 / 覆盖率 / 时长上界
# ---------------------------------------------------------------------------

class ParseCuesResponseSnapTests(unittest.TestCase):
    def test_no_boundaries_keeps_legacy_behaviour(self):
        parsed = {'cues': [{'start_s': 1.03, 'end_s': 2.07, 'text': 'x'}]}
        out = _parse_cues_response(parsed, 0.0, 10.0, 2)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]['start_s'], 1.03, places=3)

    def test_within_tolerance_snapped_to_boundary(self):
        parsed = {'cues': [{'start_s': 1.2, 'end_s': 4.1, 'text': 'x'}]}
        out = _parse_cues_response(parsed, 0.0, 10.0, 2, input_boundaries=[1.0, 4.0])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]['start_s'], 1.0, places=6)
        self.assertAlmostEqual(out[0]['end_s'], 4.0, places=6)

    def test_outside_tolerance_rejects_whole_batch(self):
        parsed = {'cues': [{'start_s': 2.5, 'end_s': 4.0, 'text': 'x'}]}
        out = _parse_cues_response(parsed, 0.0, 10.0, 2, input_boundaries=[1.0, 4.0])
        self.assertEqual(out, [])

    def test_total_duration_clamped(self):
        parsed = {'cues': [{'start_s': -3.0, 'end_s': 99.0, 'text': 'x'}]}
        out = _parse_cues_response(parsed, 0.0, 100.0, 1, total_duration_s=20.0)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(out[0]['start_s'], 0.0)
        self.assertLessEqual(out[0]['end_s'], 20.0)

    def test_overlapping_snapped_output_rejected(self):
        # 吸附模式下不允许截断（截断会产生非边界时间）→ 整批拒绝
        parsed = {'cues': [
            {'start_s': 0.0, 'end_s': 4.0, 'text': 'a'},
            {'start_s': 1.0, 'end_s': 4.0, 'text': 'b'},
        ]}
        out = _parse_cues_response(parsed, 0.0, 10.0, 2, input_boundaries=[0.0, 1.0, 4.0])
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# D4/D5：AI 调用层出口闸门与置信度分层
# ---------------------------------------------------------------------------

class AiLevelGateTests(unittest.TestCase):
    def _segmenter(self):
        cfg = AISegmentationConfig(enabled=True, api_key='k', model_name='m')
        cfg.resolved_api_key = 'k'
        cfg.resolved_model_name = 'm'
        return AISegmenter(cfg, logger=MagicMock())

    def test_word_level_passes_on_consistent_output(self):
        seg = self._segmenter()
        words = [_make_word('你好', 0, 0.5), _make_word('世界', 0.5, 1.0)]
        batch = _Batch(words=words, segments=[], time_start_s=0.0, time_end_s=1.0,
                       has_word_timestamps=True)
        with patch.object(seg, '_call_with_retry_raw', return_value='[[0,1]]'), \
                patch('modules.ai_segmentation._build_word_payload_with_context', return_value={}):
            cues = seg._call_ai_word_level(batch, 'whisper')
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0].alignment_confidence, 0.90, places=6)

    def test_word_level_rejects_when_text_lost(self):
        seg = self._segmenter()
        words = [_make_word('你', 0, 0.5), _make_word('好', 0.5, 1.0), _make_word('世', 1.0, 1.5)]
        batch = _Batch(words=words, segments=[], time_start_s=0.0, time_end_s=1.5,
                       has_word_timestamps=True)
        # 只覆盖前两个词：第三个词的内容整段丢失 → 覆盖率闸门应抛错
        with patch.object(seg, '_call_with_retry_raw', return_value='[[0,1]]'), \
                patch('modules.ai_segmentation._build_word_payload_with_context', return_value={}):
            with self.assertRaises(AISegmentationError):
                seg._call_ai_word_level(batch, 'whisper')

    def test_segment_level_rejects_when_text_lost(self):
        seg = self._segmenter()
        segs = [_make_segment('你好世界', 0.0, 2.0), _make_segment('今天不错', 2.0, 4.0)]
        batch = _Batch(words=[], segments=segs, time_start_s=0.0, time_end_s=4.0,
                       has_word_timestamps=False)
        # 第二条输入段的文本被丢弃，但时间合法 → 覆盖率闸门应抛错
        parsed = {'cues': [{'start_s': 0.0, 'end_s': 4.0, 'text': '你好世界'}]}
        with patch.object(seg, '_call_with_retry', return_value=parsed), \
                patch('modules.ai_segmentation._build_segment_payload_with_context', return_value={}):
            with self.assertRaises(AISegmentationError):
                seg._call_ai_segment_level(batch, 'whisper')

    def test_segment_level_snaps_to_segment_boundaries(self):
        seg = self._segmenter()
        segs = [
            _make_segment('你好世界', 0.0, 2.0),
            _make_segment('今天不错', 2.0, 4.0),
        ]
        batch = _Batch(words=[], segments=segs, time_start_s=0.0, time_end_s=4.0,
                       has_word_timestamps=False)
        parsed = {'cues': [
            {'start_s': 0.1, 'end_s': 1.9, 'text': '你好世界'},
            {'start_s': 2.1, 'end_s': 3.9, 'text': '今天不错'},
        ]}
        with patch.object(seg, '_call_with_retry', return_value=parsed), \
                patch('modules.ai_segmentation._build_segment_payload_with_context', return_value={}):
            cues = seg._call_ai_segment_level(batch, 'whisper')
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start_s, 0.0, places=6)
        self.assertAlmostEqual(cues[0].end_s, 2.0, places=6)
        self.assertAlmostEqual(cues[1].start_s, 2.0, places=6)
        self.assertAlmostEqual(cues[1].end_s, 4.0, places=6)
        self.assertAlmostEqual(cues[0].alignment_confidence, 0.70, places=6)

    def test_segment_level_rejects_unsnappable_time(self):
        seg = self._segmenter()
        segs = [_make_segment('你好世界', 0.0, 2.0)]
        batch = _Batch(words=[], segments=segs, time_start_s=0.0, time_end_s=2.0,
                       has_word_timestamps=False)
        parsed = {'cues': [{'start_s': 1.0, 'end_s': 1.5, 'text': '你好世界'}]}
        with patch.object(seg, '_call_with_retry', return_value=parsed), \
                patch('modules.ai_segmentation._build_segment_payload_with_context', return_value={}):
            with self.assertRaises(AISegmentationError):
                seg._call_ai_segment_level(batch, 'whisper')


# ---------------------------------------------------------------------------
# D6/D7：segment() 出口归一化与降级统计
# ---------------------------------------------------------------------------

class SegmentExitTests(unittest.TestCase):
    def _segmenter(self):
        cfg = AISegmentationConfig(enabled=True, api_key='k', model_name='m')
        cfg.resolved_api_key = 'k'
        cfg.resolved_model_name = 'm'
        return AISegmenter(cfg, logger=MagicMock())

    def _word_result(self):
        # 输入覆盖 0~6s：总时长上界=6.0，使归一化不会把测试 cue 截断
        words = [
            _make_word('hello', 0, 1), _make_word('world', 1, 2),
            _make_word('foo', 2, 3), _make_word('bar', 3, 4),
            _make_word('baz', 4, 5), _make_word('qux', 5, 6),
        ]
        return [
            _make_result([_make_segment('hello world foo bar baz qux', 0, 6, words)], 0, 6),
        ]

    def test_segment_returns_sorted_non_overlapping(self):
        seg = self._segmenter()
        unsorted = [_cue(5, 6, 'c', timing_source='ai'), _cue(0, 3, 'a', timing_source='ai')]
        with patch.object(seg, '_segment_batch_with_context', return_value=unsorted):
            out = seg.segment(self._word_result())
        self.assertEqual([c.text for c in out], ['a', 'c'])
        for i in range(1, len(out)):
            self.assertGreaterEqual(out[i].start_s, out[i - 1].end_s)

    def test_degradation_stats_populated(self):
        seg = self._segmenter()
        with patch.object(seg, '_call_ai_word_level',
                          side_effect=AISegmentationError('word fail')), \
                patch.object(seg, '_call_ai_segment_level',
                             side_effect=AISegmentationError('seg fail')):
            out = seg.segment(self._word_result())
        self.assertGreater(len(out), 0)
        stats = seg.last_degradation_stats
        self.assertEqual(stats['baseline'], 1)
        self.assertEqual(stats['rejected'], 2)
        self.assertEqual(stats['word_level'], 0)
        self.assertEqual(stats['segment_level'], 0)


if __name__ == '__main__':
    unittest.main()
