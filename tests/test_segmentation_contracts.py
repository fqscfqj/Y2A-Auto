#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""分段与翻译契约回归测试。

覆盖四类「逻辑上宣称已防护、实际没生效」的缺陷：

- C1 段级时间容忍：段内切点必须被放行（否则「拆长段」这一主用途整批降级），
  同时越界 / 逆序 / 跳进段间静音空隙必须继续整批拒绝；
- C2 文本覆盖率闸门：集合归属判定会把「源 abab → 输出 ab」判成 100% 覆盖，
  必须改用多重集；等价书写（全角数字、中文数字）不得被误判为丢字/造字；
- C3 出口归一化：``_normalize_output_cues`` 不得丢掉 ``source_window_index``，
  否则下游跨窗去重全面失效；
- C4 未译残留策略：少量残留不得丢掉整份译文，但「整批未译」必须仍然失败。
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.ai_segmentation import (  # noqa: E402
    AISegmentationError,
    _assert_text_coverage,
    _coverage_metrics,
    _interpolate_segment_cues,
    _normalize_output_cues,
    _segment_boundaries,
)
from modules.subtitle_pipeline_types import AlignedSubtitleCue, AsrSegmentTiming  # noqa: E402
from modules.subtitle_translator import (  # noqa: E402
    _is_preservable_verbatim,
    _should_fail_translation_residue,
)


def _quiet_logger():
    logger = logging.getLogger('test-segmentation-contracts')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


def _cue(start_s, end_s, text, window_index=-1):
    cue = AlignedSubtitleCue(start_s=start_s, end_s=end_s, text=text)
    cue.source_window_index = window_index
    return cue


class SegmentInterpolationTests(unittest.TestCase):
    """C1：段级时间容差的放行与拒绝边界。"""

    def setUp(self):
        self.logger = _quiet_logger()
        # 单个 30s 段：段级降级模式的典型输入
        self.single = [(0.0, 30.0)]

    def _run(self, cues, spans=None):
        return _interpolate_segment_cues(
            [dict(cue) for cue in cues], spans if spans is not None else self.single, self.logger
        )

    def test_split_long_segment_is_accepted(self):
        """把 30s 的段拆成两条是段级 prompt 的主用途，必须放行。

        切点 14.8s 距最近输入边界 14.8s/15.2s，永远超出 0.3s 吸附容差 ——
        若只认输入边界，这条路径必然整批降级，插值支持形同虚设。
        """
        out = self._run([
            {'start_s': 0.0, 'end_s': 14.8, 'text': '前半'},
            {'start_s': 14.8, 'end_s': 30.0, 'text': '后半'},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual([(c['start_s'], c['end_s']) for c in out],
                         [(0.0, 14.8), (14.8, 30.0)])
        self.assertEqual(out[0].get('timing_source'), 'segment_interp')

    def test_multi_way_split_is_accepted(self):
        out = self._run([
            {'start_s': 0.0, 'end_s': 10.0, 'text': '一'},
            {'start_s': 10.0, 'end_s': 20.0, 'text': '二'},
            {'start_s': 20.0, 'end_s': 30.0, 'text': '三'},
        ])
        self.assertEqual(len(out), 3)

    def test_unsplit_segment_keeps_boundaries(self):
        out = self._run([{'start_s': 0.0, 'end_s': 30.0, 'text': '整段'}])
        self.assertEqual(len(out), 1)
        self.assertNotIn('timing_source', out[0], '未发生插值就不该标记 segment_interp')

    def test_out_of_range_time_is_rejected(self):
        self.assertEqual(self._run([{'start_s': 0.0, 'end_s': 40.0, 'text': '越界'}]), [])

    def test_reversed_time_is_rejected(self):
        self.assertEqual(self._run([{'start_s': 20.0, 'end_s': 5.0, 'text': '逆序'}]), [])

    def test_snap_to_nearby_boundary_still_happens(self):
        """容差内的近似边界仍应吸附到真实边界，而不是被当作插值点放行。"""
        out = self._run([{'start_s': 0.0, 'end_s': 15.05, 'text': 'a'},
                         {'start_s': 15.05, 'end_s': 30.0, 'text': 'b'}])
        self.assertEqual([c['end_s'] for c in out][0], 15.05)
        single = self._run([{'start_s': 0.0, 'end_s': 30.2, 'text': '整段'}])
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]['end_s'], 30.0, '0.2s 偏差应吸附到真实边界 30.0')

    def test_span_gap_jump_is_rejected(self):
        """时间点落在段与段之间的静音空隙里必须拒绝。

        空隙不是可切分的语音内容。若用「相邻边界对」拼区间，空隙会被当成
        合法区间，(5.0, 10.0) 这类跳进无语音区的输出就会被放行。
        """
        spans = [(0.0, 5.0), (10.0, 15.0)]
        out = self._run([
            {'start_s': 0.0, 'end_s': 5.0, 'text': '一'},
            {'start_s': 7.0, 'end_s': 10.0, 'text': '二'},
            {'start_s': 10.0, 'end_s': 15.0, 'text': '三'},
        ], spans=spans)
        self.assertEqual(out, [], '7.0s 落在 5.0~10.0 的静音空隙内，必须整批拒绝')

    def test_split_within_real_segment_is_accepted(self):
        spans = [(0.0, 5.0), (10.0, 15.0)]
        out = self._run([
            {'start_s': 0.0, 'end_s': 5.0, 'text': '一'},
            {'start_s': 10.0, 'end_s': 12.0, 'text': '二'},
            {'start_s': 12.0, 'end_s': 15.0, 'text': '三'},
        ], spans=spans)
        self.assertEqual(len(out), 3, '在真实段内部切分应放行')

    def test_segment_boundaries_are_derived_from_segments(self):
        segments = [
            AsrSegmentTiming(start_s=0.0, end_s=5.0, text='a'),
            AsrSegmentTiming(start_s=10.0, end_s=15.0, text='b'),
        ]
        self.assertEqual(_segment_boundaries(segments), [0.0, 5.0, 10.0, 15.0])


