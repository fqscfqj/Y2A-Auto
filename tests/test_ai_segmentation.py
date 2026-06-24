"""AI 智能分段模块单元测试。

覆盖：配置继承解析、VAD 窗口合并成批次、字级/段级 prompt 构造、
三级降级（字级成功 / 字级失败降段级 / 全失败抛异常）、
节奏后处理（短时长合并、超长拆分）、JSON 解析校验。
"""

import unittest
from unittest.mock import patch, MagicMock

from modules.ai_segmentation import (
    AISegmentationConfig,
    AISegmentationError,
    AISegmenter,
    _Batch,
    _cues_from_index_ranges,
    _find_balanced_json,
    _find_soft_break_point,
    _load_json_candidate,
    _parse_cues_response,
    _parse_index_ranges,
    _crosses_sentence_boundary,
    _repair_broken_sentences,
    _split_long_cue,
    build_batches,
    enforce_rhythm,
    _flatten_segments_from_words,
)
from modules.subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
    DetectedSpeechWindow,
)


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------

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


def _base_app_config(**overrides):
    cfg = {
        'AI_SEGMENTATION_ENABLED': True,
        'AI_SEGMENTATION_BASE_URL': '',
        'AI_SEGMENTATION_API_KEY': '',
        'AI_SEGMENTATION_MODEL_NAME': '',
        'AI_SEGMENTATION_THINKING_ENABLED': False,
        'AI_SEGMENTATION_MIN_CUE_DURATION_S': 1.5,
        'AI_SEGMENTATION_MAX_CUE_DURATION_S': 6.0,
        'AI_SEGMENTATION_MAX_CPS': 15.0,
        'AI_SEGMENTATION_BATCH_WINDOW_S': 120.0,
        'AI_SEGMENTATION_MAX_CHARS_PER_BATCH': 8000,
        'AI_SEGMENTATION_TEMPERATURE': 0.2,
        'AI_SEGMENTATION_MAX_RETRIES': 2,
        'OPENAI_API_KEY': 'sk-global',
        'OPENAI_BASE_URL': 'https://global.example.com/v1',
        'OPENAI_MODEL_NAME': 'gpt-4o',
        'OPENAI_TIMEOUT_SECONDS': 600,
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# 配置继承解析
# ---------------------------------------------------------------------------

class AISegmentationConfigTests(unittest.TestCase):
    def test_blank_fields_inherit_global_openai(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config())
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.resolved_api_key, 'sk-global')
        self.assertEqual(cfg.resolved_base_url, 'https://global.example.com/v1')
        self.assertEqual(cfg.resolved_model_name, 'gpt-4o')
        self.assertTrue(cfg.is_model_configured)

    def test_override_takes_precedence(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(
            AI_SEGMENTATION_MODEL_NAME='parakeet-crispasr',
            AI_SEGMENTATION_API_KEY='sk-seg',
            AI_SEGMENTATION_BASE_URL='https://seg.example.com/v1',
        ))
        self.assertEqual(cfg.resolved_model_name, 'parakeet-crispasr')
        self.assertEqual(cfg.resolved_api_key, 'sk-seg')
        self.assertEqual(cfg.resolved_base_url, 'https://seg.example.com/v1')

    def test_partial_override_base_url_still_inherits(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(
            AI_SEGMENTATION_MODEL_NAME='my-model',
        ))
        self.assertEqual(cfg.resolved_model_name, 'my-model')
        self.assertEqual(cfg.resolved_api_key, 'sk-global')
        self.assertEqual(cfg.resolved_base_url, 'https://global.example.com/v1')

    def test_not_configured_when_no_key_anywhere(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(
            OPENAI_API_KEY='', AI_SEGMENTATION_API_KEY='',
        ))
        self.assertFalse(cfg.is_model_configured)


# ---------------------------------------------------------------------------
# 批次构建
# ---------------------------------------------------------------------------

