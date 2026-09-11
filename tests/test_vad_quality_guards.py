"""VAD 质量守卫回归测试（B1-B7）。

覆盖七项已审计确认的缺陷修复：
B1 chunk 窗口下限与窄 cap 告警；B2 部分分片失败可观测/拒收；B3 chunk 内 merge_gap；
B4 孤立极短段策略；B5 精修分级与诊断；B6 覆盖率口径；B7 切片临时文件生命周期。
"""

import logging
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch

from modules.subtitle_pipeline_types import DetectedSpeechWindow
from modules.vad_processor import VadConfig, VadProcessor


def _write_wav(path, duration_s=1.0, sample_rate=16000):
    with wave.open(path, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b'\x00\x00' * int(sample_rate * duration_s))
    return path


class VadQualityGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a_vad_guard_test_')
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _make_window(self, start_s, end_s, speech_duration_s=None, source_pass='scan'):
        return DetectedSpeechWindow(
            start_s=start_s,
            end_s=end_s,
            ownership_start_s=start_s,
            ownership_end_s=end_s,
            source_pass=source_pass,
            speech_duration_s=(
                float(end_s - start_s) if speech_duration_s is None else float(speech_duration_s)
            ),
        )

    def _build_chunk_scenario(self, outcomes, chunk_window_s=15.0):
        """构造 _detect_chunked 场景：outcomes[i] 为 True 表示第 i 个 chunk 推理成功。

        切片文件按 ``fake_{chunk_index}.wav`` 命名，使 _run_vad_on_audio 能反查 chunk。
        """
        config = VadConfig(
            chunk_window_s=chunk_window_s,
            chunk_overlap_s=0.4,
            refinement_enabled=False,
        )
        processor = VadProcessor(config, logger=logging.getLogger('vad_guard_test'))
        total_duration_s = chunk_window_s * len(outcomes)
        chunks = [(i * chunk_window_s, (i + 1) * chunk_window_s) for i in range(len(outcomes))]

        def fake_extract(wav_path, start_s, end_s):
            index = int(round(float(start_s) / chunk_window_s))
            clip_path = os.path.join(self._tmp, f'fake_{index:03d}.wav')
            with open(clip_path, 'wb') as handle:
                handle.write(b'RIFF')
            return clip_path

        def fake_run(wav_path, duration_s, cfg):
            index = int(os.path.basename(wav_path).split('_')[1].split('.')[0])
            return [(0.4, 1.0)] if outcomes[index] else None

        for target, replacement in (
            ('_create_chunks', Mock(return_value=chunks)),
            ('_extract_audio_clip', Mock(side_effect=fake_extract)),
            ('_run_vad_on_audio', Mock(side_effect=fake_run)),
        ):
            patcher = patch.object(processor, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return processor, total_duration_s

    # ---------------- B1 窗口下限 ----------------

    def test_chunk_window_keeps_floor_when_cap_is_narrow(self):
        for cap in (3.0, 1.0, 0.05):
            processor = VadProcessor(VadConfig(chunk_window_s=15.0, max_segment_s=cap))
            self.assertEqual(processor._effective_chunk_window_s(), 3.0)

    def test_chunk_window_keeps_floor_when_window_is_tiny(self):
        processor = VadProcessor(VadConfig(chunk_window_s=0.05, max_segment_s=15.0))
        self.assertEqual(processor._effective_chunk_window_s(), 3.0)

    def test_vad_cap_semantics_unchanged(self):
        processor = VadProcessor(VadConfig(max_segment_s=3.0))
        self.assertEqual(processor._effective_vad_cap_s(), 3.0)

    def test_narrow_cap_warning_logged_once(self):
        processor = VadProcessor(VadConfig(chunk_window_s=15.0, max_segment_s=3.0))
        logger = Mock(spec=logging.Logger)
        processor.logger = logger

        processor._effective_chunk_window_s()
        processor._effective_chunk_window_s()
        processor._effective_chunk_window_s()

        self.assertEqual(logger.warning.call_count, 1)
        self.assertIn('VAD_MAX_SEGMENT_S', logger.warning.call_args[0][0])
        self.assertTrue(processor._warned_narrow_cap)

    def test_wide_cap_does_not_warn(self):
        processor = VadProcessor(VadConfig(chunk_window_s=15.0, max_segment_s=15.0))
        logger = Mock(spec=logging.Logger)
        processor.logger = logger

        processor._effective_chunk_window_s()

        logger.warning.assert_not_called()
        self.assertFalse(processor._warned_narrow_cap)

    def test_one_hour_audio_chunk_count_stays_bounded(self):
        config = VadConfig(chunk_window_s=15.0, max_segment_s=3.0)
        processor = VadProcessor(config)
        self.assertLess(len(processor._create_chunks(3600.0, config)), 1500)

    # ---------------- B2 部分失败可观测 ----------------

    def test_all_chunks_failed_returns_none(self):
        processor, total_duration_s = self._build_chunk_scenario([False, False, False])
        result = processor._detect_chunked(
            'input.wav', total_duration_s, processor.config, source_pass='scan'
        )
        self.assertIsNone(result)

    def test_majority_failed_after_first_success_returns_none(self):
        processor, total_duration_s = self._build_chunk_scenario([True] + [False] * 9)
        result = processor._detect_chunked(
            'input.wav', total_duration_s, processor.config, source_pass='scan'
        )
        self.assertIsNone(result)

    def test_exactly_half_failed_returns_none(self):
        processor, total_duration_s = self._build_chunk_scenario([True, False])
        result = processor._detect_chunked(
            'input.wav', total_duration_s, processor.config, source_pass='scan'
        )
        self.assertIsNone(result)

    def test_minority_failed_returns_windows_and_partial_state(self):
        processor, total_duration_s = self._build_chunk_scenario([True] * 9 + [False])
        result = processor._detect_chunked(
            'input.wav', total_duration_s, processor.config, source_pass='scan'
        )
        self.assertTrue(result)
        self.assertEqual(processor.last_result_state, 'partial')
        self.assertEqual(processor.last_failure_reason, 'vad_partial_chunks')

    def test_partial_failure_ratio_logged(self):
        processor, total_duration_s = self._build_chunk_scenario([True] * 9 + [False])
        logger = Mock(spec=logging.Logger)
        processor.logger = logger

        processor._detect_chunked(
            'input.wav', total_duration_s, processor.config, source_pass='scan'
        )

        self.assertTrue(logger.warning.called)

    # ---------------- B3 chunk 内 merge_gap ----------------

    def test_gap_merge_uses_configured_gap_when_allowed(self):
        config = VadConfig(merge_gap_s=1.0)
        processor = VadProcessor(config)

        merged = processor._apply_constraints(
            [(0.0, 1.0), (1.5, 2.0)], config=config, allow_gap_merge=True
        )

        self.assertEqual(merged, [(0.0, 2.0)])

    def test_stitch_gap_blocks_wide_merge_when_not_allowed(self):
        config = VadConfig(merge_gap_s=1.0)
        processor = VadProcessor(config)

        merged = processor._apply_constraints(
            [(0.0, 1.0), (1.5, 2.0)],
            config=config,
            allow_gap_merge=False,
            stitch_gap_s=0.22,
        )

        self.assertEqual(merged, [(0.0, 1.0), (1.5, 2.0)])

    def test_explicit_stitch_gap_widens_cross_chunk_merge(self):
        config = VadConfig(merge_gap_s=0.0)
        processor = VadProcessor(config)

        merged = processor._apply_constraints(
            [(0.0, 1.0), (1.5, 2.0)],
            config=config,
            allow_gap_merge=False,
            stitch_gap_s=0.6,
        )

        self.assertEqual(merged, [(0.0, 2.0)])

    def test_merge_windows_accepts_stitch_gap(self):
        config = VadConfig(merge_gap_s=5.0)
        processor = VadProcessor(config)
        windows = [self._make_window(0.0, 1.0), self._make_window(3.0, 4.0)]

        merged = processor._merge_windows(
            windows, config=config, allow_gap_merge=False, stitch_gap_s=0.22
        )

        self.assertEqual(len(merged), 2)

    def test_chunked_pass_uses_configured_merge_gap(self):
        # chunk 内 0.5s 停顿：merge_gap_s=1.0 时应合并，默认 0.35 时不应合并
        # （旧实现一律用 chunk_overlap_s/2+0.02 的缝合间隙，用户调参无效）。
        for merge_gap_s, expected_count in ((1.0, 1), (0.2, 2)):
            config = VadConfig(
                chunk_window_s=15.0,
                chunk_overlap_s=0.4,
                merge_gap_s=merge_gap_s,
                refinement_enabled=False,
            )
            processor = VadProcessor(config, logger=logging.getLogger('vad_guard_test'))
            patchers = [
                patch.object(processor, '_create_chunks', Mock(return_value=[(0.0, 15.0)])),
                patch.object(
                    processor, '_extract_audio_clip', Mock(return_value=os.path.join(self._tmp, 'c.wav'))
                ),
                patch.object(
                    processor, '_run_vad_on_audio', Mock(return_value=[(0.4, 1.0), (1.5, 2.0)])
                ),
            ]
            for patcher in patchers:
                patcher.start()
                self.addCleanup(patcher.stop)

            result = processor._detect_chunked('input.wav', 15.0, config, source_pass='scan')

            self.assertEqual(len(result), expected_count, msg=f'merge_gap_s={merge_gap_s}')

    # ---------------- B4 孤立极短段策略 ----------------

    def test_isolated_short_segment_dropped_by_default(self):
        config = VadConfig()
        processor = VadProcessor(config)

        result = processor._apply_constraints([(0.0, 1.0), (10.0, 10.3)], config=config)

        self.assertEqual(result, [(0.0, 1.0)])
        self.assertEqual(processor.last_dropped_short_count, 1)

    def test_isolated_short_segment_kept_when_drop_disabled(self):
        config = VadConfig(drop_isolated_short=False)
        processor = VadProcessor(config)

        result = processor._apply_constraints([(0.0, 1.0), (10.0, 10.3)], config=config)

        self.assertEqual(result, [(0.0, 1.0), (10.0, 10.3)])
        self.assertEqual(processor.last_dropped_short_count, 0)

    def test_short_segment_above_drop_threshold_is_kept(self):
        # 0.5s >= drop 阈值 max(0.4, min_segment_s*0.5=0.4)，仍应保留。
        config = VadConfig()
        processor = VadProcessor(config)

        result = processor._apply_constraints([(0.0, 1.0), (10.0, 10.5)], config=config)

        self.assertEqual(result, [(0.0, 1.0), (10.0, 10.5)])
        self.assertEqual(processor.last_dropped_short_count, 0)

    def test_drop_threshold_scales_with_min_segment(self):
        # min_segment_s=1.2 → 阈值 max(0.4, 0.6)=0.6，0.55s 孤立段应被丢弃。
        config = VadConfig(min_segment_s=1.2)
        processor = VadProcessor(config)

        result = processor._apply_constraints([(0.0, 2.0), (10.0, 10.55)], config=config)

        self.assertEqual(result, [(0.0, 2.0)])
        self.assertEqual(processor.last_dropped_short_count, 1)

    def test_relaxed_and_refinement_configs_inherit_drop_flag(self):
        config = VadConfig(drop_isolated_short=False)
        processor = VadProcessor(config)

        self.assertFalse(processor._build_relaxed_retry_config().drop_isolated_short)
        self.assertFalse(processor._build_refinement_config(config).drop_isolated_short)

    def test_dropped_short_summary_logged_by_detect_wrapper(self):
        processor = VadProcessor(VadConfig())
        logger = Mock(spec=logging.Logger)
        processor.logger = logger

        def fake_impl(wav_path, total_duration_s):
            # 模拟检测过程中丢弃了两个孤立极短段。
            processor.last_dropped_short_count += 2
            return []

        with patch.object(processor, '_detect_speech_windows_impl', side_effect=fake_impl):
            processor.detect_speech_windows('input.wav', 10.0)

        self.assertTrue(logger.info.called)
        self.assertIn('isolated short segments', logger.info.call_args[0][0])

    # ---------------- B5 精修分级与诊断 ----------------

    def test_refine_drops_window_when_no_local_speech(self):
        config = VadConfig()
        processor = VadProcessor(config)
        window = self._make_window(0.0, 2.0)

        with patch.object(processor, '_extract_audio_clip', return_value='clip.wav'), patch.object(
            processor, '_run_vad_on_audio', return_value=[]
        ):
            result = processor._refine_windows('input.wav', [window], config)

        self.assertEqual(result, [])
        self.assertEqual(processor.last_refine_dropped_count, 1)

    def test_refine_keeps_window_when_refinement_fails(self):
        config = VadConfig()
        processor = VadProcessor(config)
        window = self._make_window(0.0, 2.0)

        with patch.object(processor, '_extract_audio_clip', return_value='clip.wav'), patch.object(
            processor, '_run_vad_on_audio', return_value=None
        ):
            result = processor._refine_windows('input.wav', [window], config)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start_s, 0.0)
        self.assertEqual(result[0].end_s, 2.0)
        self.assertEqual(processor.last_refine_dropped_count, 0)

    def test_short_window_skips_refinement_entirely(self):
        config = VadConfig()
        processor = VadProcessor(config)
        window = self._make_window(0.0, 1.0)

        extract = Mock(return_value='clip.wav')
        run_vad = Mock(return_value=[(0.1, 0.9)])
        with patch.object(processor, '_extract_audio_clip', extract), patch.object(
            processor, '_run_vad_on_audio', run_vad
        ):
            result = processor._refine_windows('input.wav', [window], config)

        run_vad.assert_not_called()
        extract.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].refined)

    def test_refine_preserves_source_pass_and_records_metadata(self):
        config = VadConfig()
        processor = VadProcessor(config)
        window = self._make_window(0.0, 3.0, speech_duration_s=1.8, source_pass='relaxed_retry')

        with patch.object(processor, '_extract_audio_clip', return_value='clip.wav'), patch.object(
            processor, '_run_vad_on_audio', return_value=[(0.5, 2.0)]
        ):
            result = processor._refine_windows('input.wav', [window], config)

        self.assertEqual(len(result), 1)
        refined = result[0]
        self.assertEqual(refined.source_pass, 'relaxed_retry')
        self.assertTrue(refined.refined)
        self.assertTrue(refined.metadata.get('refined'))
        self.assertIn('refine_threshold', refined.metadata)
        self.assertEqual(window.metadata, {}, msg='原窗口 metadata 不得被原地污染')

    def test_refine_dropped_summary_logged(self):
        config = VadConfig()
        processor = VadProcessor(config)
        logger = Mock(spec=logging.Logger)
        processor.logger = logger

        with patch.object(processor, '_extract_audio_clip', return_value='clip.wav'), patch.object(
            processor, '_run_vad_on_audio', return_value=[]
        ):
            processor._refine_windows('input.wav', [self._make_window(0.0, 2.0)], config)

        self.assertTrue(logger.info.called)

    # ---------------- B6 覆盖率口径 ----------------

    def test_coverage_ratio_uses_speech_duration_not_window_length(self):
        processor = VadProcessor(VadConfig())
        window = self._make_window(0.0, 10.0, speech_duration_s=2.0)

        ratio = processor._windows_coverage_ratio([window], 10.0)

        self.assertAlmostEqual(ratio, 0.2, places=6)

    def test_coverage_ratio_falls_back_to_window_length_for_unknown_speech(self):
        processor = VadProcessor(VadConfig())
        window = self._make_window(0.0, 10.0, speech_duration_s=0.0)

        ratio = processor._windows_coverage_ratio([window], 20.0)

        self.assertAlmostEqual(ratio, 0.5, places=6)

    def test_chunk_coverage_denominator_uses_ownership_range(self):
        config = VadConfig(chunk_window_s=15.0, chunk_overlap_s=0.4)
        processor = VadProcessor(config)
        keep_start, keep_end = processor._ownership_range(
            config, chunk_index=0, total_chunks=4, chunk_start=0.0, chunk_end=15.0
        )
        # ownership 区间 = 14.8s，而 chunk 全长为 15.0s：分母口径必须取前者。
        self.assertAlmostEqual(keep_end - keep_start, 14.8, places=6)
        self.assertNotAlmostEqual(keep_end - keep_start, 15.0, places=6)

    # ---------------- B7 临时目录生命周期 ----------------

    def test_clip_extraction_reuses_single_run_directory(self):
        source = _write_wav(os.path.join(self._tmp, 'source.wav'), duration_s=1.0)
        processor = VadProcessor(VadConfig())
        self.addCleanup(processor.cleanup)

        def fake_ffmpeg(cmd, **kwargs):
            _write_wav(cmd[-1], duration_s=0.5)
            return subprocess.CompletedProcess(cmd, 0, '', '')

        with patch('modules.vad_processor.get_ffmpeg_path', return_value='ffmpeg'), patch(
            'modules.vad_processor.subprocess.run', side_effect=fake_ffmpeg
        ):
            paths = [
                processor._extract_audio_clip(source, 0.0, 0.5),
                processor._extract_audio_clip(source, 0.5, 1.0),
                processor._extract_audio_clip(source, 0.0, 0.5),
            ]

        self.assertTrue(all(paths))
        clip_dirs = [d for d in processor._temp_dirs if 'y2a_vad_clip_' in d]
        self.assertEqual(len(clip_dirs), 1)
        self.assertEqual(len(set(paths)), 3)
        for path in paths:
            self.assertEqual(os.path.dirname(path), clip_dirs[0])

    def test_run_vad_releases_clip_file_owned_by_this_run(self):
        clip_dir = tempfile.mkdtemp(prefix='y2a_vad_clip_', dir=self._tmp)
        clip_path = _write_wav(os.path.join(clip_dir, 'clip_000001.wav'), duration_s=1.0)
        source = _write_wav(os.path.join(self._tmp, 'source.wav'), duration_s=1.0)

        processor = VadProcessor(VadConfig())
        processor._clip_dir = clip_dir
        processor._temp_dirs.append(clip_dir)

        get_speech_timestamps = Mock(return_value=[{'start': 0.1, 'end': 0.9}])
        with patch.object(
            processor,
            '_load_silero_vad',
            return_value=(object(), {'get_speech_timestamps': get_speech_timestamps}),
        ):
            result = processor._run_vad_on_audio(clip_path, 1.0, processor.config)

        self.assertEqual(result, [(0.1, 0.9)])
        self.assertFalse(os.path.exists(clip_path), '本轮切片文件应在读入内存后被删除')
        self.assertTrue(os.path.exists(source), '外部传入的原始音频不得被删除')

    def test_run_vad_does_not_delete_foreign_audio(self):
        source = _write_wav(os.path.join(self._tmp, 'source.wav'), duration_s=1.0)
        processor = VadProcessor(VadConfig())
        processor._clip_dir = tempfile.mkdtemp(prefix='y2a_vad_clip_', dir=self._tmp)

        get_speech_timestamps = Mock(return_value=[])
        with patch.object(
            processor,
            '_load_silero_vad',
            return_value=(object(), {'get_speech_timestamps': get_speech_timestamps}),
        ):
            processor._run_vad_on_audio(source, 1.0, processor.config)

        self.assertTrue(os.path.exists(source))

    def test_new_detection_run_resets_clip_directory(self):
        processor = VadProcessor(VadConfig())
        processor._clip_dir = 'stale-dir'
        processor._clip_seq = 42

        with patch.object(processor, '_detect_speech_windows_impl', return_value=[]):
            processor.detect_speech_windows('input.wav', 10.0)

        self.assertIsNone(processor._clip_dir)
        self.assertEqual(processor._clip_seq, 0)

    def test_chunked_run_keeps_single_temp_directory(self):
        """端到端 B7：多分片检测全程只创建一个切片目录，且切片随读随删。"""
        source = _write_wav(os.path.join(self._tmp, 'source.wav'), duration_s=120.0)
        config = VadConfig(chunk_window_s=15.0, chunk_overlap_s=0.4, refinement_enabled=False)
        processor = VadProcessor(config, logger=logging.getLogger('vad_guard_test'))
        self.addCleanup(processor.cleanup)

        def fake_ffmpeg(cmd, **kwargs):
            _write_wav(cmd[-1], duration_s=1.0)
            return subprocess.CompletedProcess(cmd, 0, '', '')

        seen_dirs = []

        def fake_run(wav_path, duration_s, cfg):
            seen_dirs.append(os.path.dirname(wav_path))
            # 真实实现会在读入内存后删除切片；此处复刻该行为以观测目录占用。
            os.remove(wav_path)
            return [(0.2, 0.8)]

        with patch('modules.vad_processor.get_ffmpeg_path', return_value='ffmpeg'), patch(
            'modules.vad_processor.subprocess.run', side_effect=fake_ffmpeg
        ), patch.object(processor, '_run_vad_on_audio', side_effect=fake_run):
            result = processor._detect_chunked(source, 120.0, config, source_pass='scan')

        clip_dirs = [d for d in processor._temp_dirs if 'y2a_vad_clip_' in d]
        self.assertEqual(len(clip_dirs), 1)
        self.assertEqual(set(seen_dirs), set(clip_dirs))
        self.assertTrue(result)
        self.assertEqual(os.listdir(clip_dirs[0]), [], '切片应随读随删，目录内不应残留文件')


