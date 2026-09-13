"""预设标签（Issue #139）解析、归一化与合并的行为锁定测试。

覆盖四类行为：

1. 解析/归一化：多种分隔符、去空、大小写去重、保序、超量与超长的用户可见告警、
   二次归一化幂等（设置页每次保存都会重跑一遍归一化）；
2. 生效判定：未勾选 / 留空 / 只有分隔符都等同未启用；
3. 合并：预设在前、去重、按平台上限截断（AcFun 6 个 / 10 字，bilibili 12 个 / 20 字）、
   未知平台按 AcFun 的保守口径；
4. 零行为变化：预设未生效时 ``resolve_upload_tags`` 必须原样返回任务标签
   （含重复项与 JSON 解析失败的空列表），与 ``task_manager._normalize_tags_list`` 对齐。

断言的都是「输入 → 输出」这类外部可观测行为，不复用模块内部实现细节。
"""
import unittest

from modules import tag_presets as tp


class ParsePresetTagsTests(unittest.TestCase):
    def test_多种分隔符可以混用(self):
        raw = 'ASMR, 搬运、助眠;放松\n音乐\r\n现场'
        self.assertEqual(
            tp.parse_preset_tags(raw),
            ['ASMR', '搬运', '助眠', '放松', '音乐', '现场'],
        )

    def test_接受列表与JSON数组字符串(self):
        self.assertEqual(tp.parse_preset_tags(['ASMR', ' 搬运 ']), ['ASMR', '搬运'])
        self.assertEqual(tp.parse_preset_tags('["ASMR", "搬运"]'), ['ASMR', '搬运'])
        self.assertEqual(tp.parse_preset_tags(None), [])
        self.assertEqual(tp.parse_preset_tags('   '), [])

    def test_去空去重且保持首次出现顺序(self):
        self.assertEqual(
            tp.parse_preset_tags('ASMR,,asmr，搬运, ,ASMR'),
            ['ASMR', '搬运'],
        )

    def test_解析不截断长度也不限数量(self):
        long_tag = 'x' * 30
        tags = [f'标签{i}' for i in range(20)] + [long_tag]
        parsed = tp.parse_preset_tags('\n'.join(tags))
        self.assertEqual(len(parsed), 21)
        self.assertEqual(parsed[-1], long_tag)


class NormalizePresetTagsConfigTests(unittest.TestCase):
    def test_落盘文本为每行一个且二次归一化幂等(self):
        text, warnings = tp.normalize_preset_tags_config('ASMR, 搬运、助眠')
        self.assertEqual(text, 'ASMR\n搬运\n助眠')
        self.assertEqual(warnings, [])
        again, again_warnings = tp.normalize_preset_tags_config(text)
        self.assertEqual(again, text)
        self.assertEqual(again_warnings, [])

    def test_超过十二个只保留前十二个并告警(self):
        text, warnings = tp.normalize_preset_tags_config(
            ','.join(f'标签{i}' for i in range(15))
        )
        self.assertEqual(text.split('\n'), [f'标签{i}' for i in range(12)])
        self.assertTrue(any('12' in warning for warning in warnings), warnings)
        # 截断后仍有 12 个，AcFun 只上传前 6 个的提示必须同样出现
        self.assertTrue(any('AcFun' in warning for warning in warnings), warnings)

    def test_超过六个提示AcFun只上传前六个(self):
        _text, warnings = tp.normalize_preset_tags_config('a,b,c,d,e,f,g')
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('AcFun', warnings[0])

    def test_超长标签保留原文但告警(self):
        long_tag = 'x' * 25
        text, warnings = tp.normalize_preset_tags_config(f'ASMR,{long_tag}')
        # 不丢弃用户数据：上传时按平台截断，配置里保留原文
        self.assertIn(long_tag, text.split('\n'))
        self.assertTrue(any('20' in warning for warning in warnings), warnings)

    def test_空配置不产生告警(self):
        self.assertEqual(tp.normalize_preset_tags_config(''), ('', []))
        self.assertEqual(tp.normalize_preset_tags_config(' , 、; \n'), ('', []))


