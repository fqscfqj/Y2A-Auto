"""``generate_acfun_tags`` 的预设去重约束（Issue #139）行为锁定测试。

预设标签生效时，任务管线会把预设标签作为 ``avoid_tags`` 传给标签生成：
既要写进提示词（告诉模型不要复用），也要在解析后真的把撞车项过滤掉——
只做前者时模型仍可能返回重复标签，合并阶段就会出现「一个标签占两个名额」。

同时锁定向后兼容：不传 ``avoid_tags`` 时提示词与过滤行为与改动前一致。
"""
import unittest
from unittest.mock import patch

from modules import ai_enhancer


def _run_generate_tags(parsed, avoid_tags=None):
    with patch.object(ai_enhancer, 'get_openai_client', return_value=object()), \
         patch.object(ai_enhancer, '_request_json_object', return_value=parsed) as request_json:
        tags = ai_enhancer.generate_acfun_tags(
            '测试标题',
            '这是一段用于测试的简介文本。',
            openai_config={'OPENAI_API_KEY': 'test-key'},
            avoid_tags=avoid_tags,
        )
    return tags, request_json


class GenerateAcfunTagsAvoidTests(unittest.TestCase):
    def test_预设标签被提示词点名且解析后被过滤(self):
        tags, request_json = _run_generate_tags(
            {'tags': ['预设A', '新1', '新2', '新3']},
            avoid_tags=['预设A'],
        )

        self.assertNotIn('预设A', tags)
        self.assertEqual(tags[:3], ['新1', '新2', '新3'])

        system_prompt = request_json.call_args.kwargs.get('system_prompt', '')
        self.assertIn('人工预设占用', system_prompt)
        self.assertIn('预设A', system_prompt)

    def test_截断到十字后的撞车同样被过滤(self):
        # AcFun 上传前每个标签都会被截到 10 字，仅比对原文会让 12 字预设与模型的
        # 10 字版本同时出现（上传时变成同一个标签）
        long_preset = '预设标签一二三四五六七八九十十一'
        tags, _request_json = _run_generate_tags(
            {'tags': [long_preset[:10], '新1']},
            avoid_tags=[long_preset],
        )

        self.assertEqual(tags[0], '新1')

    def test_超长预设拼进提示词时按二十字收口(self):
        # 预设允许保留超长原文（设置页只告警不裁剪），但拼进 system prompt 时按
        # bilibili 的单标签上限收口，避免超长文本白占提示额度
        long_preset = 'x' * 40
        tags, request_json = _run_generate_tags({'tags': ['新1']}, avoid_tags=[long_preset])

        system_prompt = request_json.call_args.kwargs.get('system_prompt', '')
        self.assertIn('x' * 20, system_prompt)
        self.assertNotIn('x' * 21, system_prompt)
        self.assertEqual(tags[0], '新1')

    def test_列表长度不足六时保持补空行为(self):
        tags, _request_json = _run_generate_tags({'tags': ['新1', '新2']}, avoid_tags=['预设A'])
        self.assertEqual(tags, ['新1', '新2', '', '', '', ''])

    def test_不传预设时不改变提示词与结果(self):
        tags, request_json = _run_generate_tags({'tags': ['A', 'B']})

        self.assertEqual(tags, ['A', 'B', '', '', '', ''])
        system_prompt = request_json.call_args.kwargs.get('system_prompt', '')
        self.assertNotIn('人工预设占用', system_prompt)


if __name__ == '__main__':
    unittest.main()

