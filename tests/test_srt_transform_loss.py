"""finalize_cues「不丢字」性质回归测试。

finalize_cues 是 speech_recognition 落盘字幕链路的最后一步，下游没有任何文本覆盖率
校验，所以这里锁定一条硬性质：**输出 cue 的文本拼接必须包含全部输入实词**。

修复前 drop_dur = min(0.3, min_cue_duration_s) 会把时长不足的 cue 整条 continue 掉，
文本既不并入前一条也不并入后一条（实测 4 段输入丢掉 'gamma delta'），用户只看到字幕
缺词，链路上不会报任何错。触发链很隐蔽：前面按间隔把过短 cue 压成 0.0x 秒的碎片，
再被 drop_dur 阀值整条吃掉。

配套的反向守卫：任何「安置不了而丢弃」都必须留下带原文的 warning，不允许静默丢弃；
该保住的可见时长（≥0.3s）也仍然要保住。
"""

import logging
import random
import unittest

from modules.srt_transform_engine import SrtTransformConfig, SrtTransformEngine

_LOGGER_NAME = 'modules.srt_transform_engine'

# 2 字中文实词表，用于生成彼此唯一的词：断言「一个词都没丢」时不能靠子串巧合。
_GLYPHS = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉'


def _engine(**overrides):
    params = {'max_line_length': 18, 'max_lines': 3, 'merge_gap_s': 0.3}
    params.update(overrides)
    return SrtTransformEngine(SrtTransformConfig(**params))


def _unique_word(index):
    return _GLYPHS[(index // 20) % 20] + _GLYPHS[index % 20]


def _joined_text(cues):
    return ' '.join(str(cue['text']) for cue in cues)


class _LogAssertionTestCase(unittest.TestCase):
    """日志断言的隔离基类。

    同目录的 test_speech_pipeline_wiring.py 会调用 logging.disable(logging.CRITICAL)，
    且并非每处都恢复。日志被全局禁用时 assertLogs 会报「没有日志」，而 assertNoLogs
    会**假通过** —— 「不允许静默丢弃」这条守卫会直接失效。这里显式复位一次。
    """

    def setUp(self):
        manager = logging.getLogger().manager
        self._previous_disable = manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, self._previous_disable)


class ShortCueTextLossTests(_LogAssertionTestCase):
    """极短 cue 的文本必须被安置，而不是随 cue 一起消失。"""

    def test_极短cue的文本并入邻居而不是被丢弃(self):
        engine = _engine()
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'alpha beta'},
            {'start': 2.05, 'end': 2.10, 'text': 'gamma delta'},
            {'start': 2.10, 'end': 2.15, 'text': 'epsilon zeta'},
            {'start': 2.15, 'end': 4.0, 'text': 'eta theta'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=4.0)

        text = _joined_text(finalized)
        for word in ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta'):
            self.assertIn(word, text, msg=f'输出丢失实词 {word!r}: {text!r}')

    def test_首条极短cue并入后一条并覆盖其时间段(self):
        # cleaned 为空时没有前一条可并，只能并入后一条；起点提前到被吸收 cue 的起点，
        # 跨度变大才能同时满足字速上限与不丢字。
        engine = _engine()
        cues = [
            {'start': 0.0, 'end': 0.05, 'text': '甲乙'},
            {'start': 0.20, 'end': 2.0, 'text': '丙丁'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=6.0)

        self.assertEqual(len(finalized), 1, msg=finalized)
        self.assertEqual(finalized[0]['text'], '甲乙丙丁')  # CJK 相邻不插空格
        self.assertLessEqual(finalized[0]['start'], 0.05)

    def test_吸收不会造出与前一条重叠的cue(self):
        engine = _engine()
        cues = [
            {'start': 0.0, 'end': 0.75, 'text': '甲乙'},
            {'start': 0.80, 'end': 0.85, 'text': '丙丁'},
            {'start': 0.90, 'end': 2.0, 'text': '戊己'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=6.0)

        for prev, cur in zip(finalized, finalized[1:]):
            self.assertLessEqual(
                float(prev['end']), float(cur['start']),
                msg=f'输出出现重叠: {finalized}',
            )
        for cue in cues:
            self.assertIn(cue['text'], _joined_text(finalized))

    def test_输出不再含不可见时长的cue(self):
        engine = _engine()
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': '第一句内容'},
            {'start': 2.0, 'end': 2.02, 'text': '补充'},
            {'start': 2.03, 'end': 4.0, 'text': '第三句内容'},
            {'start': 9.95, 'end': 10.0, 'text': '末尾'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=10.0)

        self.assertTrue(finalized)
        for cue in finalized:
            self.assertGreaterEqual(cue['end'] - cue['start'], 0.3)
        text = _joined_text(finalized)
        for word in ('第一句内容', '补充', '第三句内容', '末尾'):
            self.assertIn(word, text, msg=f'输出丢失文本 {word!r}: {text!r}')


class UnplaceableCueGuardTests(_LogAssertionTestCase):
    """确实无处安置时仍然丢弃，但必须留下带原文的 warning。"""

    def test_贴末尾且无人可并的极短cue丢弃并告警(self):
        engine = _engine()
        cues = [{'start': 9.95, 'end': 10.0, 'text': '甲乙'}]

        with self.assertLogs(_LOGGER_NAME, level='WARNING') as captured:
            finalized = engine.finalize_cues(cues, total_duration_s=10.0)

        self.assertEqual(finalized, [])
        records = ' '.join(captured.output)
        self.assertIn('甲乙', records, msg=f'丢弃日志未带上被丢文本: {records}')

    def test_可延长的极短cue不会走丢弃分支(self):
        engine = _engine()
        cues = [{'start': 0.0, 'end': 0.05, 'text': '甲乙'}]

        with self.assertNoLogs(_LOGGER_NAME, level='WARNING'):
            finalized = engine.finalize_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]['text'], '甲乙')
        self.assertGreaterEqual(finalized[0]['end'] - finalized[0]['start'], 0.3)


