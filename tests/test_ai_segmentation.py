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
    _parse_cues_response,
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
        'AI_SEGMENTATION_MIN_CUE_DURATION_S': 0.8,
        'AI_SEGMENTATION_MAX_CUE_DURATION_S': 7.0,
        'AI_SEGMENTATION_MAX_CPS': 18.0,
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

        def fake_word(batch, provider):
            call_state['word_called'] = True
            if word_exc:
                raise word_exc
            return word_response

        def fake_seg(batch, provider):
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
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=0.8, AI_SEGMENTATION_MAX_CUE_DURATION_S=7.0)
        cues = [
            AlignedSubtitleCue(start_s=0, end_s=0.3, text='a'),  # 过短
            AlignedSubtitleCue(start_s=0.3, end_s=2.0, text='b'),
        ]
        out = enforce_rhythm(cues, cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, 'a b')
        self.assertGreaterEqual(out[0].end_s - out[0].start_s, 0.8)

    def test_long_cue_split(self):
        cfg = self._cfg(AI_SEGMENTATION_MIN_CUE_DURATION_S=0.8, AI_SEGMENTATION_MAX_CUE_DURATION_S=3.0)
        cues = [AlignedSubtitleCue(start_s=0, end_s=10, text='first part. second part.')]
        out = enforce_rhythm(cues, cfg)
        self.assertGreater(len(out), 1)
        for c in out:
            self.assertLessEqual(c.end_s - c.start_s, 3.0 + 0.01)

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


if __name__ == '__main__':
    unittest.main()