class BuildBatchesTests(unittest.TestCase):
    def test_adjacent_windows_merged_within_batch_window(self):
        r1 = _make_result([_make_segment('hello world', 0, 2, [_make_word('hello', 0, 1), _make_word('world', 1, 2)])], 0, 2)
        r2 = _make_result([_make_segment('foo bar', 2.5, 4, [_make_word('foo', 2.5, 3), _make_word('bar', 3, 4)])], 2.5, 4)
        batches = build_batches([r1, r2], batch_window_s=120.0, max_chars=8000)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0].words), 4)
        self.assertTrue(batches[0].has_word_timestamps)

    def test_windows_split_when_exceeding_batch_window(self):
        r1 = _make_result([_make_segment('a', 0, 2, [_make_word('a', 0, 2)])], 0, 2)
        r2 = _make_result([_make_segment('b', 200, 202, [_make_word('b', 200, 202)])], 200, 202)
        batches = build_batches([r1, r2], batch_window_s=10.0, max_chars=8000)
        self.assertEqual(len(batches), 2)

    def test_oversized_result_split_by_char_limit(self):
        # 单个 result 超过字符上限 → 按词切分子批次
        words = [_make_word(f'w{i}', i, i + 0.5) for i in range(500)]  # 500 词，每词 2 字符 = 1000 字符
        seg = _make_segment(''.join(w.text for w in words), 0, 250, words)
        r = _make_result([seg], 0, 250)
        batches = build_batches([r], batch_window_s=120.0, max_chars=300)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertLessEqual(b.char_count, 300 + 2)  # 容差 1 个词

    def test_capability_change_splits_batch(self):
        r1 = _make_result([_make_segment('has words', 0, 2, [_make_word('has', 0, 1), _make_word('words', 1, 2)])], 0, 2, timestamp_mode='word')
        r2 = _make_result([_make_segment('no words here', 3, 5)], 3, 5, timestamp_mode='segment')
        batches = build_batches([r1, r2], batch_window_s=120.0, max_chars=8000)
        self.assertEqual(len(batches), 2)
        self.assertTrue(batches[0].has_word_timestamps)
        self.assertFalse(batches[1].has_word_timestamps)


# ---------------------------------------------------------------------------
# JSON 解析校验
# ---------------------------------------------------------------------------

class ParseCuesResponseTests(unittest.TestCase):
    def test_valid_response(self):
        parsed = {'cues': [
            {'start_s': 0.0, 'end_s': 1.5, 'text': 'hello'},
            {'start_s': 1.5, 'end_s': 3.0, 'text': 'world'},
        ]}
        cues = _parse_cues_response(parsed, batch_start_s=0.0, batch_end_s=3.0, input_count=2)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]['text'], 'hello')

    def test_overlapping_cues_deduplicated(self):
        parsed = {'cues': [
            {'start_s': 0.0, 'end_s': 2.0, 'text': 'a'},
            {'start_s': 1.5, 'end_s': 3.0, 'text': 'b'},  # 重叠
        ]}
        cues = _parse_cues_response(parsed, 0.0, 3.0, 2)
        self.assertEqual(len(cues), 2)
        self.assertGreaterEqual(cues[1]['start_s'], cues[0]['end_s'] - 0.001)

    def test_out_of_range_clamped(self):
        parsed = {'cues': [
            {'start_s': -5.0, 'end_s': 100.0, 'text': 'x'},
        ]}
        cues = _parse_cues_response(parsed, 0.0, 3.0, 1)
        self.assertEqual(len(cues), 1)
        self.assertGreaterEqual(cues[0]['start_s'], -0.5)
        self.assertLessEqual(cues[0]['end_s'], 3.5)

    def test_empty_or_invalid_returns_empty(self):
        self.assertEqual(_parse_cues_response(None, 0, 1, 1), [])
        self.assertEqual(_parse_cues_response({}, 0, 1, 1), [])
        self.assertEqual(_parse_cues_response({'cues': []}, 0, 1, 1), [])
        self.assertEqual(_parse_cues_response({'cues': [{'start_s': 2, 'end_s': 1, 'text': 'x'}]}, 0, 1, 1), [])
        self.assertEqual(_parse_cues_response({'cues': [{'start_s': 0, 'end_s': 1, 'text': ''}]}, 0, 1, 1), [])

    def test_excessive_count_truncated(self):
        cues_in = [{'start_s': i * 0.1, 'end_s': i * 0.1 + 0.05, 'text': f'x{i}'} for i in range(50)]
        cues = _parse_cues_response({'cues': cues_in}, 0, 10, 5)
        max_allowed = max(8, 5 * 2 + 4)
        self.assertLessEqual(len(cues), max_allowed)


# ---------------------------------------------------------------------------
# 三级降级（Mock LLM）
# ---------------------------------------------------------------------------