class PresetTagsEffectiveTests(unittest.TestCase):
    def test_未勾选即未生效(self):
        self.assertFalse(tp.is_preset_tags_enabled({}))
        self.assertFalse(tp.is_preset_tags_effective({'PRESET_TAGS': 'ASMR'}))
        self.assertFalse(
            tp.is_preset_tags_effective({'PRESET_TAGS_ENABLED': False, 'PRESET_TAGS': 'ASMR'})
        )

    def test_勾选但留空等同未启用(self):
        self.assertTrue(tp.is_preset_tags_enabled({'PRESET_TAGS_ENABLED': 'on'}))
        self.assertFalse(
            tp.is_preset_tags_effective({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': '  ,、\n '})
        )

    def test_勾选且有内容即生效(self):
        self.assertTrue(
            tp.is_preset_tags_effective({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': 'ASMR'})
        )

    def test_非字典配置不抛异常(self):
        self.assertFalse(tp.is_preset_tags_enabled(None))
        self.assertFalse(tp.is_preset_tags_effective('PRESET_TAGS_ENABLED'))


class MergeTagsTests(unittest.TestCase):
    def test_预设在前并大小写不敏感去重(self):
        merged = tp.merge_tags(['ASMR', '搬运'], ['asmr', '游戏', '搬运'], limit=10)
        self.assertEqual(merged, ['ASMR', '搬运', '游戏'])

    def test_按上限截断且预设优先(self):
        merged = tp.merge_tags(['预1', '预2', '预3'], ['a', 'b', 'c', 'd'], limit=6)
        self.assertEqual(merged, ['预1', '预2', '预3', 'a', 'b', 'c'])

    def test_单标签按上限截断(self):
        # 先 trim 再截断：'  abcdef  ' -> 'abcdef' -> 'abc'
        self.assertEqual(
            tp.merge_tags(['  abcdef  ', 'ab'], [], limit=6, tag_max_len=3),
            ['abc', 'ab'],
        )

    def test_单个字符串入参不会被按字符拆开(self):
        self.assertEqual(tp.merge_tags('ASMR', ['搬运'], limit=6), ['ASMR', '搬运'])


class ResolveUploadTagsTests(unittest.TestCase):
    def test_预设未生效时原样返回且保留重复项(self):
        # 与 task_manager._normalize_tags_list 对齐：去空、保序、不去重
        raw = '["ASMR", "", "  ", "ASMR", 123]'
        self.assertEqual(
            tp.resolve_upload_tags({'PRESET_TAGS_ENABLED': False}, raw, tp.PLATFORM_ACFUN),
            ['ASMR', 'ASMR', '123'],
        )

    def test_预设未生效时JSON解析失败视为空(self):
        self.assertEqual(
            tp.resolve_upload_tags({'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': ''}, 'not-json', 'acfun'),
            [],
        )
        self.assertEqual(tp.resolve_upload_tags({}, 'not-json', 'bilibili'), [])

    def test_acfun按六个数与十个字截断(self):
        long_preset = '预设标签一二三四五六七八九十十一'
        config = {
            'PRESET_TAGS_ENABLED': True,
            'PRESET_TAGS': long_preset,
        }
        tags = tp.resolve_upload_tags(
            config,
            '["AI1", "AI2", "AI3", "AI4", "AI5", "AI6", "AI7"]',
            tp.PLATFORM_ACFUN,
        )
        self.assertEqual(len(tags), tp.ACFUN_TAG_LIMIT)
        self.assertEqual(tags[0], long_preset[:10])
        self.assertEqual(tags[1:], ['AI1', 'AI2', 'AI3', 'AI4', 'AI5'])

    def test_bilibili按十二个数与二十个字截断(self):
        config = {
            'PRESET_TAGS_ENABLED': True,
            'PRESET_TAGS': '预' + 'x' * 24,
        }
        existing = ['AI%d' % i for i in range(1, 12)]
        tags = tp.resolve_upload_tags(config, existing, tp.PLATFORM_BILIBILI)
        self.assertEqual(len(tags), tp.BILIBILI_TAG_LIMIT)
        self.assertEqual(tags[0], '预' + 'x' * 19)
        self.assertEqual(tags[-1], 'AI11')

    def test_未知平台按acfun的保守口径(self):
        config = {'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': 'p1,p2,p3,p4,p5,p6'}
        existing = ['a', 'b', 'c']
        self.assertEqual(
            tp.resolve_upload_tags(config, existing, None),
            ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'],
        )
        self.assertEqual(
            tp.resolve_upload_tags(config, existing, 'unknown-platform'),
            ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'],
        )

    def test_重复调用幂等(self):
        config = {'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': '预1\n预2'}
        first = tp.resolve_upload_tags(config, '["AI1", "预1"]', tp.PLATFORM_BILIBILI)
        second = tp.resolve_upload_tags(config, first, tp.PLATFORM_BILIBILI)
        self.assertEqual(first, ['预1', '预2', 'AI1'])
        self.assertEqual(second, first)

    def test_预设已被任务标签包含时不会重复(self):
        config = {'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': 'ASMR'}
        # 预设的书写形式优先（首个出现者胜出），任务里的同义标签被去掉
        self.assertEqual(
            tp.resolve_upload_tags(config, '["asmr", "游戏"]', 'acfun'),
            ['ASMR', '游戏'],
        )

    def test_日志失败不影响返回结果(self):
        class _BrokenLogger:
            def warning(self, *_args, **_kwargs):
                raise RuntimeError('log sink down')

        config = {'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': 'p1\np2\np3\np4\np5\np6\np7'}
        self.assertEqual(
            tp.resolve_upload_tags(config, '[]', 'acfun', logger_obj=_BrokenLogger()),
            ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'],
        )


if __name__ == '__main__':
    unittest.main()
