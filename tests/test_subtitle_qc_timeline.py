# -*- coding: utf-8 -*-
"""G 组：字幕质检的时间轴维度回归测试。

覆盖 ``modules/subtitle_qc`` 的时间轴硬失败 token、时间轴指标、阈值可配、
AI 不可用不再放行、advisory 覆盖收紧，以及「模块常量 vs 配置默认值」的
一致性锁（本仓库已确认的历史缺陷模式：两套默认值分叉）。

所有用例都显式传入 ``total_duration_s``（不依赖 ffprobe），并 mock
``_call_ai_judge``，不发起任何真实网络请求。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules import subtitle_qc as qc

SENT = "this is a fully descriptive sentence number {} used for quality checking"
SPARSE_TEXT = "alpha bravo charlie delta echo foxtrot golf hotel"

TIMELINE_FAIL_PREFIX = 'rule_fail:timeline'


def _ts(seconds):
    """秒 -> SRT 时间戳 HH:MM:SS,mmm。"""
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3600000)
    minutes, total_ms = divmod(total_ms, 60000)
    secs, ms = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


class _SrtFixtureMixin:
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-qc-timeline-')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_srt(self, cues, name='fixture.srt'):
        """cues: [(start_s, end_s, text), ...]（可重复文本，按给定顺序落盘）。"""
        path = os.path.join(self.tmpdir, name)
        blocks = []
        for index, (start, end, text) in enumerate(cues, 1):
            blocks.append(f"{index}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write("\n".join(blocks) + "\n")
        return path

    @staticmethod
    def uniform(count, cue_len, stride, text_factory=None, start=0.0):
        factory = text_factory or (lambda i: SENT.format(i))
        return [(start + i * stride, start + i * stride + cue_len, factory(i))
                for i in range(count)]

    def qc(self, cues, total_duration_s, config=None, strict=False, ai=None):
        path = self.write_srt(cues)
        with patch.object(qc, '_call_ai_judge',
                          return_value=ai or (None, None, None, 'missing_openai_api_key')):
            return qc.run_subtitle_qc(path, dict(config or {}),
                                      total_duration_s=total_duration_s, strict=strict)


class TimelineThresholdLockTests(unittest.TestCase):
    """模块常量必须与配置默认值单一来源，防止再次分叉。"""

    CONST_TO_KEY = {
        'TIMELINE_MIN_COVERAGE_RATIO': 'SUBTITLE_QC_MIN_COVERAGE_RATIO',
        'TIMELINE_MAX_GAP_SECONDS': 'SUBTITLE_QC_MAX_GAP_S',
        'TIMELINE_CPS_UPPER': 'SUBTITLE_QC_MAX_CPS',
    }

    def test_module_constants_equal_pipeline_defaults(self):
        from modules.speech_pipeline_settings import SPEECH_PIPELINE_DEFAULTS

        for const_name, key in self.CONST_TO_KEY.items():
            self.assertEqual(
                getattr(qc, const_name), float(SPEECH_PIPELINE_DEFAULTS[key]),
                msg=f'{const_name} 与 SPEECH_PIPELINE_DEFAULTS[{key}] 分叉',
            )

    def test_module_constants_equal_default_config(self):
        from modules.config_manager import DEFAULT_CONFIG

        for const_name, key in self.CONST_TO_KEY.items():
            self.assertEqual(
                getattr(qc, const_name), float(DEFAULT_CONFIG[key]),
                msg=f'{const_name} 与 DEFAULT_CONFIG[{key}] 分叉',
            )

    def test_constants_are_derived_not_fallback(self):
        # 派生的证明：拿一个不可能存在的 key 时返回 fallback，
        # 拿真实 key 时必须返回 DEFAULT_CONFIG 的值而不是 fallback。
        from modules.config_manager import DEFAULT_CONFIG

        self.assertEqual(qc._pipeline_timeline_default('__NO_SUCH_KEY__', -1.0), -1.0)
        for _const_name, key in self.CONST_TO_KEY.items():
            self.assertEqual(
                qc._pipeline_timeline_default(key, -1.0), float(DEFAULT_CONFIG[key])
            )
            self.assertNotEqual(qc._pipeline_timeline_default(key, -1.0), -1.0)

    def test_declared_threshold_contract_values(self):
        self.assertEqual(qc.TIMELINE_MIN_COVERAGE_RATIO, 0.15)
        self.assertEqual(qc.TIMELINE_SUSPICIOUS_COVERAGE_RATIO, 0.35)
        self.assertEqual(qc.TIMELINE_MAX_GAP_SECONDS, 90.0)
        self.assertEqual(qc.TIMELINE_MAX_GAP_RATIO, 0.20)
        self.assertEqual(qc.TIMELINE_SUSPICIOUS_MAX_GAP_SECONDS, 20.0)
        self.assertEqual(qc.TIMELINE_MAX_FIRST_CUE_START_RATIO, 0.25)
        self.assertEqual(qc.TIMELINE_SUSPICIOUS_FIRST_CUE_START_RATIO, 0.08)
        self.assertEqual(qc.TIMELINE_MIN_LAST_CUE_END_RATIO, 0.60)
        self.assertEqual(qc.TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO, 0.85)
        self.assertEqual(qc.TIMELINE_MAX_OVERLAP_RATIO, 0.05)
        self.assertEqual(qc.TIMELINE_MAX_CPS_OUTLIER_RATIO, 0.20)
        self.assertEqual(qc.TIMELINE_HARD_STUTTER_RUN, 4)
        self.assertEqual(qc.TIMELINE_SUSPICIOUS_STUTTER_RUN, 3)


class TimelineHardFailTokenTests(_SrtFixtureMixin, unittest.TestCase):
    """七种时间轴硬失败各自能被独立触发。"""

    def test_coverage_too_low(self):
        # 14 条 x 4s 只铺满前 56s，视频 600s -> 覆盖率 0.093
        cues = self.uniform(14, 4.0, 4.0)
        result = self.qc(cues, 600.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_coverage_too_low')
        self.assertLess(result.raw_ai['coverage_ratio'], qc.TIMELINE_MIN_COVERAGE_RATIO)

    def test_truncated_tail(self):
        # 覆盖 0..24s（覆盖率 0.24 高于硬线），但末条结束仅 24% -> 尾部截断
        cues = self.uniform(8, 3.0, 3.0)
        result = self.qc(cues, 100.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_truncated_tail')
        self.assertGreaterEqual(result.raw_ai['coverage_ratio'], qc.TIMELINE_MIN_COVERAGE_RATIO)
        self.assertLess(result.raw_ai['last_cue_end_ratio'], qc.TIMELINE_MIN_LAST_CUE_END_RATIO)

    def test_late_start(self):
        # 首条起点 100/300 = 0.333 > 0.25；覆盖率与尾部均达标
        cues = self.uniform(30, 5.0, 6.0, start=100.0)
        result = self.qc(cues, 300.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_late_start')
        self.assertGreater(result.raw_ai['first_cue_start_ratio'],
                           qc.TIMELINE_MAX_FIRST_CUE_START_RATIO)

    def test_gap_too_large(self):
        # 前段 0..160s 密集，末条 380..388s，中间 220s 空档
        cues = self.uniform(20, 8.0, 8.0) + [(380.0, 388.0, SENT.format(999))]
        result = self.qc(cues, 400.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_gap_too_large')
        self.assertGreater(result.raw_ai['max_gap_seconds'], qc.TIMELINE_MAX_GAP_SECONDS)

    def test_overlap(self):
        # 每条与下一条重叠 10s，相邻对比重叠率 1.0
        cues = self.uniform(10, 20.0, 10.0)
        result = self.qc(cues, 200.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_overlap')
        self.assertGreater(result.raw_ai['overlap_ratio'], qc.TIMELINE_MAX_OVERLAP_RATIO)

    def test_cps_outlier(self):
        # 78 字符塞进 0.3s -> CPS 远超上限，异常占比 1.0
        text = ('alpha bravo charlie delta echo foxtrot golf hotel india juliet '
                'kilo lima mike')
        cues = [(i * 6.0, i * 6.0 + 0.3, text) for i in range(8)]
        result = self.qc(cues, 96.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_cps_outlier')
        self.assertGreater(result.raw_ai['cps_outlier_ratio'],
                           qc.TIMELINE_MAX_CPS_OUTLIER_RATIO)

    def test_stutter_repeat(self):
        # 连续 4 条完全相同文本且间隔 0 -> 结巴刷屏
        cues = [(i * 5.0, i * 5.0 + 5.0, 'repeated identical text sample') for i in range(4)]
        cues += [(30.0 + i * 10.0, 35.0 + i * 10.0,
                  f'distinct tail block number {i} with its own wording here')
                 for i in range(6)]
        result = self.qc(cues, 100.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_stutter_repeat')
        self.assertGreaterEqual(result.raw_ai['stutter_run'], qc.TIMELINE_HARD_STUTTER_RUN)


class TimelineHealthyAndBackCompatTests(_SrtFixtureMixin, unittest.TestCase):
    def test_healthy_subtitle_not_timeline_failed(self):
        # 50 条 x 8s、间隔 12s、总时长 600s -> coverage 0.667
        cues = self.uniform(50, 8.0, 12.0)
        result = self.qc(cues, 600.0)
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX))
        self.assertEqual(result.decision, 'rule_pass')
        self.assertTrue(result.passed)

    def test_sparse_but_legit_subtitle_not_false_killed(self):
        # 反向防误杀：24 条 x 7s、间隔 25s、总时长 600s -> coverage 0.28
        # 这是含大量静音/长镜头的合法视频，绝不能被覆盖率硬线误杀。
        cues = self.uniform(24, 7.0, 25.0)
        result = self.qc(cues, 600.0)
        self.assertLess(result.raw_ai['coverage_ratio'], qc.TIMELINE_SUSPICIOUS_COVERAGE_RATIO)
        self.assertGreaterEqual(result.raw_ai['coverage_ratio'], qc.TIMELINE_MIN_COVERAGE_RATIO)
        self.assertNotEqual(result.reason, 'rule_fail:timeline_coverage_too_low')
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX))

    def test_timeline_metrics_exposed(self):
        cues = self.uniform(50, 8.0, 12.0)
        result = self.qc(cues, 600.0)
        metrics = result.raw_ai
        self.assertTrue(metrics['timeline_checked'])
        for key in ('coverage_ratio', 'last_cue_end_ratio', 'first_cue_start_ratio',
                    'max_gap_seconds', 'max_gap_ratio', 'overlap_ratio',
                    'cps_outlier_ratio', 'stutter_run', 'timeline_union_seconds'):
            self.assertIn(key, metrics, msg=f'缺少时间轴指标 {key}')
        self.assertAlmostEqual(metrics['timeline_union_seconds'], 400.0, places=3)

    def test_no_duration_skips_timeline_dimension(self):
        # 向后兼容：拿不到总时长时必须 timeline_checked=False 且不产生任何
        # rule_fail:timeline* token（含 gap/overlap/cps/stutter 绝对值维度）。
        gap_cues = [(i * 100.0, i * 100.0 + 5.0, SENT.format(i)) for i in range(6)]
        overlap_cues = self.uniform(10, 20.0, 10.0)
        stutter_cues = ([(i * 5.0, i * 5.0 + 5.0, 'repeated identical text sample')
                         for i in range(4)]
                        + [(30.0 + i * 10.0, 35.0 + i * 10.0,
                            f'distinct tail block number {i} with its own wording here')
                           for i in range(6)])
        for cues in (gap_cues, overlap_cues, stutter_cues):
            with self.subTest(cues=len(cues)):
                result = self.qc(cues, None)
                self.assertFalse(result.raw_ai['timeline_checked'])
                self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX),
                                 msg=f'无总时长时仍触发时间轴判定: {result.reason}')

    def test_timeline_disabled_by_config(self):
        cues = self.uniform(14, 2.0, 12.0, text_factory=lambda i: f'{SPARSE_TEXT} item {i:02d}')
        result = self.qc(cues, 240.0, config={'SUBTITLE_QC_TIMELINE_ENABLED': False})
        self.assertFalse(result.raw_ai['timeline_checked'])
        self.assertTrue(result.raw_ai['timeline_disabled_by_config'])
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX))

    def test_coverage_threshold_is_configurable(self):
        # 同一字幕：默认阈值下覆盖率硬失败；把硬线调低到 0.10 后不再覆盖率失败。
        cues = self.uniform(14, 3.0, 20.0, text_factory=lambda i: f'{SPARSE_TEXT} item {i:02d}')

        strict_result = self.qc(cues, 320.0)
        self.assertEqual(strict_result.reason, 'rule_fail:timeline_coverage_too_low')

        relaxed_result = self.qc(cues, 320.0,
                                 config={'SUBTITLE_QC_MIN_COVERAGE_RATIO': 0.10})
        self.assertNotEqual(relaxed_result.reason, 'rule_fail:timeline_coverage_too_low')
        self.assertFalse(relaxed_result.reason.startswith(TIMELINE_FAIL_PREFIX))

    def test_gap_threshold_is_configurable(self):
        # 140s 空档 > 默认 90s 硬线；同时空档占比 0.14 不触发比例分支，
        # 因此只有绝对秒阈值起作用，调高后不再失败。
        cues = self.uniform(20, 8.0, 8.0) + self.uniform(20, 8.0, 16.0, start=300.0)
        default_result = self.qc(cues, 1000.0)
        self.assertEqual(default_result.reason, 'rule_fail:timeline_gap_too_large')
        self.assertGreater(default_result.raw_ai['max_gap_seconds'],
                           qc.TIMELINE_MAX_GAP_SECONDS)
        self.assertLessEqual(default_result.raw_ai['max_gap_ratio'],
                             qc.TIMELINE_MAX_GAP_RATIO)

        relaxed_result = self.qc(cues, 1000.0, config={'SUBTITLE_QC_MAX_GAP_S': 600.0})
        self.assertNotEqual(relaxed_result.reason, 'rule_fail:timeline_gap_too_large')
        self.assertFalse(relaxed_result.reason.startswith(TIMELINE_FAIL_PREFIX))


class AiUnavailableMustNotPassTests(_SrtFixtureMixin, unittest.TestCase):
    def test_suspicious_sample_with_ai_error_not_passed(self):
        cues = self.uniform(24, 7.0, 25.0)
        result = self.qc(cues, 600.0, ai=(None, None, None, 'ai_error:timeout'))
        self.assertEqual(result.raw_ai['boundary_level'], 'suspicious')
        self.assertFalse(result.passed)
        self.assertIn('ai_error_timeout', result.reason)

    def test_strict_mode_never_passes_when_ai_unavailable(self):
        # 规则分很高（boundary、rule_score≈0.97）也必须 strict 不放行。
        cues = self._advisory_cues()
        result = self.qc(cues, 100.0, strict=True, ai=(None, None, None, 'ai_error:timeout'))
        self.assertGreaterEqual(result.rule_score, 0.85)
        self.assertFalse(result.passed)
        self.assertTrue(result.raw_ai['ai_unavailable_strict'])

    def test_non_strict_high_confidence_boundary_still_passes(self):
        # 对照：同样的高置信 boundary 样本，非 strict 时允许放行（qc_skipped）。
        cues = self._advisory_cues()
        result = self.qc(cues, 100.0, strict=False, ai=(None, None, None, 'ai_error:timeout'))
        self.assertGreaterEqual(result.rule_score, 0.85)
        self.assertTrue(result.passed)
        self.assertTrue(result.reason.startswith('qc_skipped:'))

    @staticmethod
    def _advisory_cues():
        """12 条正常文本 + 5 条占位行：boundary_level=boundary、rule_score>=0.85、
        但 low_content_ratio>0.25 因而不会走 rule_pass 分支。"""
        cues = []
        index = 0
        for position in range(17):
            start = position * 5.5
            if position in (3, 6, 9, 12, 15):
                cues.append((start, start + 5.0, '.'))
            else:
                cues.append((start, start + 5.0, SENT.format(index)))
                index += 1
        return cues


class AdvisoryOverrideTighteningTests(_SrtFixtureMixin, unittest.TestCase):
    def _advisory_result(self, ai_passed, ai_score):
        cues = AiUnavailableMustNotPassTests._advisory_cues()
        return self.qc(cues, 100.0, ai=(ai_passed, ai_score, {'reason': 'some_non_hard_reason'}, 'ok'))

    def test_advisory_sample_reaches_advisory_mode(self):
        cues = AiUnavailableMustNotPassTests._advisory_cues()
        result = self.qc(cues, 100.0, ai=(True, 0.9, {'reason': 'ok'}, 'ok'))
        self.assertEqual(result.raw_ai['ai_mode'], 'advisory_only')
        self.assertEqual(result.raw_ai['boundary_level'], 'boundary')

    def test_strong_ai_negation_cannot_be_overridden(self):
        # ai_score 0.2 < ADVISORY_OVERRIDE_MIN_AI_SCORE(0.4) -> 禁止 advisory 覆盖
        result = self._advisory_result(False, 0.2)
        self.assertFalse(result.passed)
        self.assertFalse(result.raw_ai['ai_override'])
        self.assertEqual(result.reason, 'ai_fail:some_non_hard_reason')

    def test_soft_ai_negation_is_overridden(self):
        # ai_score 0.7 >= 0.4 且非硬失败原因 -> 允许 advisory 覆盖
        result = self._advisory_result(False, 0.7)
        self.assertTrue(result.passed)
        self.assertTrue(result.raw_ai['ai_override'])
        self.assertEqual(result.raw_ai['ai_override_reason'], 'some_non_hard_reason')

    def test_hard_fail_reason_still_rejected_in_advisory_mode(self):
        cues = AiUnavailableMustNotPassTests._advisory_cues()
        result = self.qc(cues, 100.0,
                         ai=(False, 0.9, {'reason': 'hallucination_meta'}, 'ok'))
        self.assertFalse(result.passed)
        self.assertFalse(result.raw_ai['ai_override'])

    def test_advisory_min_ai_score_constant(self):
        self.assertEqual(qc.ADVISORY_OVERRIDE_MIN_AI_SCORE, 0.4)


if __name__ == '__main__':
    unittest.main()