class ThreeLevelDegradationTests(unittest.TestCase):
    def _segmenter_with_mocks(self, word_response=None, seg_response=None, word_exc=None, seg_exc=None):
        cfg = AISegmentationConfig.from_app_config(_base_app_config())
        segmenter = AISegmenter(cfg, logger=MagicMock())
        call_state = {'word_called': False, 'seg_called': False}

        def fake_word(batch, provider, context_cues=None):
            call_state['word_called'] = True
            if word_exc:
                raise word_exc
            return word_response

        def fake_seg(batch, provider, context_cues=None):
            call_state['seg_called'] = True
            if seg_exc:
                raise seg_exc
            return seg_response

        segmenter._call_ai_word_level = fake_word
        segmenter._call_ai_segment_level = fake_seg
        return segmenter, call_state

    def test_word_level_success(self):
        ai_cues = [AlignedSubtitleCue(start_s=0, end_s=1.5, text='hello', timing_source='ai')]
        seg, state = self._segmenter_with_mocks(word_response=ai_cues)
        results = [_make_result([_make_segment('hello', 0, 1.5, [_make_word('hello', 0, 1.5)])], 0, 1.5)]
        out = seg.segment(results)
        self.assertTrue(state['word_called'])
        self.assertFalse(state['seg_called'])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].timing_source, 'ai')

    def test_word_failure_falls_back_to_segment_level(self):
        ai_seg_cues = [AlignedSubtitleCue(start_s=0, end_s=1.5, text='hello', timing_source='ai')]
        seg, state = self._segmenter_with_mocks(
            word_exc=AISegmentationError('word failed'),
            seg_response=ai_seg_cues,
        )
        results = [_make_result([_make_segment('hello', 0, 1.5, [_make_word('hello', 0, 1.5)])], 0, 1.5)]
        out = seg.segment(results)
        self.assertTrue(state['word_called'])
        self.assertTrue(state['seg_called'])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].timing_source, 'ai')

    def test_both_fail_falls_back_to_baseline(self):
        seg, state = self._segmenter_with_mocks(
            word_exc=AISegmentationError('word failed'),
            seg_exc=AISegmentationError('seg failed'),
        )
        results = [_make_result([_make_segment('hello world', 0, 2, [_make_word('hello', 0, 1), _make_word('world', 1, 2)])], 0, 2)]
        out = seg.segment(results)
        self.assertTrue(state['word_called'])
        self.assertTrue(state['seg_called'])
        # 基线对齐产出 cue（非 ai 来源）
        self.assertGreater(len(out), 0)
        self.assertNotEqual(out[0].timing_source, 'ai')

    def test_disabled_raises(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(AI_SEGMENTATION_ENABLED=False))
        seg = AISegmenter(cfg, logger=MagicMock())
        with self.assertRaises(AISegmentationError):
            seg.segment([_make_result([_make_segment('x', 0, 1, [_make_word('x', 0, 1)])], 0, 1)])

    def test_not_configured_raises(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(
            OPENAI_API_KEY='', AI_SEGMENTATION_API_KEY='', OPENAI_MODEL_NAME='',
        ))
        seg = AISegmenter(cfg, logger=MagicMock())
        with self.assertRaises(AISegmentationError):
            seg.segment([_make_result([_make_segment('x', 0, 1, [_make_word('x', 0, 1)])], 0, 1)])

    def test_segment_level_used_when_no_word_timestamps(self):
        ai_seg_cues = [AlignedSubtitleCue(start_s=0, end_s=2, text='hello world', timing_source='ai')]
        seg, state = self._segmenter_with_mocks(seg_response=ai_seg_cues)
        # 无字级时间戳的结果
        results = [_make_result([_make_segment('hello world', 0, 2)], 0, 2, timestamp_mode='segment')]
        out = seg.segment(results)
        self.assertFalse(state['word_called'])
        self.assertTrue(state['seg_called'])
        self.assertEqual(out[0].timing_source, 'ai')


# ---------------------------------------------------------------------------
# 节奏后处理
# ---------------------------------------------------------------------------