class VadPartialStateMachineTests(unittest.TestCase):
    """B2 状态机最小侵入验证：partial 必须穿透 detect_speech_windows 到达上层。"""

    def _window(self):
        return DetectedSpeechWindow(
            start_s=0.0,
            end_s=10.0,
            ownership_start_s=0.0,
            ownership_end_s=10.0,
            speech_duration_s=8.0,
        )

    def test_partial_state_survives_coverage_success_path(self):
        processor = VadProcessor(VadConfig())

        def fake_detect(wav_path, total_duration_s, config, *, source_pass):
            processor.last_result_state = 'partial'
            processor.last_failure_reason = 'vad_partial_chunks'
            return [self._window()]

        with patch.object(processor, '_detect_windows_with_config', side_effect=fake_detect):
            result = processor.detect_speech_windows('input.wav', 10.0)

        self.assertEqual(len(result), 1)
        self.assertEqual(processor.last_result_state, 'partial')
        self.assertEqual(processor.last_failure_reason, 'vad_partial_chunks')
        self.assertAlmostEqual(processor.last_speech_coverage_ratio, 0.8, places=6)

    def test_success_state_preserved_without_partial(self):
        processor = VadProcessor(VadConfig())

        with patch.object(processor, '_detect_windows_with_config', return_value=[self._window()]):
            result = processor.detect_speech_windows('input.wav', 10.0)

        self.assertEqual(len(result), 1)
        self.assertEqual(processor.last_result_state, 'success')
        self.assertEqual(processor.last_failure_reason, '')

    def test_failure_status_and_returns_none(self):
        processor = VadProcessor(VadConfig())

        with patch.object(processor, '_detect_windows_with_config', return_value=None):
            processor._last_run_failure_reason = 'vad_chunk_decode_failed'
            result = processor.detect_speech_windows('input.wav', 10.0)

        self.assertIsNone(result)
        self.assertEqual(processor.last_result_state, 'failure')
        self.assertEqual(processor.last_failure_reason, 'vad_chunk_decode_failed')

    def test_impl_exception_is_contained(self):
        processor = VadProcessor(VadConfig())

        with patch.object(processor, '_detect_speech_windows_impl', side_effect=RuntimeError('boom')):
            result = processor.detect_speech_windows('input.wav', 10.0)

        self.assertIsNone(result)
        self.assertEqual(processor.last_result_state, 'failure')
        self.assertIn('boom', processor.last_failure_reason)

    def test_counters_reset_per_run(self):
        processor = VadProcessor(VadConfig())
        processor.last_dropped_short_count = 7
        processor.last_refine_dropped_count = 5

        with patch.object(processor, '_detect_speech_windows_impl', return_value=[]):
            processor.detect_speech_windows('input.wav', 10.0)

        # 包装器在每轮开始前置零；impl 被 mock 掉故保持 0。
        self.assertEqual(processor.last_dropped_short_count, 0)
        self.assertEqual(processor.last_refine_dropped_count, 0)


if __name__ == '__main__':
    unittest.main()
