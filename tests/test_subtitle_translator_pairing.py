#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕翻译「译文 / 原文配对契约」回归测试。

覆盖：
- 按下标配对（下标对象数组完整 / 乱序 / 缺项）
- 无下标纯数组按位置配对（条数必须严格相等）
- 行首编号剥离收紧（不再损坏数字开头的正文）
- 未译残留策略（SUBTITLE_TRANSLATION_ALLOW_PARTIAL）
- 批次字符预算（超长单条独占一批、不截断）

全部用 mock 替换 LLM 调用，不发起真实网络请求。
"""

import json
import logging
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.subtitle_translator import (
    LLMRequester,
    SubtitleAlignmentError,
    SubtitleItem,
    SubtitleReader,
    SubtitleTranslator,
    SubtitleWriter,
    TranslationConfig,
    _should_fail_translation_residue,
    _split_items_into_batches,
)

_LOGGER_NAME = 'test_subtitle_pairing'
_PATCH_TARGET = 'modules.subtitle_translator.openai_chat_create_with_thinking_control'


def _indexed_response(texts, resolver=None):
    """按输入条数生成「带下标的 JSON 对象数组」响应。"""
    resolver = resolver or (lambda text, position: f"译文{position}")
    return json.dumps(
        {
            "translations": [
                {"index": position, "translation": resolver(text, position)}
                for position, text in enumerate(texts)
            ]
        },
        ensure_ascii=False,
    )


def _make_requester(responder=None):
    """构造不发起真实请求的 LLMRequester（LLM 调用通过 patch 注入）。

    responder: callable(texts) -> str，默认返回带下标的完整配对响应；
    同时把每次请求实际发送的 texts 记录到 requester.sent_batches。
    """
    requester = LLMRequester.__new__(LLMRequester)
    requester.client = object()  # 通过 _init_client 的真实性检查，但不发生网络调用
    requester.openai_config = {
        'OPENAI_MODEL_NAME': 'test-model',
        'OPENAI_THINKING_ENABLED': False,
    }
    requester.task_id = 'test'
    requester.logger = logging.getLogger(_LOGGER_NAME)
    requester._log_lock = threading.Lock()
    requester._capability_lock = threading.Lock()
    requester._json_mode_disabled = False
    requester._batch_counter = 0
    requester._batch_log_interval = 10
    requester.sent_batches = []

    def fake_create(client, create_kwargs, **kwargs):
        user_prompt = create_kwargs['messages'][1]['content']
        texts = json.loads(user_prompt)['texts']
        requester.sent_batches.append(list(texts))
        content = responder(texts) if responder else _indexed_response(texts)
        return SimpleNamespace(choices=[SimpleNamespace(message={'content': content})])

    requester.fake_create = fake_create
    return requester


def _make_translator(requester=None, **config_overrides):
    """构造不初始化客户端/日志文件的 SubtitleTranslator。"""
    config_kwargs = {
        'api_key': 'test-key',
        'target_language': 'zh',
        'batch_size': 3,
        'max_retries': 2,
        'retry_delay': 0,
        'max_workers': 1,
    }
    config_kwargs.update(config_overrides)
    translator = SubtitleTranslator.__new__(SubtitleTranslator)
    translator.config = TranslationConfig(**config_kwargs)
    translator.task_id = 'test'
    translator.logger = logging.getLogger(_LOGGER_NAME)
    translator.openai_config = {'OPENAI_MODEL_NAME': 'test-model'}
    translator.llm_requester = requester or _make_requester()
    translator.reader = SubtitleReader()
    translator.writer = SubtitleWriter()
    return translator


def _make_items(source_texts):
    return [
        SubtitleItem(
            index=position + 1,
            start_time=f'00:00:{position:02d},000',
            end_time=f'00:00:{position + 1:02d},000',
            source_text=text,
        )
        for position, text in enumerate(source_texts)
    ]


class IndexedPairingTests(unittest.TestCase):
    """E1：按下标配对。"""

    def test_missing_middle_index_fails_batch_without_shifted_text(self):
        # 模型漏掉 index=1：绝不允许把 index=2 的译文回填到第 2 条
        response = json.dumps(
            {
                "items": [
                    {"index": 0, "translation": "第一条译文"},
                    {"index": 2, "translation": "第三条译文"},
                ]
            },
            ensure_ascii=False,
        )
        requester = _make_requester(lambda texts: response)

        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': response}, 3, 'missing_middle'
        )
        self.assertIsNone(parsed)
        self.assertFalse(ok)

        with patch(_PATCH_TARGET, side_effect=requester.fake_create):
            with self.assertRaises(SubtitleAlignmentError):
                requester.translate_batch(['alpha', 'bravo', 'charlie'], 'zh', batch_id='missing_middle')

    def test_missing_middle_index_end_to_end_keeps_all_translations_empty(self):
        # 该响应形状用旧实现（纯数组展开 + 截断/补空串 + 按下标位置回填）会得到
        # ['甲', '丙', ''] 的错位结果，这里必须整体失败而不是错位烧录。
        response = json.dumps(
            {"translations": [{"index": 0, "translation": "甲"}, {"index": 2, "translation": "丙"}]},
            ensure_ascii=False,
        )
        requester = _make_requester(lambda texts: response)
        translator = _make_translator(requester, batch_size=5)
        items = _make_items(['alpha', 'bravo', 'charlie'])

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, 'out.srt')
            with patch(_PATCH_TARGET, side_effect=requester.fake_create):
                result = translator._translate_concurrent(items, output_path)

            self.assertFalse(result)
            # 不产生错位译文：全部条目保持空译文（批次失败语义）
            self.assertEqual([item.translated_text for item in items], ['', '', ''])
            self.assertFalse(os.path.exists(output_path))

    def test_short_plain_array_fails_batch(self):
        response = json.dumps(["第一条译文", "第二条译文"], ensure_ascii=False)
        requester = _make_requester(lambda texts: response)

        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': response}, 3, 'short_array'
        )
        self.assertIsNone(parsed)
        self.assertFalse(ok)

        with patch(_PATCH_TARGET, side_effect=requester.fake_create):
            with self.assertRaises(SubtitleAlignmentError):
                requester.translate_batch(['alpha', 'bravo', 'charlie'], 'zh', batch_id='short_array')

    def test_exact_plain_array_pairs_in_order(self):
        response = json.dumps(["译文甲", "译文乙", "译文丙"], ensure_ascii=False)
        requester = _make_requester(lambda texts: response)

        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': response}, 3, 'exact_array'
        )
        self.assertTrue(ok)
        self.assertEqual(parsed, ['译文甲', '译文乙', '译文丙'])

    def test_out_of_order_indexed_array_is_refilled_by_index(self):
        response = json.dumps(
            {
                "translations": [
                    {"index": 2, "translation": "译文丙"},
                    {"index": 0, "translation": "译文甲"},
                    {"index": 1, "translation": "译文乙"},
                ]
            },
            ensure_ascii=False,
        )
        requester = _make_requester(lambda texts: response)

        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': response}, 3, 'shuffled'
        )
        self.assertTrue(ok)
        self.assertEqual(parsed, ['译文甲', '译文乙', '译文丙'])

        translator = _make_translator(requester, batch_size=5)
        items = _make_items(['alpha', 'bravo', 'charlie'])
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, 'out.srt')
            with patch(_PATCH_TARGET, side_effect=requester.fake_create):
                result = translator._translate_concurrent(items, output_path)

            self.assertTrue(result)
            self.assertEqual(
                [item.translated_text for item in items],
                ['译文甲', '译文乙', '译文丙'],
            )
            with open(output_path, 'r', encoding='utf-8') as handle:
                content = handle.read()
            self.assertLess(content.index('译文甲'), content.index('译文乙'))
            self.assertLess(content.index('译文乙'), content.index('译文丙'))

    def test_index_key_and_translation_key_variants(self):
        response = json.dumps(
            [
                {"idx": 0, "text": "变体甲"},
                {"i": 1, "t": "变体乙"},
            ],
            ensure_ascii=False,
        )
        requester = _make_requester(lambda texts: response)

        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': response}, 2, 'variants'
        )
        self.assertTrue(ok)
        self.assertEqual(parsed, ['变体甲', '变体乙'])

    def test_numbered_plain_lines_with_gap_fail(self):
        # 纯文本编号行缺一项（1,2,4）→ 结构不可信，判定失败
        requester = _make_requester()
        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': '1. 第一句\n2. 第二句\n4. 第四句'}, 3, 'plain_gap',
        )
        self.assertIsNone(parsed)
        self.assertFalse(ok)

    def test_numbered_plain_lines_sequential_are_accepted(self):
        requester = _make_requester()
        parsed, ok = requester._parse_structured_translation_result_with_status(
            {'content': '1. 第一句\n2. 第二句'}, 2, 'plain_ok',
        )
        self.assertTrue(ok)
        self.assertEqual(parsed, ['第一句', '第二句'])


class LeadingIndexStripTests(unittest.TestCase):
    """E2：行首编号剥离收紧。"""

    def setUp(self):
        self.translator = SubtitleTranslator.__new__(SubtitleTranslator)

    def test_decimal_text_is_preserved(self):
        self.assertEqual(
            self.translator._sanitize_translated_text('10.5% 的人表示支持'),
            '10.5% 的人表示支持',
        )

    def test_chinese_enumeration_text_is_preserved(self):
        self.assertEqual(
            self.translator._sanitize_translated_text('3、4 号方案都可行'),
            '3、4 号方案都可行',
        )

    def test_numbered_list_is_stripped(self):
        self.assertEqual(
            self.translator._sanitize_translated_text('1. 你好\n2. 世界'),
            '你好\n世界',
        )

    def test_single_line_numbered_text_is_not_treated_as_list(self):
        # 单行无法证明是编号清单，保持原样（不损坏数字信息）
        self.assertEqual(
            self.translator._sanitize_translated_text('1. 你好'),
            '1. 你好',
        )


class ResidualUntranslatedPolicyTests(unittest.TestCase):
    """E3：未译残留策略。"""

    def _items_with_one_untranslated(self):
        items = _make_items(['alpha', 'bravo', 'charlie'])
        items[0].translated_text = '译文甲'
        items[1].translated_text = 'bravo'  # 与原文一致 → 判为未译残留
        items[2].translated_text = '译文丙'
        return items

    def test_default_policy_fails_on_any_residual(self):
        translator = _make_translator()
        self.assertFalse(translator.config.allow_partial)
        self.assertFalse(translator._finalize_residual_untranslated_items(self._items_with_one_untranslated()))

    def test_allow_partial_keeps_residual_with_marker(self):
        translator = _make_translator(allow_partial=True)
        items = self._items_with_one_untranslated()
        self.assertTrue(translator._finalize_residual_untranslated_items(items))
        self.assertTrue(items[1].residual_untranslated)
        self.assertEqual(items[1].translated_text, '')
        self.assertEqual(items[0].translated_text, '译文甲')
        self.assertFalse(items[0].residual_untranslated)
        self.assertFalse(items[2].residual_untranslated)

    def test_allow_partial_write_falls_back_to_source_text(self):
        translator = _make_translator(allow_partial=True)
        items = self._items_with_one_untranslated()
        translator._finalize_residual_untranslated_items(items)
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, 'partial.srt')
            self.assertTrue(translator._write_translated_file(items, output_path))
            with open(output_path, 'r', encoding='utf-8') as handle:
                content = handle.read()
        self.assertIn('译文甲', content)
        self.assertIn('bravo', content)  # 未译残留按写盘回退语义输出原文

    def test_residue_threshold_helper_respects_policy_flag(self):
        self.assertTrue(_should_fail_translation_residue(100, 1, allow_partial=False))
        self.assertFalse(_should_fail_translation_residue(100, 1, allow_partial=True))
        self.assertTrue(_should_fail_translation_residue(100, 20, allow_partial=True))

    def test_untranslated_residual_blocks_translation_end_to_end(self):
        # 'bravo' 始终返回原文 → 补翻/严格补救也无法消除 → 默认策略下整体失败且不写盘
        requester = _make_requester(
            lambda texts: _indexed_response(
                texts,
                resolver=lambda text, position: text if text == 'bravo' else f'译文{position}',
            )
        )
        translator = _make_translator(requester, batch_size=5)
        items = _make_items(['alpha', 'bravo', 'charlie'])

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, 'out.srt')
            with patch(_PATCH_TARGET, side_effect=requester.fake_create):
                result = translator._translate_concurrent(items, output_path)

            self.assertFalse(result)
            self.assertFalse(os.path.exists(output_path))


class BatchCharBudgetTests(unittest.TestCase):
    """E4：批次字符预算。"""

    def test_oversized_item_occupies_its_own_batch_without_truncation(self):
        long_text = 'x' * 4000
        items = _make_items([long_text, 'short one', 'short two'])
        batches = _split_items_into_batches(items, batch_size=10, max_chars_per_batch=100)

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 1)
        self.assertIs(batches[0][0], items[0])
        self.assertEqual(batches[0][0].source_text, long_text)  # 不截断
        self.assertEqual([item.source_text for item in batches[1]], ['short one', 'short two'])

    def test_count_limit_still_applies_with_generous_char_budget(self):
        items = _make_items(['a' * 10, 'b' * 10, 'c' * 10])
        batches = _split_items_into_batches(items, batch_size=2, max_chars_per_batch=1000)
        self.assertEqual([[len(batch) for batch in batches]], [[2, 1]])

    def test_invalid_char_budget_falls_back_to_default(self):
        items = _make_items(['a' * 10, 'b' * 10])
        batches = _split_items_into_batches(items, batch_size=5, max_chars_per_batch='bad')
        self.assertEqual([[len(batch) for batch in batches]], [[2]])

    def test_translation_uses_char_budget_and_sends_full_text(self):
        long_text = 'l' * 150
        requester = _make_requester()
        translator = _make_translator(requester, batch_size=10, max_chars_per_batch=60)
        items = _make_items([long_text, 'short one', 'short two'])

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, 'out.srt')
            with patch(_PATCH_TARGET, side_effect=requester.fake_create):
                result = translator._translate_concurrent(items, output_path)

        self.assertTrue(result)
        self.assertEqual(len(requester.sent_batches), 2)
        self.assertEqual(requester.sent_batches[0], [long_text])  # 超长单条独占一批且内容完整
        self.assertEqual(requester.sent_batches[1], ['short one', 'short two'])


if __name__ == '__main__':
    unittest.main()