class EnforceRhythmTests(unittest.TestCase):
    def _cfg(self, **kw):
        return AISegmentationConfig.from_app_config(_base_app_config(**kw))

    def test_short_cue_merged_with_next(self):
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=1.5, AI_SEGMENTATION_MAX_CUE_DURATION_S=6.0)
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=0.3, text='a'),  # 过短
            AlignedSubtitleCue(start_s=0.3, end_s=2.5, text='b'),
        ]
        out = enforce_rhythm(cues, cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, 'a b')
        self.assertGreaterEqual(out[0].end_s - out[0].start_s, 1.5)

    def test_long_cue_split(self):
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=1.5, AI_SEGMENTATION_MAX_CUE_DURATION_S=3.0)
        cues = [AlignedSubtitleCue(start_s=0, end_s=10, text='first part. second part.')]
        out = enforce_rhythm(cues, cfg)
        self.assertGreater(len(out), 1)
        for c in out:
            self.assertLessEqual(c.end_s - c.start_s, 3.0 + 0.01)

    def test_suboptimal_cues_merged_into_ideal_range(self):
        """两条 1.5-2s 偏短条目应合并到 2-4s 理想区间。"""
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=1.5, AI_SEGMENTATION_MAX_CUE_DURATION_S=6.0)
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=1.8, text='短句一'),
            AlignedSubtitleCue(start_s=1.9, end_s=3.7, text='短句二'),
        ]
        out = enforce_rhythm(cues, cfg)
        self.assertEqual(len(out), 1)
        merged_dur = out[0].end_s - out[0].start_s
        self.assertGreaterEqual(merged_dur, 2.0)
        self.assertLessEqual(merged_dur, 4.0)

    def test_overlapping_cues_resolved(self):
        cfg = self._cfg()
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=2, text='a'),
            AlignedSubtitleCue(start_s=1.5, end_s=3, text='b'),  # 重叠
        ]
        out = enforce_rhythm(cues, cfg)
        for i in range(1, len(out)):
            self.assertGreaterEqual(out[i].start_s, out[i - 1].end_s - 0.001)

    def test_empty_input(self):
        cfg = self._cfg()
        self.assertEqual(enforce_rhythm([], cfg), [])


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_flatten_segments_from_words_splits_at_punctuation(self):
        words = [
            _make_word('hello', 0, 0.5),
            _make_word('world.', 0.5, 1.0),
            _make_word('foo', 1.0, 1.5),
            _make_word('bar.', 1.5, 2.0),
        ]
        segs, start, end = _flatten_segments_from_words(words)
        self.assertEqual(len(segs), 2)
        self.assertEqual(start, 0.0)
        self.assertEqual(end, 2.0)
        self.assertEqual(segs[0].text, 'helloworld.')
        self.assertEqual(segs[1].text, 'foobar.')

    def test_baseline_align_batch_word_mode(self):
        words = [_make_word(f'w{i}', i, i + 0.5) for i in range(15)]
        batch = _Batch(words=words, segments=[], time_start_s=0, time_end_s=7.5, has_word_timestamps=True)
        from modules.ai_segmentation import _baseline_align_batch
        cues = _baseline_align_batch(batch, 'whisper')
        self.assertGreater(len(cues), 1)
        self.assertEqual(cues[0].timing_source, 'word')


# ---------------------------------------------------------------------------
# 索引范围解析
# ---------------------------------------------------------------------------

