# -*- coding: utf-8 -*-
"""时间轴维度边界回归：退化 cue（零时长 / 逆序）与运行时阈值。

既有 ``test_subtitle_qc_timeline.py`` 的 ``uniform`` 夹具只能生成升序、不重叠、
正时长的 cue —— 倒序 / 零时长 / 运行时放松阈值这三类输入在测试里根本无法表达，
这正是「零时长 cue 让整个时间轴维度静默关闭」「``SUBTITLE_QC_MAX_CPS`` 的放宽
方向恒不生效」「片尾正常留白让良性字幕在 AI 不可用时永不烧录」三个缺陷能长期
存活的原因。本文件用可表达三类输入的夹具把三个方向都锁死，每条修复都配反向守卫。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules import subtitle_qc as qc

SENT = "this is a fully descriptive sentence number {} used for quality checking"
# "alpha bravo charlie" 重复两次 -> 命中 template_like_phrase（文本维度软信号）
TEMPLATE_LINE = "alpha bravo charlie alpha bravo charlie"
# 78 字符塞进 0.3s -> CPS 远超任何合理上限，用于验证「放松不等于关闭」
HIGH_CPS_TEXT = ('alpha bravo charlie delta echo foxtrot golf hotel india juliet '
                 'kilo lima mike')

NORMAL = 'normal'
ZERO = 'zero'
REVERSED = 'reversed'

TIMELINE_FAIL_PREFIX = 'rule_fail:timeline'
AI_UNAVAILABLE = 'missing_openai_api_key'


def _ts(seconds):
    """秒 -> SRT 时间戳 HH:MM:SS,mmm。"""
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3600000)
    minutes, total_ms = divmod(total_ms, 60000)
    secs, ms = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def cues_from_specs(specs):
    """扩展夹具：把 (kind, start, length, text) 规格展开成 SRT cue 三元组。

    ``kind`` 取 ``normal`` / ``zero`` / ``reversed``：
    - normal   -> (start, start + length)
    - zero     -> (start, start)               零时长 cue（VAD 崩坏的典型产物）
    - reversed -> (start + length, start)      逆序 cue（时间戳倒挂）
    """
    cues = []
    for kind, start, length, text in specs:
        if kind == NORMAL:
            cues.append((start, start + length, text))
        elif kind == ZERO:
            cues.append((start, start, text))
        elif kind == REVERSED:
            cues.append((start + length, start, text))
        else:
            raise AssertionError(f'未知 cue 类型: {kind}')
    return cues


def healthy_cues(count, cue_len, stride, start=0.0):
    """升序、不重叠、正时长的健康基线（等价于既有的 uniform 夹具）。"""
    return [(start + i * stride, start + i * stride + cue_len, SENT.format(i))
            for i in range(count)]


class _EdgeFixtureMixin:
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-qc-timeline-edge-')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_srt(self, cues, name='edge.srt'):
        path = os.path.join(self.tmpdir, name)
        blocks = []
        for index, (start, end, text) in enumerate(cues, 1):
            blocks.append(f"{index}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write("\n".join(blocks) + "\n")
        return path

    def qc(self, cues, total_duration_s, config=None, strict=False, ai_status=AI_UNAVAILABLE):
        path = self.write_srt(cues)
        with patch.object(qc, '_call_ai_judge', return_value=(None, None, None, ai_status)):
            return qc.run_subtitle_qc(path, dict(config or {}),
                                      total_duration_s=total_duration_s, strict=strict)


class FixtureCapabilityTests(_EdgeFixtureMixin, unittest.TestCase):
    """夹具自证：三类此前无法表达的输入确实能被构造出来并落盘解析。"""

    def test_fixture_expresses_zero_and_reversed_and_normal(self):
        cues = cues_from_specs([
            (NORMAL, 0.0, 4.0, 'a'),
            (ZERO, 10.0, 0.0, 'b'),
            (REVERSED, 20.0, 5.0, 'c'),
        ])
        self.assertEqual(cues[0], (0.0, 4.0, 'a'))
        self.assertEqual(cues[1][0], cues[1][1], '零时长 cue 未构造成功')
        self.assertGreater(cues[2][0], cues[2][1], '逆序 cue 未构造成功')

    def test_fixture_round_trips_through_srt_parser(self):
        cues = cues_from_specs([
            (NORMAL, 0.0, 4.0, 'alpha bravo'),
            (ZERO, 4.0, 0.0, 'charlie delta'),
            (REVERSED, 12.0, 4.0, 'echo foxtrot'),
        ])
        items = qc._read_srt_items(self.write_srt(cues))
        self.assertEqual(len(items), 3)
        spans = []
        for item in items:
            start = qc._parse_srt_timestamp_seconds(item.start_time)
            end = qc._parse_srt_timestamp_seconds(item.end_time)
            spans.append(end - start)
        self.assertEqual(spans, [4.0, 0.0, -4.0])


class ZeroDurationCueTests(_EdgeFixtureMixin, unittest.TestCase):
    """零时长 cue：必须计入时间轴维度，占比超线即硬失败，偶发 1–2 条不误伤。"""

    @staticmethod
    def _all_zero_duration(count=15, stride=8.0):
        return cues_from_specs([(ZERO, i * stride, 0.0, SENT.format(i))
                                for i in range(count)])

    def test_all_zero_duration_cues_no_longer_disable_timeline(self):
        # 修复前：15 条 start==end -> timeline_checked=False、coverage_ratio=None、
        # 无任何硬失败原因，甚至直接 rule_pass 放行。
        result = self.qc(self._all_zero_duration(), 120.0)
        metrics = result.raw_ai
        self.assertTrue(metrics['timeline_checked'], '零时长 cue 仍让时间轴维度静默关闭')
        self.assertIsNotNone(metrics['coverage_ratio'])
        self.assertEqual(metrics['timeline_cue_count'], 15)
        self.assertEqual(metrics['zero_duration_count'], 15)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_zero_duration')

    def test_zero_duration_ratio_is_reported_separately(self):
        result = self.qc(self._all_zero_duration(), 120.0)
        metrics = result.raw_ai
        # 占比与条数都要暴露给排查方，且不依赖 coverage 口径
        self.assertAlmostEqual(metrics['zero_duration_ratio'], 1.0, places=6)
        self.assertEqual(metrics['reversed_cue_count'], 0)
        # 正时长条目为 0：几何量退回用点位置，仍不关闭维度
        self.assertEqual(metrics['timeline_positive_cue_count'], 0)

    def test_occasional_zero_duration_cues_do_not_false_kill(self):
        # 反向守卫：40 条正常字幕 + 2 条偶发零时长（音频块边界重合的实际情形）
        cues = healthy_cues(40, 2.8, 3.0)
        cues += cues_from_specs([(ZERO, 117.5, 0.0, SENT.format(900)),
                                 (ZERO, 120.0, 0.0, SENT.format(901))])
        result = self.qc(cues, 125.0)
        metrics = result.raw_ai
        self.assertTrue(metrics['timeline_checked'])
        self.assertEqual(metrics['zero_duration_count'], 2)
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX),
                         msg=f'偶发零时长 cue 被误杀: {result.reason}')
        self.assertTrue(result.passed)

    def test_two_zero_duration_cues_are_tolerated_even_on_ratio(self):
        # 2/10 = 20% 已接近占比线，但仍低于「最少 3 条」的绝对条数下限 -> 不硬失败
        cues = healthy_cues(8, 4.0, 6.0)
        cues += cues_from_specs([(ZERO, 50.0, 0.0, SENT.format(800)),
                                 (ZERO, 54.0, 0.0, SENT.format(801))])
        result = self.qc(cues, 60.0)
        metrics = result.raw_ai
        self.assertAlmostEqual(metrics['zero_duration_ratio'], 0.2, places=6)
        self.assertNotEqual(result.reason, 'rule_fail:timeline_zero_duration')
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX))
        self.assertTrue(result.passed)

    def test_zero_duration_at_ratio_and_count_line_still_fails(self):
        # 反向守卫：3/12 = 25% 恰好同时触到占比线与条数线 -> 必须硬失败
        cues = healthy_cues(6, 10.0, 10.0)
        cues += cues_from_specs([(ZERO, 70.0 + i * 10.0, 0.0, SENT.format(100 + i))
                                 for i in range(6)])
        result = self.qc(cues, 130.0)
        metrics = result.raw_ai
        self.assertEqual(metrics['zero_duration_count'], 6)
        self.assertAlmostEqual(metrics['zero_duration_ratio'], 0.5, places=6)
        self.assertEqual(result.reason, 'rule_fail:timeline_zero_duration')


class ReversedCueTests(_EdgeFixtureMixin, unittest.TestCase):
    """逆序 cue：计入时间轴维度、不要报成 overlap，但要给出独立诊断。"""

    @staticmethod
    def _all_reversed(count=15, stride=8.0, length=6.0):
        return cues_from_specs([(REVERSED, i * stride, length, SENT.format(i))
                                for i in range(count)])

    def test_reversed_cues_hard_fail_with_dedicated_token(self):
        # 修复前：timeline_checked=False，逆序被整体排除，无任何判定。
        result = self.qc(self._all_reversed(), 120.0)
        metrics = result.raw_ai
        self.assertTrue(metrics['timeline_checked'])
        self.assertEqual(metrics['reversed_cue_count'], 15)
        self.assertAlmostEqual(metrics['reversed_cue_ratio'], 1.0, places=6)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_reversed_cue')

    def test_reversed_cues_are_not_reported_as_overlap(self):
        # 逆序 cue 折成零长度点，不得被算作重叠（否则排查方向被误导到 overlap）
        result = self.qc(self._all_reversed(), 120.0)
        metrics = result.raw_ai
        self.assertEqual(metrics['overlap_ratio'], 0.0)
        self.assertNotEqual(result.reason, 'rule_fail:timeline_overlap')

    def test_single_reversed_cue_goes_to_ai_instead_of_hard_fail(self):
        # 反向守卫：20 条里 1 条倒挂（占比 5%，未超线且未达 2 条）不算系统性崩坏，
        # 只降级为可疑送 AI 复核。
        cues = healthy_cues(20, 5.5, 6.0)
        cues[10] = cues_from_specs([(REVERSED, cues[10][0], 5.0, cues[10][2])])[0]
        result = self.qc(cues, 125.0)
        metrics = result.raw_ai
        self.assertEqual(metrics['reversed_cue_count'], 1)
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX),
                         msg=f'单条逆序被当成系统性崩坏: {result.reason}')
        self.assertEqual(metrics['boundary_level'], 'suspicious')
        self.assertEqual(result.decision, 'needs_ai')

    def test_small_sample_inversion_is_hard_failed(self):
        # 小样本兜底：4 条里 1 条倒挂 = 25%，虽只 1 条也必须判病态
        cues = healthy_cues(3, 4.0, 6.0)
        cues += cues_from_specs([(REVERSED, 18.0, 4.0, SENT.format(50))])
        result = self.qc(cues, 40.0)
        self.assertAlmostEqual(result.raw_ai['reversed_cue_ratio'], 0.25, places=6)
        self.assertEqual(result.reason, 'rule_fail:timeline_reversed_cue')


class CpsRuntimeLimitTests(_EdgeFixtureMixin, unittest.TestCase):
    """SUBTITLE_QC_MAX_CPS 必须按运行时配置生效，放宽与收紧两个方向都要线性可感。"""

    # CPS=27 落在 25–30 之间：默认口径下是异常，放宽到 30 后应恢复正常
    FAST_CPS = 27.0

    def _fast_cues(self):
        normalized_len = len(qc._normalize_line(SENT.format(0)))
        fast_len = normalized_len / self.FAST_CPS
        cues = healthy_cues(6, 4.0, 6.0)
        cues += cues_from_specs([
            (NORMAL, 36.0, fast_len, SENT.format(700)),
            (NORMAL, 39.0, fast_len, SENT.format(701)),
        ])
        return cues, fast_len

    def test_default_limit_flags_the_fast_cues(self):
        cues, _ = self._fast_cues()
        result = self.qc(cues, 48.0)
        self.assertAlmostEqual(result.raw_ai['cps_outlier_ratio'], 0.25, places=6)
        self.assertEqual(result.reason, 'rule_fail:timeline_cps_outlier')

    def test_relaxed_max_cps_is_honoured(self):
        # 修复前：cfg=30.0 与 cfg=None 结果完全相同（指标与 reason 都一模一样）。
        cues, _ = self._fast_cues()
        result = self.qc(cues, 48.0, config={'SUBTITLE_QC_MAX_CPS': 30.0})
        metrics = result.raw_ai
        self.assertAlmostEqual(metrics['cps_outlier_ratio'], 0.0, places=6)
        self.assertNotEqual(result.reason, 'rule_fail:timeline_cps_outlier')
        self.assertFalse(result.reason.startswith(TIMELINE_FAIL_PREFIX))
        self.assertTrue(result.passed)

    def test_metric_itself_follows_the_runtime_config(self):
        # 防同义反复：同一份 SRT 在两种配置下必须给出**不同的指标值**，
        # 而不只是走不同的分支。旧实现三种配置的 cps_outlier_ratio 一模一样。
        cues, _ = self._fast_cues()
        default_ratio = self.qc(cues, 48.0).raw_ai['cps_outlier_ratio']
        relaxed_ratio = self.qc(cues, 48.0,
                                config={'SUBTITLE_QC_MAX_CPS': 30.0}).raw_ai['cps_outlier_ratio']
        self.assertNotAlmostEqual(default_ratio, relaxed_ratio, places=6)

    def test_tightened_max_cps_is_honoured(self):
        cues, _ = self._fast_cues()
        result = self.qc(cues, 48.0, config={'SUBTITLE_QC_MAX_CPS': 8.0})
        self.assertAlmostEqual(result.raw_ai['cps_outlier_ratio'], 1.0, places=6)
        self.assertEqual(result.reason, 'rule_fail:timeline_cps_outlier')

    def test_relaxation_is_not_a_blanket_switch_off(self):
        # 反向守卫：把上限放宽到 200，仍要抓住真正塞不进时长的条目
        cues = healthy_cues(6, 4.0, 6.0)
        cues += cues_from_specs([(NORMAL, 36.0, 0.3, HIGH_CPS_TEXT),
                                 (NORMAL, 39.0, 0.3, HIGH_CPS_TEXT)])
        result = self.qc(cues, 48.0, config={'SUBTITLE_QC_MAX_CPS': 200.0})
        self.assertAlmostEqual(result.raw_ai['cps_outlier_ratio'], 0.25, places=6)
        self.assertEqual(result.reason, 'rule_fail:timeline_cps_outlier')


class TailGraceTests(_EdgeFixtureMixin, unittest.TestCase):
    """片尾留白：绝对秒数 + 比值双条件，且不能放过「末条明显提前结束 / 覆盖率不足」。"""

    @staticmethod
    def _cues_ending_at(last_end, total_cues=20):
        span = last_end / total_cues
        return [(i * span, i * span + span, SENT.format(i)) for i in range(total_cues)]

    def test_benign_tail_padding_passes_without_ai(self):
        # 复审实测样本：20 条覆盖 0–99s / 视频 120s（ratio 0.825）
        result = self.qc(self._cues_ending_at(99.0), 120.0)
        metrics = result.raw_ai
        self.assertLess(metrics['last_cue_end_ratio'], qc.TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO)
        self.assertLessEqual(metrics['total_duration_seconds'] - metrics['last_cue_end_seconds'],
                             qc.TIMELINE_TAIL_GRACE_SECONDS,
                             msg='本用例没有落在片尾容忍带内，没锁定到目标分支')
        self.assertNotEqual(metrics['boundary_level'], 'suspicious')
        self.assertEqual(result.decision, 'rule_pass')
        self.assertTrue(result.passed)

    def test_tail_grace_boundary_is_the_absolute_gap(self):
        # 缺口 30.0s（容忍带边界内）放行；缺口 30.1s 超出容忍带 -> 降级为可疑且 AI
        # 不可用时不放行。两个方向一起锁定 TIMELINE_TAIL_GRACE_SECONDS 的取值。
        inside = self.qc(self._cues_ending_at(90.0), 120.0)
        self.assertTrue(inside.passed)
        self.assertEqual(inside.decision, 'rule_pass')

        outside = self.qc(self._cues_ending_at(89.9), 120.0)
        self.assertFalse(outside.passed)
        self.assertEqual(outside.raw_ai['boundary_level'], 'suspicious')
        self.assertGreaterEqual(outside.rule_score, qc.STRICT_MIN_RULE_SCORE)

    def test_half_covered_tail_still_hard_fails(self):
        # 反向守卫：末条只覆盖到一半（60/120）必须仍然拦截
        result = self.qc(self._cues_ending_at(60.0), 120.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, 'rule_fail:timeline_truncated_tail')

    def test_coverage_deficiency_still_blocks_without_ai(self):
        # 反向守卫：覆盖率 0.28（低于 0.35 可疑线）在 AI 不可用时依旧不放行 ——
        # 放宽的是「末条留白」，不是「覆盖率严重不足」。
        cues = [(i * 25.0, i * 25.0 + 7.0, SENT.format(i)) for i in range(24)]
        result = self.qc(cues, 600.0)
        metrics = result.raw_ai
        self.assertLess(metrics['coverage_ratio'], qc.TIMELINE_SUSPICIOUS_COVERAGE_RATIO)
        self.assertEqual(metrics['boundary_level'], 'suspicious')
        self.assertGreaterEqual(result.rule_score, qc.STRICT_MIN_RULE_SCORE)
        self.assertFalse(result.passed)

    def test_soft_text_suspicion_with_high_rule_score_passes_without_ai(self):
        # suspicious 但规则分高、时间轴无实质缺陷 -> AI 不可用时仍可放行
        cues = healthy_cues(20, 5.95, 5.95)
        cues[7] = (cues[7][0], cues[7][1], TEMPLATE_LINE)
        result = self.qc(cues, 120.0)
        self.assertEqual(result.raw_ai['boundary_level'], 'suspicious')
        self.assertGreaterEqual(result.rule_score, qc.STRICT_MIN_RULE_SCORE)
        self.assertFalse(qc._timeline_materially_deficient(result.raw_ai))
        self.assertTrue(result.passed)
        self.assertTrue(result.reason.startswith('qc_skipped:'))

    def test_strict_mode_still_blocks_soft_suspicion(self):
        # 反向守卫：strict 路径不受上述放宽影响
        cues = healthy_cues(20, 5.95, 5.95)
        cues[7] = (cues[7][0], cues[7][1], TEMPLATE_LINE)
        result = self.qc(cues, 120.0, strict=True)
        self.assertFalse(result.passed)
        self.assertTrue(result.raw_ai['ai_unavailable_strict'])


class ThresholdContractTests(_EdgeFixtureMixin, unittest.TestCase):
    """取值契约由 test_subtitle_qc_timeline.py 锁定，这里只锁行为语义。"""

    def test_tail_grace_helper_requires_duration(self):
        # 拿不到总时长时容忍带无意义，必须返回 False（按可疑处理）
        self.assertFalse(qc._tail_within_grace({'last_cue_end_seconds': 100.0}))
        self.assertFalse(qc._tail_within_grace({
            'total_duration_seconds': 120.0, 'last_cue_end_seconds': None}))
        self.assertTrue(qc._tail_within_grace({
            'total_duration_seconds': 120.0, 'last_cue_end_seconds': 90.0}))
        self.assertFalse(qc._tail_within_grace({
            'total_duration_seconds': 120.0, 'last_cue_end_seconds': 89.0}))

    def test_material_deficiency_only_covers_coverage_and_tail(self):
        # 只有「覆盖率严重不足」「末条明显提前结束」算实质缺陷；轻微重叠不算
        self.assertTrue(qc._timeline_materially_deficient({
            'timeline_checked': True, 'coverage_ratio': 0.20,
            'last_cue_end_ratio': 0.95, 'total_duration_seconds': 600.0,
            'last_cue_end_seconds': 570.0}))
        self.assertTrue(qc._timeline_materially_deficient({
            'timeline_checked': True, 'coverage_ratio': 0.90,
            'last_cue_end_ratio': 0.50, 'total_duration_seconds': 600.0,
            'last_cue_end_seconds': 300.0}))
        self.assertFalse(qc._timeline_materially_deficient({
            'timeline_checked': True, 'coverage_ratio': 0.90,
            'last_cue_end_ratio': 0.825, 'total_duration_seconds': 120.0,
            'last_cue_end_seconds': 99.0}))
        self.assertFalse(qc._timeline_materially_deficient({'timeline_checked': False}))


if __name__ == '__main__':
    unittest.main()