class NoTextLossPropertyTests(_LogAssertionTestCase):
    """性质测试：任意碎片化输入，输出都不得丢词。"""

    DURATION_CHOICES = (0.12, 0.15, 0.20, 0.30, 0.60, 1.00)
    GAP_CHOICES = (0.0, 0.01, 0.02, 0.05, 0.10)

    def _build_cues(self, rng, count):
        """生成含大量极短 cue 的输入。

        每个 2 字词的时长都 ≥0.12s，即文本密度 ≤16.7 cps，低于 max_chars_per_second
        （20）。连成一段后密度只会更低，所以这里断言的是「吸收机制有没有把文字安置好」，
        而不是要求实现突破字速上限去容纳不可能满足的输入。
        """
        cues = []
        cursor = 0.0
        for index in range(count):
            duration = rng.choice(self.DURATION_CHOICES)
            cues.append({
                'start': round(cursor, 3),
                'end': round(cursor + duration, 3),
                'text': _unique_word(index),
            })
            cursor += duration + rng.choice(self.GAP_CHOICES)
        return cues

    def _assert_no_loss(self, cues, total_duration_s):
        with self.assertNoLogs(_LOGGER_NAME, level='WARNING'):
            finalized = _engine().finalize_cues([dict(cue) for cue in cues], total_duration_s)
        text = _joined_text(finalized)
        for cue in cues:
            self.assertIn(cue['text'], text, msg=f'丢失实词 {cue["text"]!r}: {text!r}')
        return finalized

    def test_随机碎片序列不丢词(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                cues = self._build_cues(rng, rng.randint(3, 14))
                total_duration_s = round(
                    max(cue['end'] for cue in cues) + rng.choice((0.6, 0.7, 1.0)), 3
                )
                self._assert_no_loss(cues, total_duration_s)

    def test_表驱动碎片组合不丢词(self):
        cases = (
            # 浮点敏感的相邻边界：2.10 - 2.05 在二进制里大于 0.05，会先被压成碎片
            ((0.0, 2.0, '甲乙'), (2.05, 2.10, '丙丁'), (2.10, 2.15, '戊己'), (2.15, 4.0, '庚辛')),
            # 连续三片都短于 drop 阈值
            ((0.0, 0.12, '甲乙'), (0.12, 0.24, '丙丁'), (0.24, 0.36, '戊己')),
            # 0.01s 的碎片夹在两条正常 cue 之间
            ((0.0, 1.0, '甲乙'), (1.01, 1.02, '丙丁'), (1.03, 3.0, '戊己')),
            # 稀疏分布：首条与末条都短
            ((0.30, 0.35, '甲乙'), (0.90, 1.20, '丙丁')),
            # 末条贴着视频末尾的余量，但前一条装得下
            ((0.0, 2.0, '甲乙'), (5.0, 5.05, '丙丁'), (5.06, 5.20, '戊己')),
        )
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                cues = [
                    {'start': start, 'end': end, 'text': text}
                    for start, end, text in raw
                ]
                total_duration_s = max(cue['end'] for cue in cues) + 1.0
                self._assert_no_loss(cues, total_duration_s)


class AbsorbCapacityTests(_LogAssertionTestCase):
    """容器装不下时也不能丢字，而是把这句留在自己的时间轴上。"""

    def test_前一条文本已满时极短cue仍被保留(self):
        # max_merge_chars = 6 * 1 = 6，前一条正好占满，吸收必然失败
        engine = _engine(max_line_length=6, max_lines=1)
        cues = [
            {'start': 0.0, 'end': 5.0, 'text': '甲甲乙乙丙丙'},
            {'start': 5.02, 'end': 5.07, 'text': '丁丁'},
        ]

        with self.assertNoLogs(_LOGGER_NAME, level='WARNING'):
            finalized = engine.finalize_cues(cues, total_duration_s=8.0)

        self.assertEqual(_joined_text(finalized), '甲甲乙乙丙丙 丁丁')


if __name__ == '__main__':
    unittest.main()