class ParseIndexRangesTests(unittest.TestCase):
    def test_array_format(self):
        raw = '[[0, 2], [3, 5]]'
        result = _parse_index_ranges(raw, word_count=6)
        self.assertEqual(result, [(0, 2), (3, 5)])

    def test_dict_format_start_index_end_index(self):
        raw = '[{"start_index": 0, "end_index": 1}, {"start_index": 2, "end_index": 3}]'
        result = _parse_index_ranges(raw, word_count=4)
        self.assertEqual(result, [(0, 1), (2, 3)])

    def test_dict_format_start_end(self):
        raw = '[{"start": 0, "end": 2}]'
        result = _parse_index_ranges(raw, word_count=3)
        self.assertEqual(result, [(0, 2)])

    def test_dict_format_from_to(self):
        raw = '[{"from": 0, "to": 1}]'
        result = _parse_index_ranges(raw, word_count=2)
        self.assertEqual(result, [(0, 1)])

    def test_dict_format_indices(self):
        raw = '[{"indices": [0, 2]}]'
        result = _parse_index_ranges(raw, word_count=3)
        self.assertEqual(result, [(0, 2)])

    def test_wrapped_format_ranges_key(self):
        raw = '{"ranges": [[0, 1], [2, 4]]}'
        result = _parse_index_ranges(raw, word_count=5)
        self.assertEqual(result, [(0, 1), (2, 4)])

    def test_empty_word_count_returns_empty(self):
        result = _parse_index_ranges('[]', word_count=0)
        self.assertEqual(result, [])

    def test_invalid_index_raises(self):
        raw = '[[0, 1], ["bad", 3]]'
        with self.assertRaises(AISegmentationError):
            _parse_index_ranges(raw, word_count=4)

    def test_start_greater_than_end_raises(self):
        raw = '[[3, 0]]'
        with self.assertRaises(AISegmentationError):
            _parse_index_ranges(raw, word_count=4)

    def test_gap_in_ranges_raises(self):
        raw = '[[0, 1], [3, 4]]'  # 缺少 2
        with self.assertRaises(AISegmentationError):
            _parse_index_ranges(raw, word_count=5)

    def test_incomplete_coverage_raises(self):
        raw = '[[0, 1]]'  # 只覆盖 0-1，word_count=4 需覆盖到 3
        with self.assertRaises(AISegmentationError):
            _parse_index_ranges(raw, word_count=4)

    def test_code_fence_wrapped(self):
        raw = '```json\n[[0, 2]]\n```'
        result = _parse_index_ranges(raw, word_count=3)
        self.assertEqual(result, [(0, 2)])

    def test_float_index_coerced(self):
        raw = '[[0.0, 1.0]]'
        result = _parse_index_ranges(raw, word_count=2)
        self.assertEqual(result, [(0, 1)])


# ---------------------------------------------------------------------------
# 软切分点
# ---------------------------------------------------------------------------

class FindSoftBreakPointTests(unittest.TestCase):
    def test_punctuation_break(self):
        words = [
            _make_word('hello', 0, 0.5),
            _make_word('world.', 0.5, 1.0),
            _make_word('foo', 1.0, 1.5),
        ]
        idx = _find_soft_break_point(words)
        self.assertEqual(idx, 2)  # 在 'world.' 后切分

    def test_pause_break(self):
        words = [
            _make_word('hello', 0, 0.5),
            _make_word('world', 0.5, 1.0),
            _make_word('foo', 2.0, 2.5),  # 1.0s 停顿 ≥ 0.6
        ]
        idx = _find_soft_break_point(words)
        self.assertEqual(idx, 2)

    def test_no_break_point_returns_zero(self):
        # words are contiguous with no punctuation and gaps < 0.6s
        words = [_make_word(f'w{i}', i * 0.3, i * 0.3 + 0.25) for i in range(5)]
        idx = _find_soft_break_point(words)
        self.assertEqual(idx, 0)

    def test_single_word_returns_zero(self):
        words = [_make_word('hello', 0, 1)]
        idx = _find_soft_break_point(words)
        self.assertEqual(idx, 0)

    def test_empty_returns_zero(self):
        idx = _find_soft_break_point([])
        self.assertEqual(idx, 0)


# ---------------------------------------------------------------------------
# 平衡 JSON 提取
# ---------------------------------------------------------------------------

class FindBalancedJsonTests(unittest.TestCase):
    def test_simple_array(self):
        result = _find_balanced_json('[[0, 1], [2, 3]]', '[', ']')
        self.assertEqual(result, '[[0, 1], [2, 3]]')

    def test_simple_object(self):
        result = _find_balanced_json('{"key": "value"}', '{', '}')
        self.assertEqual(result, '{"key": "value"}')

    def test_nested_brackets(self):
        result = _find_balanced_json('[[[0, 1]]]', '[', ']')
        self.assertEqual(result, '[[[0, 1]]]')

    def test_string_with_brackets(self):
        result = _find_balanced_json('[["[nested]", 1]]', '[', ']')
        self.assertEqual(result, '[["[nested]", 1]]')

    def test_no_match_returns_empty(self):
        result = _find_balanced_json('no json here', '[', ']')
        self.assertEqual(result, '')

    def test_unbalanced_returns_empty(self):
        result = _find_balanced_json('[[0, 1]', '[', ']')
        self.assertEqual(result, '')


