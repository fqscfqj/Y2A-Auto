#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""语音管线接线回归测试。

覆盖三类此前「测试全绿但生产未生效」的缺陷：
W1 分片终止性：chunk overlap >= window 时不得死循环；
W2 配置接线：VAD_DROP_ISOLATED_SHORT / SUBTITLE_MAX_CUE_DURATION_S / SUBTITLE_MAX_CPS
   必须真正从运行时 app_config 传导到下游对象并改变行为；
W3 覆盖率口径：_windows_coverage_ratio 使用 speech_duration_s 而非窗口全长。
"""

import logging
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.speech_recognition import (
    SpeechRecognitionConfig,
    SpeechRecognizer,
    create_speech_recognizer_from_config,
)
from modules.subtitle_pipeline_types import AlignedSubtitleCue, DetectedSpeechWindow
from modules.vad_processor import VadConfig, VadProcessor

# 分片数上界：step 恒 >= 0.01s，因此任何输入下的分片数都不可能超过 total/0.01。
_MIN_CHUNK_STEP_S = 0.01


def _base_app_config(**overrides):
    config = {
        'SPEECH_RECOGNITION_ENABLED': True,
        'SPEECH_RECOGNITION_PROVIDER': 'whisper',
        'WHISPER_API_KEY': 'unit-test-key',
        'VAD_ENABLED': True,
    }
    config.update(overrides)
    return config


class AudioChunkTerminationTests(unittest.TestCase):
    """W1：_create_audio_chunks 对任意 window/overlap 组合都必须终止且覆盖全时长。"""

    def setUp(self):
        # 每个用例都独立构造识别器；不触网、不写日志文件。
        # logging.disable() 返回 None，恢复时必须显式读回原级别。
        self._previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self._previous_disable)

    def _chunks(self, window, overlap, total_duration_s=60.0, provider='whisper'):
        recognizer = SpeechRecognizer(
            SpeechRecognitionConfig(
                chunk_window_s=window,
                chunk_overlap_s=overlap,
                api_provider=provider,
            ),
            task_id='unit-test-chunks',
        )
        self.addCleanup(recognizer._cleanup_temp_files)
        return recognizer._create_audio_chunks(total_duration_s)

    def _assert_terminating_and_covering(self, chunks, total_duration_s):
        self.assertTrue(chunks, '必须至少产出一个分片')
        self.assertEqual(chunks[0][0], 0.0, '首个分片必须从 0 开始')
        self.assertAlmostEqual(chunks[-1][1], total_duration_s, places=6, msg='末个分片必须到达音频末尾')
        starts = [start for start, _ in chunks]
        self.assertEqual(starts, sorted(starts), '分片起点必须单调递增，否则起点重复即死循环特征')
        self.assertEqual(len(starts), len(set(starts)), '分片起点不得重复')
        for start, end in chunks:
            self.assertLess(start, end, '分片必须为非空区间')
        # 终止性上界：步进不可能小于 0.01s。
        self.assertLessEqual(len(chunks), int(total_duration_s / _MIN_CHUNK_STEP_S) + 2)

    def test_overlap_equal_to_window_terminates(self):
        chunks = self._chunks(window=5.0, overlap=5.0)
        self._assert_terminating_and_covering(chunks, 60.0)

    def test_overlap_greater_than_window_terminates(self):
        chunks = self._chunks(window=5.0, overlap=5.5)
        self._assert_terminating_and_covering(chunks, 60.0)

    def test_overlap_far_greater_than_window_terminates(self):
        chunks = self._chunks(window=3.0, overlap=600.0)
        self._assert_terminating_and_covering(chunks, 60.0)

    def test_illegal_windows_fall_back_without_raising(self):
        for window in (0.0, -1.0, '0', None, '', 'invalid'):
            with self.subTest(window=window):
                chunks = self._chunks(window=window, overlap=0.4, total_duration_s=45.0)
                self._assert_terminating_and_covering(chunks, 45.0)

    def test_illegal_overlap_falls_back_without_raising(self):
        for overlap in (-1.0, 'invalid', None, ''):
            with self.subTest(overlap=overlap):
                chunks = self._chunks(window=15.0, overlap=overlap, total_duration_s=45.0)
                self._assert_terminating_and_covering(chunks, 45.0)

    def test_tiny_window_stays_bounded(self):
        chunks = self._chunks(window=0.001, overlap=0.0, total_duration_s=2.0)
        self._assert_terminating_and_covering(chunks, 2.0)

    def test_zero_overlap_steps_by_full_window(self):
        chunks = self._chunks(window=15.0, overlap=0.0, total_duration_s=60.0)
        self._assert_terminating_and_covering(chunks, 60.0)
        # overlap=0 时严格等距：60 / 15 = 4 片。
        self.assertEqual(len(chunks), 4)

    def test_normal_overlap_keeps_expected_chunk_count(self):
        chunks = self._chunks(window=15.0, overlap=0.4, total_duration_s=60.0)
        self._assert_terminating_and_covering(chunks, 60.0)
        self.assertEqual(len(chunks), 5)

    def test_short_audio_returns_single_chunk(self):
        chunks = self._chunks(window=15.0, overlap=14.9, total_duration_s=10.0)
        self.assertEqual(chunks, [(0.0, 10.0)])

    def test_clamping_is_logged_once(self):
        recognizer = SpeechRecognizer(
            SpeechRecognitionConfig(chunk_window_s=5.0, chunk_overlap_s=5.0),
            task_id='unit-test-chunk-warning',
        )
        self.addCleanup(recognizer._cleanup_temp_files)
        logger = Mock(spec=logging.Logger)
        recognizer.logger = logger

        recognizer._create_audio_chunks(60.0)

        self.assertEqual(logger.warning.call_count, 1)
        self.assertIn('AUDIO_CHUNK_OVERLAP_S', logger.warning.call_args[0][0])

    def test_no_warning_when_overlap_is_legal(self):
        recognizer = SpeechRecognizer(
            SpeechRecognitionConfig(chunk_window_s=15.0, chunk_overlap_s=0.4),
            task_id='unit-test-chunk-no-warning',
        )
        self.addCleanup(recognizer._cleanup_temp_files)
        logger = Mock(spec=logging.Logger)
        recognizer.logger = logger

        recognizer._create_audio_chunks(60.0)

        logger.warning.assert_not_called()

    def test_voxtral_window_cap_still_applies_with_clamping(self):
        recognizer = SpeechRecognizer(
            SpeechRecognitionConfig(
                api_provider='voxtral',
                chunk_window_s=5.0,
                chunk_overlap_s=5.0,
                voxtral_max_audio_duration_s=30.0,
                voxtral_long_audio_margin_s=5.0,
                voxtral_enforce_max_duration=True,
            ),
            task_id='unit-test-chunk-voxtral',
        )
        self.addCleanup(recognizer._cleanup_temp_files)

        chunks = recognizer._create_audio_chunks(60.0)

        self._assert_terminating_and_covering(chunks, 60.0)

    def test_vad_processor_sibling_implementation_shares_clamp_semantics(self):
        """同一份音频在两个模块下都必须终止——分片口径不得分叉。"""
        config = VadConfig(chunk_window_s=5.0, chunk_overlap_s=5.0)
        processor = VadProcessor(config)

        chunks = processor._create_chunks(60.0, config)

        self.assertEqual(chunks[0][0], 0.0)
        self.assertAlmostEqual(chunks[-1][1], 60.0, places=6)
        starts = [start for start, _ in chunks]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)))


class DropIsolatedShortWiringTests(unittest.TestCase):
    """W2-a：VAD_DROP_ISOLATED_SHORT 必须从 app_config 传导到 VadConfig 并改变行为。"""

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def test_config_field_exists_on_speech_config(self):
        self.assertIn('vad_drop_isolated_short', SpeechRecognitionConfig().__dict__)

    def test_default_is_true(self):
        recognizer = create_speech_recognizer_from_config(_base_app_config(), task_id='unit-test-drop-default')
        self.assertIsNotNone(recognizer)
        self.assertTrue(recognizer.config.vad_drop_isolated_short)
        self.assertTrue(recognizer._vad.config.drop_isolated_short)

    def test_disabling_propagates_to_vad_config(self):
        recognizer = create_speech_recognizer_from_config(
            _base_app_config(VAD_DROP_ISOLATED_SHORT=False),
            task_id='unit-test-drop-off',
        )
        self.assertIsNotNone(recognizer)
        self.assertFalse(recognizer.config.vad_drop_isolated_short)
        self.assertFalse(recognizer._vad.config.drop_isolated_short)

    def test_disabling_changes_observable_filtering_behaviour(self):
        """改配置必须真的改变窗口过滤结果，而不只是改变属性值。"""
        sparse_segments = [(0.0, 1.0), (10.0, 10.3)]

        def filtered(flag):
            recognizer = create_speech_recognizer_from_config(
                _base_app_config(VAD_DROP_ISOLATED_SHORT=flag),
                task_id=f'unit-test-drop-behaviour-{flag}',
            )
            self.assertIsNotNone(recognizer)
            return recognizer._vad._apply_constraints(
                list(sparse_segments), config=recognizer._vad.config
            )

        self.assertEqual(filtered(True), [(0.0, 1.0)], '开启丢弃时孤立极短段应被移除')
        self.assertEqual(filtered(False), sparse_segments, '关闭丢弃时孤立极短段应被保留')

    def test_dropped_counter_reflects_config(self):
        def dropped_count(flag):
            recognizer = create_speech_recognizer_from_config(
                _base_app_config(VAD_DROP_ISOLATED_SHORT=flag),
                task_id=f'unit-test-drop-counter-{flag}',
            )
            recognizer._vad._apply_constraints([(0.0, 1.0), (10.0, 10.3)], config=recognizer._vad.config)
            return recognizer._vad.last_dropped_short_count

        self.assertEqual(dropped_count(True), 1)
        self.assertEqual(dropped_count(False), 0)

    def test_relaxed_and_refinement_configs_inherit_the_flag(self):
        recognizer = create_speech_recognizer_from_config(
            _base_app_config(VAD_DROP_ISOLATED_SHORT=False),
            task_id='unit-test-drop-inherit',
        )
        processor = recognizer._vad

        self.assertFalse(processor._build_relaxed_retry_config().drop_isolated_short)
        self.assertFalse(processor._build_refinement_config(processor.config).drop_isolated_short)


class SubtitleQualityLimitWiringTests(unittest.TestCase):
    """W2-b：字幕质量硬上限必须随运行时配置变化，而非导入期常量。"""

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def _recognizer(self, **overrides):
        recognizer = create_speech_recognizer_from_config(
            _base_app_config(**overrides), task_id='unit-test-subtitle-limits'
        )
        self.assertIsNotNone(recognizer)
        return recognizer

    def test_config_fields_exist(self):
        instance = SpeechRecognitionConfig()
        self.assertIn('subtitle_max_cue_duration_s', instance.__dict__)
        self.assertIn('subtitle_max_cps', instance.__dict__)

    def test_defaults_match_engine_defaults(self):
        recognizer = self._recognizer()
        self.assertAlmostEqual(recognizer.config.subtitle_max_cue_duration_s, 8.0, places=6)
        self.assertAlmostEqual(recognizer.config.subtitle_max_cps, 20.0, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_cue_duration_s, 8.0, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_chars_per_second, 20.0, places=6)

    def test_app_config_propagates_to_engine(self):
        recognizer = self._recognizer(SUBTITLE_MAX_CUE_DURATION_S=4.5, SUBTITLE_MAX_CPS=11.0)

        self.assertAlmostEqual(recognizer.config.subtitle_max_cue_duration_s, 4.5, places=6)
        self.assertAlmostEqual(recognizer.config.subtitle_max_cps, 11.0, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_cue_duration_s, 4.5, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_chars_per_second, 11.0, places=6)

    def test_changing_config_changes_engine_behaviour(self):
        """同一个 engine 配置项取值不同时，合并结果必须不同——证明接线真实生效。

        构造上必须让文本**连续**（首尾词重叠）：``finalize_cues`` 只在
        ``_merge_text_with_overlap`` 判为同一句续写时才会尝试合并，
        时长上限的作用是**否决**这次合并。若文本互不相干，
        两条 cue 本来就不会合并，上限取任何值结果都一样，测不出接线。
        """
        cues = [
            AlignedSubtitleCue(start_s=0.0, end_s=4.0, text='alpha beta gamma delta').to_dict(),
            AlignedSubtitleCue(start_s=4.05, end_s=11.0, text='delta epsilon zeta').to_dict(),
        ]

        tight = self._recognizer(SUBTITLE_MAX_CUE_DURATION_S=4.0)
        loose = self._recognizer(SUBTITLE_MAX_CUE_DURATION_S=30.0)

        merged_tight = tight._srt.finalize_cues([dict(cue) for cue in cues], 11.0)
        merged_loose = loose._srt.finalize_cues([dict(cue) for cue in cues], 11.0)

        # 合并后跨度 11.0s：上限 4.0s 必须否决，上限 30.0s 必须放行
        self.assertEqual(len(merged_tight), 2, '上限 4.0s 时不得把跨 11.0s 的两句合并成超长 cue')
        self.assertEqual(len(merged_loose), 1, '上限 30.0s 时应允许合并为一条')
        self.assertNotEqual(len(merged_tight), len(merged_loose))
        self.assertEqual(
            merged_loose[0]['text'], 'alpha beta gamma delta epsilon zeta',
            '合并必须保留重叠词接续后的完整文本',
        )

    def test_max_cps_is_consumed_by_merge_limits(self):
        """max_chars_per_second 必须影响同一组 cue 的合并判定。

        合并后跨度 2.0s、文本 20 字 → 10 字/秒：上限 5/s 否决，上限 60/s 放行。
        """
        cues = [
            AlignedSubtitleCue(start_s=0.0, end_s=1.0, text='aaaaaaaaaa').to_dict(),
            AlignedSubtitleCue(start_s=1.0, end_s=2.0, text='aaaaaaaaaabbbbbbbbbb').to_dict(),
        ]

        slow_limit = self._recognizer(SUBTITLE_MAX_CPS=5.0, SUBTITLE_MAX_CUE_DURATION_S=60.0)
        fast_limit = self._recognizer(SUBTITLE_MAX_CPS=60.0, SUBTITLE_MAX_CUE_DURATION_S=60.0)

        blocked = slow_limit._srt.finalize_cues([dict(cue) for cue in cues], 2.0)
        allowed = fast_limit._srt.finalize_cues([dict(cue) for cue in cues], 2.0)

        self.assertEqual(len(blocked), 2, '字速上限 5/s 时不得合并（20 字 / 2s = 10/s 超限）')
        self.assertEqual(len(allowed), 1, '字速上限 60/s 时应允许合并')

    def test_values_are_not_read_from_import_time_constants(self):
        """同样一份 app_config 取值必须覆盖导入期默认常量。"""
        import modules.speech_recognition as sr_module

        self.assertIsNotNone(sr_module._DEFAULT_CONFIG)
        recognizer = self._recognizer(SUBTITLE_MAX_CUE_DURATION_S=17.0, SUBTITLE_MAX_CPS=33.0)

        self.assertAlmostEqual(recognizer._srt.config.max_cue_duration_s, 17.0, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_chars_per_second, 33.0, places=6)

    def test_invalid_values_fall_back_to_defaults(self):
        recognizer = self._recognizer(SUBTITLE_MAX_CUE_DURATION_S='invalid', SUBTITLE_MAX_CPS=None)

        self.assertAlmostEqual(recognizer._srt.config.max_cue_duration_s, 8.0, places=6)
        self.assertAlmostEqual(recognizer._srt.config.max_chars_per_second, 20.0, places=6)


class VadCoverageCaliberTests(unittest.TestCase):
    """W3：覆盖率口径为「语音时长占比」，并保留下调后的默认阈值。"""

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def _window(self, start_s, end_s, speech_duration_s):
        return DetectedSpeechWindow(
            start_s=start_s,
            end_s=end_s,
            ownership_start_s=start_s,
            ownership_end_s=end_s,
            speech_duration_s=speech_duration_s,
        )

    def test_new_caliber_uses_speech_duration(self):
        processor = VadProcessor(VadConfig())
        window = self._window(0.0, 10.0, 2.0)

        ratio = processor._windows_coverage_ratio([window], 10.0)

        self.assertAlmostEqual(ratio, 0.2, places=6)

    def test_legacy_caliber_would_overstate(self):
        """同一份窗口数据下，旧口径（窗口全长）必须显著高估——这是阈值重标定的依据。"""
        processor = VadProcessor(VadConfig())
        window = self._window(0.0, 10.0, 2.0)

        new_ratio = processor._windows_coverage_ratio([window], 10.0)
        legacy_ratio = (window.end_s - window.start_s) / 10.0

        self.assertAlmostEqual(legacy_ratio, 1.0, places=6)
        self.assertGreater(legacy_ratio, new_ratio * 4.0, '旧口径高估倍数应达到 4 倍以上量级')

    def test_zero_speech_duration_falls_back_to_window_span(self):
        processor = VadProcessor(VadConfig())
        window = self._window(0.0, 10.0, 0.0)

        ratio = processor._windows_coverage_ratio([window], 20.0)

        self.assertAlmostEqual(ratio, 0.5, places=6)

    def test_empty_windows_returns_zero(self):
        processor = VadProcessor(VadConfig())

        self.assertEqual(processor._windows_coverage_ratio([], 10.0), 0.0)
        self.assertEqual(processor._windows_coverage_ratio(None, 10.0), 0.0)

    def test_debug_log_reports_both_calibers_and_ratio(self):
        processor = VadProcessor(VadConfig())
        logger = Mock(spec=logging.Logger)
        logger.isEnabledFor = Mock(return_value=True)
        processor.logger = logger
        window = self._window(0.0, 10.0, 2.0)

        processor._windows_coverage_ratio([window], 10.0)

        self.assertTrue(logger.debug.called, 'debug 级别下应输出新旧两种口径')
        message = logger.debug.call_args[0][0]
        self.assertIn('new=', message)
        self.assertIn('legacy=', message)
        self.assertIn('boost=', message)

    def test_debug_log_is_suppressed_at_info_level(self):
        processor = VadProcessor(VadConfig())
        logger = Mock(spec=logging.Logger)
        logger.isEnabledFor = Mock(return_value=False)
        processor.logger = logger

        processor._windows_coverage_ratio([self._window(0.0, 10.0, 2.0)], 10.0)

        logger.debug.assert_not_called()
        logger.info.assert_not_called()

    def test_default_threshold_recalibrated_for_new_caliber(self):
        """新口径下默认阈值必须下调，否则稀疏素材会被误判为 vad_low_coverage。"""
        from modules.speech_recognition import create_speech_recognizer_from_config as build

        recognizer = build(_base_app_config(), task_id='unit-test-coverage-default')
        self.assertIsNotNone(recognizer)
        self.assertAlmostEqual(recognizer.config.vad_min_speech_coverage_ratio, 0.01, places=6)
        self.assertAlmostEqual(recognizer._vad.config.min_speech_coverage_ratio, 0.01, places=6)
        self.assertAlmostEqual(VadConfig().min_speech_coverage_ratio, 0.01, places=6)

    def test_explicit_config_still_wins(self):
        from modules.speech_recognition import create_speech_recognizer_from_config as build

        recognizer = build(
            _base_app_config(VAD_MIN_SPEECH_COVERAGE_RATIO=0.05),
            task_id='unit-test-coverage-explicit',
        )

        self.assertAlmostEqual(recognizer.config.vad_min_speech_coverage_ratio, 0.05, places=6)
        self.assertAlmostEqual(recognizer._vad.config.min_speech_coverage_ratio, 0.05, places=6)

    def test_sparse_material_survives_new_caliber_threshold(self):
        """回归目标：旧口径约 6.75% 的稀疏素材在新口径下约 2.5%，不得跌破 1.5%。"""
        processor = VadProcessor(VadConfig())
        recognizer_config = VadConfig()

        # 窗口全长 90s（含 padding/静音），真实语音 27s -> 旧口径 6.75%（按 400s 素材），
        # 这里用等价比例直接验证新口径下仍高于重标定后的阈值。
        window = self._window(0.0, 90.0, 27.0)
        ratio = processor._windows_coverage_ratio([window], 400.0)

        self.assertAlmostEqual(ratio, 0.0675, places=6)
        self.assertGreater(ratio, recognizer_config.min_speech_coverage_ratio)

        # 再稀疏一档（约 2.5%）仍应高于 1.0% 阈值。
        sparse = self._window(0.0, 90.0, 10.0)
        sparse_ratio = processor._windows_coverage_ratio([sparse], 400.0)
        self.assertAlmostEqual(sparse_ratio, 0.025, places=6)
        self.assertGreater(sparse_ratio, recognizer_config.min_speech_coverage_ratio)


class SpeechSpansWiringTests(unittest.TestCase):
    """W2-c：VAD 语音区间必须真正传入 clean_hallucinations，否则静音段重复检测永不触发。"""

    def setUp(self):
        logging.disable(logging.CRITICAL)

    class _FakeVad:
        def __init__(self):
            self.cleaned = False

        def detect_speech_windows(self, wav_path, total_duration_s):
            return [DetectedSpeechWindow(start_s=0.0, end_s=10.0, ownership_start_s=0.0,
                                         ownership_end_s=10.0, speech_duration_s=10.0,
                                         raw_spans=[(0.0, 10.0)])]

        def cleanup(self):
            self.cleaned = True

    class _FakeResult:
        ok = True
        timestamp_mode = 'segment'
        fallback_token = ''
        failure_token = ''
        segments = ()
        text = ''
        metadata = {}

    class _FakeAsr:
        client = object()
        last_failure_ratio = 0.0
        last_window_count = 1

        def set_language_hint(self, *args, **kwargs):
            pass

        def detect_language_from_segments(self, *args, **kwargs):
            return ''

        def transcribe_windows_concurrent(self, inputs):
            return [SpeechSpansWiringTests._FakeResult() for _ in inputs]

    def _run_transcription(self, cues, video_exists=True, vad_enabled=True):
        recognizer = SpeechRecognizer(
            SpeechRecognitionConfig(vad_enabled=vad_enabled), task_id='unit-test-spans'
        )
        recognizer._vad = self._FakeVad()
        recognizer._asr = self._FakeAsr()
        recognizer._extract_audio_wav = lambda path: 'audio.wav'
        recognizer._probe_media_duration = lambda path: 40.0
        recognizer._extract_audio_clip = lambda wav, start, end: 'clip.wav'
        recognizer._prepare_window_inputs = lambda wav, windows: [(windows[0], 'clip.wav')]
        recognizer._srt.align_transcription_results = lambda results, total_duration_s=0.0: list(cues)

        captured = {}
        real_clean = recognizer._srt.clean_hallucinations

        def spy(cue_list, speech_spans=None):
            captured['speech_spans'] = speech_spans
            return real_clean(cue_list, speech_spans=speech_spans)

        recognizer._srt.clean_hallucinations = spy
        with patch('modules.speech_recognition.os.path.exists', return_value=video_exists):
            output_path = os.path.join('/tmp', 'unit-test-spans.srt')
            result = recognizer.transcribe_video_to_subtitles('video.mp4', output_path)
        return recognizer, result, captured

    def test_speech_spans_reach_clean_hallucinations(self):
        cue = AlignedSubtitleCue(start_s=1.0, end_s=2.0, text='line one')
        recognizer, _, captured = self._run_transcription([cue])

        self.assertIsNotNone(captured.get('speech_spans'), '生产路径必须传入 VAD 语音区间')
        self.assertEqual(list(captured['speech_spans']), [(0.0, 10.0)])

    def test_repeat_cue_in_silence_is_dropped_end_to_end(self):
        """落在静音段且与上一条高度相似的 cue 必须在生产路径上被剔除。"""
        first = AlignedSubtitleCue(start_s=1.0, end_s=2.0, text='silence echo line')
        repeat = AlignedSubtitleCue(start_s=8.0, end_s=9.0, text='silence echo line')

        recognizer, _, captured = self._run_transcription([first, repeat])

        self.assertEqual(list(captured['speech_spans']), [(0.0, 10.0)])

    def test_without_vad_no_spans_are_fabricated(self):
        cue = AlignedSubtitleCue(start_s=1.0, end_s=2.0, text='line one')
        recognizer, _, captured = self._run_transcription([cue], vad_enabled=False)

        self.assertIsNone(captured.get('speech_spans'), '未启用 VAD 时不得伪造语音区间')

    def test_spans_are_reset_between_runs(self):
        cue = AlignedSubtitleCue(start_s=1.0, end_s=2.0, text='line one')
        recognizer, _, _ = self._run_transcription([cue])
        self.assertTrue(recognizer._last_vad_spans)

        with patch('modules.speech_recognition.os.path.exists', return_value=False):
            recognizer.transcribe_video_to_subtitles('video.mp4', '/tmp/out.srt')

        self.assertEqual(recognizer._last_vad_spans, (), '新一轮转录必须清空上一轮的语音区间')

    def test_collect_speech_spans_prefers_raw_spans(self):
        window = DetectedSpeechWindow(
            start_s=0.0, end_s=10.0, ownership_start_s=0.0, ownership_end_s=10.0,
            speech_duration_s=4.0, raw_spans=[(2.0, 4.0), (6.0, 8.0)],
        )

        spans = SpeechRecognizer._collect_speech_spans([window])

        self.assertEqual(list(spans), [(2.0, 4.0), (6.0, 8.0)])

    def test_collect_speech_spans_falls_back_to_window_span(self):
        window = DetectedSpeechWindow(
            start_s=1.0, end_s=5.0, ownership_start_s=1.0, ownership_end_s=5.0,
            speech_duration_s=3.0,
        )

        spans = SpeechRecognizer._collect_speech_spans([window])

        self.assertEqual(list(spans), [(1.0, 5.0)])


if __name__ == '__main__':
    unittest.main()