class TextCoverageGateTests(unittest.TestCase):
    """C2：覆盖率闸门必须用多重集，且容忍等价书写。"""

    def setUp(self):
        self.logger = _quiet_logger()

    def _check(self, src, out_text):
        cues = [AlignedSubtitleCue(start_s=0.0, end_s=1.0, text=out_text)]
        _assert_text_coverage(src, cues, context='unit', logger=self.logger)

    def test_multiset_detects_half_content_loss(self):
        """源 `abab` → 输出 `ab`：集合判定会放行，多重集必须拒绝。"""
        coverage, missing = _coverage_metrics('abab', 'ab')
        self.assertAlmostEqual(coverage, 0.5, places=6)
        self.assertIn('a', missing)
        with self.assertRaises(AISegmentationError):
            self._check('abab', 'ab')

    def test_full_content_passes(self):
        coverage, _missing = _coverage_metrics('alpha beta', 'alpha beta')
        self.assertAlmostEqual(coverage, 1.0, places=6)
        self._check('alpha beta', 'alpha beta')

    def test_repunctuation_is_not_content_change(self):
        self._check('hello world', 'Hello, world!')

    def test_fullwidth_digits_are_equivalent(self):
        """模型把全角数字写成半角属于等价书写，不得判为丢字。"""
        self._check('１２３４５', '12345')

    def test_cjk_digits_are_equivalent(self):
        """ASR 输出中文数字、模型归一为阿拉伯数字属于同一信息。"""
        self._check('一年有三百六十五天', '1年有365天')

    def test_digit_run_without_units_is_read_positionally(self):
        """`一二三` 是逐位读的 123，不是数值 3 —— 后者会掩盖真实丢字。"""
        self._check('一二三', '123')
        self._check('二零二四', '2024')

    def test_numerals_with_units_are_read_as_values(self):
        self._check('三千五百', '3500')
        self._check('一万二千三百四十五', '12345')

    def test_lone_unit_character_is_not_collapsed(self):
        """孤立单位字是词素而非数量：`百分之六十` 的 `百`、`1.2 万` 的 `万`
        都不能被折算 —— 否则 `1.2 万` 会塌成 `0`，与任何内容都不匹配，
        反而掩盖真实的内容变更。"""
        self._check('百分之六十', '百分之60')
        self._check('1.2 万', '1.2 万')

    def test_filler_removal_is_tolerated(self):
        """ASR 原文里的填充词被模型清理是 prompt 允许的行为。"""
        self._check('so um well I think uh it works', 'So, well, I think it works.')

    def test_added_content_is_rejected(self):
        """输出出现原文没有的实词 → 幻觉/串批，必须拒绝。"""
        with self.assertRaises(AISegmentationError):
            self._check('hello world', 'hello world goodnight moon')

    def test_empty_source_passes(self):
        self._check('', '')