class LoadJsonCandidateTests(unittest.TestCase):
    def test_plain_json_array(self):
        result = _load_json_candidate('[[0, 1], [2, 3]]')
        self.assertEqual(result, [[0, 1], [2, 3]])

    def test_code_fence_wrapped(self):
        result = _load_json_candidate('```json\n[{"a": 1}]\n```')
        self.assertEqual(result, [{'a': 1}])

    def test_bom_stripped(self):
        result = _load_json_candidate('\ufeff[1, 2, 3]')
        self.assertEqual(result, [1, 2, 3])

    def test_nested_object(self):
        result = _load_json_candidate('{"data": {"nested": true}}')
        self.assertEqual(result, {'data': {'nested': True}})

    def test_invalid_json_raises(self):
        with self.assertRaises(AISegmentationError):
            _load_json_candidate('not json at all')

    def test_empty_raises(self):
        with self.assertRaises(AISegmentationError):
            _load_json_candidate('')


# ---------------------------------------------------------------------------
# 索引范围映射回 cue
# ---------------------------------------------------------------------------

class CuesFromIndexRangesTests(unittest.TestCase):
    def test_normal_mapping(self):
        words = [_make_word('hello', 0, 0.5), _make_word('world', 0.5, 1.0)]
        ranges = [(0, 1)]
        cues = _cues_from_index_ranges(ranges, words, 'whisper')
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, 'helloworld')
        self.assertEqual(cues[0].start_s, 0.0)
        self.assertEqual(cues[0].end_s, 1.0)
        self.assertEqual(cues[0].timing_source, 'ai')

    def test_multiple_ranges(self):
        words = [_make_word(f'w{i}', i, i + 0.5) for i in range(6)]
        ranges = [(0, 2), (3, 5)]
        cues = _cues_from_index_ranges(ranges, words, 'whisper')
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, 'w0w1w2')
        self.assertEqual(cues[1].text, 'w3w4w5')

    def test_out_of_bounds_skipped_with_warning(self):
        words = [_make_word('hello', 0, 0.5)]
        ranges = [(0, 0), (-1, 0), (0, 5)]
        logger = MagicMock()
        cues = _cues_from_index_ranges(ranges, words, 'whisper', logger=logger)
        self.assertEqual(len(cues), 1)  # 只有 (0,0) 有效
        self.assertEqual(logger.warning.call_count, 2)  # 两次越界警告

    def test_empty_text_skipped(self):
        words = [_make_word('', 0, 0.5)]
        ranges = [(0, 0)]
        cues = _cues_from_index_ranges(ranges, words, 'whisper')
        self.assertEqual(len(cues), 0)

    def test_empty_ranges(self):
        words = [_make_word('hello', 0, 0.5)]
        cues = _cues_from_index_ranges([], words, 'whisper')
        self.assertEqual(len(cues), 0)


# ---------------------------------------------------------------------------
# rhythm_enabled 配置
# ---------------------------------------------------------------------------

class RhythmEnabledConfigTests(unittest.TestCase):
    def test_default_rhythm_disabled(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config())
        self.assertFalse(cfg.rhythm_enabled)

    def test_rhythm_enabled_from_config(self):
        cfg = AISegmentationConfig.from_app_config(_base_app_config(AI_SEGMENTATION_RHYTHM_ENABLED=True))
        self.assertTrue(cfg.rhythm_enabled)


# ---------------------------------------------------------------------------
# 句子边界检测
# ---------------------------------------------------------------------------

class CrossesSentenceBoundaryTests(unittest.TestCase):
    def test_period_then_uppercase_is_boundary(self):
        self.assertTrue(_crosses_sentence_boundary('Hello world.', 'Next sentence.'))

    def test_period_then_cjk_is_boundary(self):
        self.assertTrue(_crosses_sentence_boundary('Hello world.', '下一个句子'))

    def test_comma_then_text_is_not_boundary(self):
        self.assertFalse(_crosses_sentence_boundary('Hello,', 'world'))

    def test_no_punct_is_not_boundary(self):
        self.assertFalse(_crosses_sentence_boundary('Hello', 'world'))

    def test_empty_text_is_not_boundary(self):
        self.assertFalse(_crosses_sentence_boundary('', 'world'))
        self.assertFalse(_crosses_sentence_boundary('hello', ''))


# ---------------------------------------------------------------------------
# 超长切分：中文逗号/顿号
# ---------------------------------------------------------------------------

