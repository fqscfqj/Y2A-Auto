# -*- coding: utf-8 -*-
"""B-c 回归测试：未译检测不得豁免整类 CJK 文本。

缺陷（本 PR 引入的漏检回归）：``_is_preservable_verbatim`` 原本只要文本
不含拉丁字母/数字就判为「不可译」→ 已翻译，把日文/韩文原文照抄整类豁免。
日文视频译中文时，模型原样返回日文会被判「已译」，残留闸门放过，
日文原文写进中文字幕并烧录。

修复后的判据：CJK 文本只有「文本本身是中文**且**目标语言也是中文」才算
合法照抄；假名/谚文是明确的「非中文」信号。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_translator import (  # noqa: E402
    _is_chinese_text,
    _is_cjk_preservable,
    _is_preservable_verbatim,
)


class PreservableVerbatimTargetLanguageTests(unittest.TestCase):
    """照抄原文是否合法，必须取决于目标语言。"""

    def test_japanese_original_copied_verbatim_is_not_preserved(self):
        """日文原文照抄 → 未译（修复前被判已译，是漏检回归）。"""
        for text in ('こんにちは世界', 'ありがとうございます', 'これはテストです'):
            self.assertFalse(
                _is_preservable_verbatim(text, target_language='zh'), text)

    def test_korean_original_copied_verbatim_is_not_preserved(self):
        for text in ('안녕하세요', '감사합니다'):
            self.assertFalse(
                _is_preservable_verbatim(text, target_language='zh'), text)

    def test_pure_hanzi_with_chinese_target_is_preserved(self):
        """汉字本身就是中文：目标是中文时无需翻译，照抄合法。"""
        for text in ('你好世界', '東京大学', '字幕测试'):
            self.assertTrue(
                _is_preservable_verbatim(text, target_language='zh'), text)

    def test_hanzi_with_non_chinese_target_is_not_preserved(self):
        """目标是英文却照抄中文原文 → 未译。"""
        self.assertFalse(_is_preservable_verbatim('你好世界', target_language='en'))

    def test_unknown_target_language_is_conservative(self):
        """目标语言未知时按不放行处理 —— 修复方向是收紧漏检，不是放宽。"""
        self.assertFalse(_is_preservable_verbatim('你好世界', target_language=None))
        self.assertFalse(_is_preservable_verbatim('こんにちは', target_language=None))

    def test_preservable_entries_are_still_preserved(self):
        """反向守卫：URL / 数字 / 缩写 / 专有名词的既有兼容性不得被收紧。"""
        for text in ('https://example.com/x', 'www.example.com', '2024', '60%',
                     '1.2.3', 'NVIDIA', 'RTX 4090', 'iPhone', 'x86_64', '---'):
            self.assertTrue(
                _is_preservable_verbatim(text, target_language='zh'), text)

    def test_plain_english_still_not_preserved(self):
        for text in ('alpha', 'bravo', 'hello world', 'good morning everyone'):
            self.assertFalse(
                _is_preservable_verbatim(text, target_language='zh'), text)

    def test_mixed_japanese_sentence_not_preserved(self):
        """含假名的多 token 短语不得因汉字 token 被整句放行。"""
        self.assertFalse(
            _is_preservable_verbatim('東京 タワー', target_language='zh'))
        self.assertFalse(
            _is_preservable_verbatim('東京大学 は 日本 の 大学', target_language='zh'))


class ChineseTargetDetectionTests(unittest.TestCase):
    """目标语言与文本语种的判定辅助。"""

    def test_chinese_target_variants(self):
        for lang in ('zh', 'zh-CN', 'zh-Hans', 'ZH', 'zh-TW', 'chinese'):
            self.assertTrue(_is_cjk_preservable('你好', lang), lang)
        for lang in ('en', 'ja', 'ko', 'japanese', '', None):
            self.assertFalse(_is_cjk_preservable('你好', lang), repr(lang))

    def test_chinese_text_detection(self):
        self.assertTrue(_is_chinese_text('你好世界'))
        self.assertTrue(_is_chinese_text('東京大学'))
        self.assertFalse(_is_chinese_text('こんにちは'))
        self.assertFalse(_is_chinese_text('안녕하세요'))
        self.assertFalse(_is_chinese_text('hello'))
        self.assertFalse(_is_chinese_text(''))
        self.assertFalse(_is_chinese_text('123'))

    def test_mixed_hanzi_kana_is_not_chinese(self):
        self.assertFalse(_is_chinese_text('東京タワー'))
        self.assertFalse(_is_chinese_text('漢字とかな'))


class LikelyUntranslatedIntegrationTests(unittest.TestCase):
    """端到端：用真实翻译器实例验证残留判定。"""

    def _translator(self, target_language='zh'):
        from unittest.mock import MagicMock

        from modules.subtitle_translator import SubtitleTranslator, TranslationConfig

        translator = SubtitleTranslator.__new__(SubtitleTranslator)
        translator.config = TranslationConfig(target_language=target_language)
        translator.logger = MagicMock()
        translator.task_id = 'unit'
        return translator

    def test_japanese_verbatim_is_flagged_untranslated(self):
        translator = self._translator('zh')
        self.assertTrue(translator._likely_untranslated('こんにちは', 'こんにちは'))
        self.assertTrue(translator._likely_untranslated('ありがとう', 'ありがとう'))

    def test_korean_verbatim_is_flagged_untranslated(self):
        translator = self._translator('zh')
        self.assertTrue(translator._likely_untranslated('안녕하세요', '안녕하세요'))

    def test_chinese_verbatim_is_not_flagged(self):
        """中文源 + 中文目标：照抄是正常结果，不该被反复追认。"""
        translator = self._translator('zh')
        self.assertFalse(translator._likely_untranslated('你好世界', '你好世界'))

    def test_pure_kana_translation_is_flagged(self):
        """译文全是假名、与原文不同：仍是未译（此前会因分母为 0 被判已译）。"""
        translator = self._translator('zh')
        self.assertTrue(translator._likely_untranslated('thank you', 'ありがとうございます'))

    def test_normal_translation_is_not_flagged(self):
        translator = self._translator('zh')
        self.assertFalse(translator._likely_untranslated('hello world', '你好，世界'))
        self.assertFalse(translator._likely_untranslated('hello', '你好'))

    def test_url_verbatim_is_not_flagged(self):
        translator = self._translator('zh')
        self.assertFalse(translator._likely_untranslated(
            'https://example.com/x', 'https://example.com/x'))

    def test_english_verbatim_is_flagged(self):
        translator = self._translator('zh')
        self.assertTrue(translator._likely_untranslated('hello world', 'hello world'))


class RatioBranchCjkRegressionTests(unittest.TestCase):
    """R1 回归：比值分支也必须认假名/谚文，「加个标点」不能绕过未译检测。

    缺陷：`d == s` 的快速路径修好之后，模型只要给日文原文加一个句号、删一个
    逗号或插一个空格，`d != s` 就成立，而比值分支把「汉字」一律计入中文 ——
    汉字占多数的日文因此算出「中文译文」，日文原文被写进中文字幕并烧录。
    这些用例的判据必须在**没有** `d == s` 帮助的情况下成立。
    """

    def _translator(self, target_language='zh'):
        from unittest.mock import MagicMock

        from modules.subtitle_translator import SubtitleTranslator, TranslationConfig

        translator = SubtitleTranslator.__new__(SubtitleTranslator)
        translator.config = TranslationConfig(target_language=target_language)
        translator.logger = MagicMock()
        translator.task_id = 'unit'
        return translator

    def test_japanese_echo_with_trailing_period_is_untranslated(self):
        translator = self._translator('zh')
        source = '東京駅は新宿駅です'
        for dst in (
            '東京駅は新宿駅です。',   # 加了一个句号
            '東京駅は新宿駅です',      # 去掉逗号（源带逗号）
            '東京駅 は 新宿駅 です',   # 插入空格
        ):
            self.assertTrue(
                translator._likely_untranslated('東京、' + source, dst), dst)

    def test_kanji_heavy_japanese_echo_is_untranslated(self):
        """汉字占多数（假名占比低）的日文照抄同样必须被抓住。"""
        translator = self._translator('zh')
        self.assertTrue(
            translator._likely_untranslated(
                '新宿駅に到着、東京駅を出発', '新宿駅に到着 東京駅を出発'))

    def test_kanji_only_limitation_is_pinned(self):
        """已知限制：纯汉字文本在「目标是中文」时无法与中文区分。

        汉字码位被中日韩共用，单看文本无从判定它到底是「本来就是中文」还是
        「日文原文照抄」，因此纯汉字的照抄在 zh 目标下仍被判为已译。这里把该
        边界钉住，避免将来有人以为它已被覆盖；同时确认目标是**非中文**时
        同一条文本仍会被拦下（那条路径有语言信号可用）。
        """
        translator_zh = self._translator('zh')
        self.assertFalse(translator_zh._likely_untranslated('新宿駅到着', '新宿駅到着。'))
        translator_en = self._translator('en')
        self.assertTrue(translator_en._likely_untranslated('新宿駅到着', '新宿駅到着。'))

    def test_english_source_japanese_output_is_untranslated(self):
        """源是英文、模型整句返回日文：源里没有假名，靠译文自身占比判定。"""
        translator = self._translator('zh')
        self.assertTrue(translator._likely_untranslated('Tokyo tower', '東京タワー'))

    def test_chinese_translation_with_japanese_loanword_is_not_flagged(self):
        """反向守卫：中文译文里保留一个日文借词不得被判成未译。"""
        translator = self._translator('zh')
        self.assertFalse(
            translator._likely_untranslated('I love anime', '我很喜欢アニメ这部动画'))

    def test_chinese_target_non_chinese_translation_of_hanzi_source(self):
        """目标是英文却整句返回汉字（含标点差异）→ 未译。"""
        translator = self._translator('en')
        self.assertTrue(translator._likely_untranslated('你好，世界', '你好世界。'))

    def test_korean_echo_with_punctuation_change_is_untranslated(self):
        translator = self._translator('zh')
        self.assertTrue(
            translator._likely_untranslated('안녕하세요 여러분', '안녕하세요, 여러분'))


if __name__ == '__main__':
    unittest.main()