class NormalizeOutputCuesTests(unittest.TestCase):
    """C3：出口归一化必须保留 source_window_index。"""

    def test_source_window_index_is_preserved(self):
        cues = [_cue(0.0, 1.0, 'a', window_index=7), _cue(1.0, 2.0, 'b', window_index=8)]
        out = _normalize_output_cues(cues)
        self.assertEqual([c.source_window_index for c in out], [7, 8])

    def test_missing_window_index_falls_back_to_minus_one(self):
        plain = [AlignedSubtitleCue(start_s=0.0, end_s=1.0, text='a')]
        del plain[0].source_window_index
        out = _normalize_output_cues(plain)
        self.assertEqual(out[0].source_window_index, -1)

    def test_empty_text_cue_is_dropped(self):
        cues = [_cue(0.0, 1.0, '   ', window_index=3), _cue(1.0, 2.0, 'keep', window_index=4)]
        out = _normalize_output_cues(cues)
        self.assertEqual([c.text for c in out], ['keep'])
        self.assertEqual(out[0].source_window_index, 4)

    def test_overlap_is_removed_and_order_is_fixed(self):
        cues = [_cue(5.0, 6.0, 'later', window_index=1), _cue(0.0, 1.0, 'early', window_index=2)]
        out = _normalize_output_cues(cues)
        self.assertEqual([c.text for c in out], ['early', 'later'])
        self.assertEqual([c.source_window_index for c in out], [2, 1])


class PreservableVerbatimTests(unittest.TestCase):
    """C4 前置：哪些条目「照留原文」不算未译。"""

    def test_urls_and_numbers_are_preservable(self):
        for text in ('https://example.com/x', 'www.example.com', '2024', '60%', '1.2.3'):
            self.assertTrue(_is_preservable_verbatim(text), text)

    def test_symbols_and_cjk_are_preservable(self):
        for text in ('---', '♪♪', '你好世界'):
            self.assertTrue(_is_preservable_verbatim(text), text)

    def test_names_and_codes_are_preservable(self):
        for text in ('NVIDIA', 'RTX 4090', 'iPhone', 'x86_64', 'GPU'):
            self.assertTrue(_is_preservable_verbatim(text), text)

    def test_plain_english_words_are_not_preservable(self):
        """普通英文词照抄就是未译 —— 放行它们等于取消「整批未译」拦截。"""
        for text in ('alpha', 'bravo', 'hello', 'world', 'the'):
            self.assertFalse(_is_preservable_verbatim(text), text)

    def test_english_sentences_are_not_preservable(self):
        for text in ('hello world', 'this is a test sentence', 'good morning everyone'):
            self.assertFalse(_is_preservable_verbatim(text), text)

    def test_mixed_sentence_with_word_token_is_not_preservable(self):
        self.assertFalse(_is_preservable_verbatim('RTX 4090 is fast'))


class ResidualPolicyTests(unittest.TestCase):
    """C4：残留策略的两个方向都必须成立。"""

    def test_small_residue_does_not_fail_whole_translation(self):
        # allow_partial=True 的旧容忍阈值：同时超过条数与比例才失败
        self.assertFalse(_should_fail_translation_residue(100, 1, allow_partial=True))
        self.assertTrue(_should_fail_translation_residue(100, 20, allow_partial=True))

    def test_strict_policy_still_fails_on_any_residue_at_helper_level(self):
        # 助手层保留「严格即失败」语义；少量残留的追认在 _finalize_* 内完成
        self.assertTrue(_should_fail_translation_residue(100, 1, allow_partial=False))

    def test_zero_residue_never_fails(self):
        for allow_partial in (True, False):
            self.assertFalse(
                _should_fail_translation_residue(100, 0, allow_partial=allow_partial)
            )

    def test_batch_level_untranslated_ratio_is_caught(self):
        """「整批未译」必须失败：10 条残留 5 条 = 50%，两个策略都要拦。"""
        self.assertTrue(_should_fail_translation_residue(10, 5, allow_partial=True))
        self.assertTrue(_should_fail_translation_residue(10, 5, allow_partial=False))


if __name__ == '__main__':
    unittest.main()