class SplitLongCueChineseCommaTests(unittest.TestCase):
    def test_split_at_chinese_comma(self):
        """含中文逗号的超长 cue 应在逗号处切分，而非中点。"""
        cue = AlignedSubtitleCue(
            start_s=0, end_s=10,
            text='这是第一部分的内容，这是第二部分的内容',
        )
        out = _split_long_cue(cue, max_duration_s=3.0)
        self.assertGreater(len(out), 1)
        # 验证切分结果拼接后与原文一致
        combined = ''.join(c.text for c in out)
        self.assertIn('内容', combined)

    def test_split_at_chinese_dunhao(self):
        """含顿号的超长 cue 应在顿号处切分。"""
        cue = AlignedSubtitleCue(
            start_s=0, end_s=10,
            text='苹果、香蕉、橘子、葡萄、西瓜',
        )
        out = _split_long_cue(cue, max_duration_s=2.0)
        self.assertGreater(len(out), 1)

    def test_no_punct_uses_fallback(self):
        """无标点的超长 cue 应使用虚词/中点兜底。"""
        cue = AlignedSubtitleCue(
            start_s=0, end_s=10,
            text='这是一个没有任何标点符号的很长很长很长的中文句子',
        )
        out = _split_long_cue(cue, max_duration_s=3.0)
        self.assertGreater(len(out), 1)
        # 不应在单个汉字中间劈开
        for c in out:
            self.assertGreater(len(c.text.strip()), 0)


# ---------------------------------------------------------------------------
# 断裂句修复
# ---------------------------------------------------------------------------

class RepairBrokenSentencesTests(unittest.TestCase):
    def test_merges_function_word_ending(self):
        """以虚词"的"结尾的 cue 应与下一条合并。"""
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=2, text='这是我最喜欢的'),
            AlignedSubtitleCue(start_s=2, end_s=4, text='游戏'),
        ]
        out = _repair_broken_sentences(cues, max_duration_s=6.0, max_cps=15.0)
        self.assertEqual(len(out), 1)
        self.assertIn('最喜欢', out[0].text)
        self.assertIn('游戏', out[0].text)

    def test_does_not_merge_sentence_end(self):
        """以句号结尾的 cue 不应与下一条合并。"""
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=2, text='这是第一句。'),
            AlignedSubtitleCue(start_s=2, end_s=4, text='这是第二句。'),
        ]
        out = _repair_broken_sentences(cues, max_duration_s=6.0, max_cps=15.0)
        self.assertEqual(len(out), 2)

    def test_does_not_merge_when_duration_exceeded(self):
        """合并后超长则不合并。"""
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=5.5, text='这是一个很长的句子的'),
            AlignedSubtitleCue(start_s=5.5, end_s=10, text='后续部分也很长'),
        ]
        out = _repair_broken_sentences(cues, max_duration_s=6.0, max_cps=15.0)
        self.assertEqual(len(out), 2)  # 合并后 10s > 6s，不合并

    def test_empty_input(self):
        self.assertEqual(_repair_broken_sentences([], 6.0, 15.0), [])

    def test_single_cue(self):
        cues = [AlignedSubtitleCue(start_s=0, end_s=2, text='的')]
        out = _repair_broken_sentences(cues, 6.0, 15.0)
        self.assertEqual(len(out), 1)


# ---------------------------------------------------------------------------
# enforce_rhythm 句子边界守卫
# ---------------------------------------------------------------------------

class EnforceRhythmBoundaryGuardTests(unittest.TestCase):
    def _cfg(self, **kw):
        return AISegmentationConfig.from_app_config(_base_app_config(**kw))

    def test_does_not_merge_across_sentence_boundary(self):
        """enforce_rhythm 不应跨越句号合并两条独立句子。"""
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=1.5, AI_SEGMENTATION_MAX_CUE_DURATION_S=6.0)
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=0.8, text='好。'),  # 短，但以句号结尾
            AlignedSubtitleCue(start_s=0.8, end_s=3.0, text='这是新的句子'),
        ]
        out = enforce_rhythm(cues, cfg)
        # 不应合并——跨越了句子边界
        self.assertEqual(len(out), 2)

    def test_merges_continuation_without_boundary(self):
        """enforce_rhythm 应合并以逗号结尾的短 cue 与续接的 cue。"""
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=1.5, AI_SEGMENTATION_MAX_CUE_DURATION_S=6.0)
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=0.8, text='但是，'),  # 短，以逗号结尾
            AlignedSubtitleCue(start_s=0.8, end_s=3.0, text='这确实是续接的内容'),
        ]
        out = enforce_rhythm(cues, cfg)
        self.assertEqual(len(out), 1)


if __name__ == '__main__':
    unittest.main()
