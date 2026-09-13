"""预设标签（Issue #139）在任务管线里的行为锁定测试。

覆盖两段真实代码路径：

1. ``TaskProcessor._generate_tags``：预设生效时「预设在前 + AI 补齐」，关闭「自动生成标签」
   时只写预设且**不调用标签 AI**；预设未生效时与改动前完全一致（不注入预设、不透传
   ``avoid_tags``）；
2. 上传路径 ``_do_upload_to_acfun`` / ``_do_upload_to_bilibili``：真正交给上传器的 ``tags``
   实参已按平台上限合并（AcFun 6 个 / bilibili 12 个），预设排在 AI 标签之前。

上传路径用假上传器捕获实参并返回失败，避免触碰真实网络与真实上传成功后的收尾逻辑。
"""
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from modules import tag_presets as tp
from modules import task_manager as tm


class _FakeAcfunUploader:
    calls = []

    def __init__(self, cookie_file=None):
        self.cookie_file = cookie_file

    def upload_video(self, **kwargs):
        type(self).calls.append(kwargs)
        return False, '模拟上传失败'


class _FakeBilibiliUploader:
    calls = []

    def __init__(self, cookie_file=None):
        self.cookie_file = cookie_file

    def upload_video(self, **kwargs):
        type(self).calls.append(kwargs)
        return False, '模拟上传失败'


def _fake_processor(config):
    processor = tm.TaskProcessor(dict(config))
    return processor


class GenerateTagsTests(unittest.TestCase):
    def _run_generate_tags(self, config, task):
        processor = _fake_processor(config)

        def fake_get_task(_task_id):
            return dict(task)

        def fake_update_task(_task_id, **kwargs):
            task.update({k: v for k, v in kwargs.items() if k != 'silent'})
            return True

        with patch.object(tm, 'get_task', side_effect=fake_get_task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task), \
             patch('modules.ai_enhancer.generate_acfun_tags') as generate_tags:
            generate_tags.return_value = ['AI1', 'AI2', 'AI3', '', '', '']
            processor._generate_tags('task-preset-tags', MagicMock())
        return task, generate_tags

    def test_预设在前且AI补齐剩余名额(self):
        task, generate_tags = self._run_generate_tags(
            {
                'GENERATE_TAGS': True,
                'PRESET_TAGS_ENABLED': True,
                'PRESET_TAGS': '预1\n预2',
            },
            {
                'id': 'task-preset-tags',
                'video_title_translated': '标题',
                'description_translated': '简介',
                'tags_generated': None,
            },
        )

        self.assertEqual(
            json.loads(task['tags_generated']),
            ['预1', '预2', 'AI1', 'AI2', 'AI3'],
        )
        # 预设标签必须作为「不要重复」约束传给 AI，否则补齐会与预设撞车
        self.assertEqual(generate_tags.call_args.kwargs.get('avoid_tags'), ['预1', '预2'])

    def test_关闭自动生成标签时只写预设且不调用AI(self):
        task, generate_tags = self._run_generate_tags(
            {
                'GENERATE_TAGS': False,
                'PRESET_TAGS_ENABLED': True,
                'PRESET_TAGS': '预1, 预2',
            },
            {
                'id': 'task-preset-tags',
                'video_title_translated': '标题',
                'description_translated': '简介',
                'tags_generated': None,
            },
        )

        self.assertEqual(json.loads(task['tags_generated']), ['预1', '预2'])
        generate_tags.assert_not_called()

    def test_预设未生效时保持原有行为(self):
        task, generate_tags = self._run_generate_tags(
            {'GENERATE_TAGS': True, 'PRESET_TAGS_ENABLED': False, 'PRESET_TAGS': '预1'},
            {
                'id': 'task-preset-tags',
                'video_title_translated': '标题',
                'description_translated': '简介',
                'tags_generated': None,
            },
        )

        # 与改动前一致：AI 结果按 6 个截断写回，且不注入预设、不透传 avoid_tags
        self.assertEqual(json.loads(task['tags_generated']), ['AI1', 'AI2', 'AI3', '', '', ''])
        self.assertNotIn('avoid_tags', generate_tags.call_args.kwargs)

    def test_预设启用但留空时等同未启用(self):
        task, generate_tags = self._run_generate_tags(
            {'GENERATE_TAGS': True, 'PRESET_TAGS_ENABLED': True, 'PRESET_TAGS': '  '},
            {
                'id': 'task-preset-tags',
                'video_title_translated': '标题',
                'description_translated': '简介',
                'tags_generated': None,
            },
        )

        self.assertEqual(json.loads(task['tags_generated']), ['AI1', 'AI2', 'AI3', '', '', ''])
        self.assertNotIn('avoid_tags', generate_tags.call_args.kwargs)

    def test_已有标签与预设合并时幂等(self):
        task, _generate_tags = self._run_generate_tags(
            {
                'GENERATE_TAGS': True,
                'PRESET_TAGS_ENABLED': True,
                'PRESET_TAGS': '预1\n预2',
            },
            {
                'id': 'task-preset-tags',
                'video_title_translated': '标题',
                'description_translated': '简介',
                'tags_generated': '["预1", "预2", "AI1"]',
            },
        )

        self.assertEqual(
            json.loads(task['tags_generated']),
            ['预1', '预2', 'AI1', 'AI2', 'AI3'],
        )


class UploadPathTagResolutionTests(unittest.TestCase):
    """真正执行上传路径，捕获传给上传器的 tags 实参。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.video_path = os.path.join(self.temp_dir.name, 'video.mp4')
        self.cover_path = os.path.join(self.temp_dir.name, 'cover.jpg')
        self.cookie_path = os.path.join(self.temp_dir.name, 'cookies.json')
        for path in (self.video_path, self.cover_path, self.cookie_path):
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('{}')
        _FakeAcfunUploader.calls = []
        _FakeBilibiliUploader.calls = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_upload(self, upload_method_name, task, config, uploader_cls, uploader_patch_target,
                    is_bilibili=False):
        processor = tm.TaskProcessor(dict(config))

        def fake_update_task(_task_id, **kwargs):
            task.update({k: v for k, v in kwargs.items() if k != 'silent'})
            return True

        patches = [
            patch.object(tm, 'get_task', side_effect=lambda _task_id: dict(task)),
            patch.object(tm, 'update_task', side_effect=fake_update_task),
            patch.object(tm.TaskProcessor, '_recover_cover_path',
                         side_effect=lambda _task_id, cover_path, _logger: cover_path),
            patch.object(tm, '_get_missing_required_translation_fields', return_value=[]),
            patch.object(tm, 'resolve_cookie_file_path', return_value=self.cookie_path),
            patch.object(tm, 'validate_cookies', return_value=(True, '')),
            patch.object(tm, 'get_task_cancel_event', return_value=None),
            patch(uploader_patch_target, uploader_cls),
        ]
        if is_bilibili:
            patches.append(
                patch('modules.bilibili_zones.collect_valid_tids', return_value={'2001'})
            )

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            getattr(processor, upload_method_name)('task-upload-tags', MagicMock(), subtitle_prepared=True)

        self.assertEqual(len(uploader_cls.calls), 1, '假上传器未被调用，上传路径提前返回了')
        return uploader_cls.calls[0]

    def test_acfun按六个标签合并预设与AI(self):
        task = {
            'id': 'task-upload-tags',
            'upload_target': 'acfun',
            'youtube_url': 'https://www.youtube.com/watch?v=abc',
            'video_path_local': self.video_path,
            'cover_path_local': self.cover_path,
            'video_title_translated': '标题',
            'description_translated': '简介',
            'tags_generated': json.dumps(['AI1', 'AI2', 'AI3', 'AI4', 'AI5', 'AI6', 'AI7'],
                                         ensure_ascii=False),
            'selected_partition_id_acfun': '1001',
            'metadata_json_path_local': '',
        }
        config = {
            'ACFUN_COOKIES_PATH': self.cookie_path,
            'PRESET_TAGS_ENABLED': True,
            'PRESET_TAGS': '预1\n预2',
        }

        kwargs = self._run_upload(
             '_do_upload_to_acfun',
            task,
            config,
            _FakeAcfunUploader,
            'modules.acfun_uploader.AcfunUploader',
        )

        self.assertEqual(kwargs['tags'], ['预1', '预2', 'AI1', 'AI2', 'AI3', 'AI4'])

    def test_bilibili按十二个标签合并预设与AI(self):
        task = {
            'id': 'task-upload-tags',
            'upload_target': 'bilibili',
            'youtube_url': 'https://www.youtube.com/watch?v=abc',
            'video_path_local': self.video_path,
            'cover_path_local': self.cover_path,
            'video_title_translated': '标题',
            'description_translated': '简介',
            'tags_generated': json.dumps([f'AI{i}' for i in range(1, 12)], ensure_ascii=False),
            'selected_partition_id_bilibili': '2001',
            'metadata_json_path_local': '',
        }
        config = {
            'BILIBILI_COOKIES_PATH': self.cookie_path,
            'PRESET_TAGS_ENABLED': True,
            'PRESET_TAGS': '预1\n预2\n预3',
        }

        kwargs = self._run_upload(
            '_do_upload_to_bilibili',
            task,
            config,
            _FakeBilibiliUploader,
            'modules.bilibili_uploader.BilibiliUploader',
            is_bilibili=True,
        )

        self.assertEqual(
            kwargs['tags'],
            ['预1', '预2', '预3'] + [f'AI{i}' for i in range(1, 10)],
        )

    def test_预设未生效时上传标签与改动前一致(self):
        task = {
            'id': 'task-upload-tags',
            'upload_target': 'acfun',
            'youtube_url': 'https://www.youtube.com/watch?v=abc',
            'video_path_local': self.video_path,
            'cover_path_local': self.cover_path,
            'video_title_translated': '标题',
            'description_translated': '简介',
            'tags_generated': '["AI1", "", "AI2", "AI1"]',
            'selected_partition_id_acfun': '1001',
            'metadata_json_path_local': '',
        }
        config = {
            'ACFUN_COOKIES_PATH': self.cookie_path,
            'PRESET_TAGS_ENABLED': False,
            'PRESET_TAGS': '预1',
        }

        kwargs = self._run_upload(
             '_do_upload_to_acfun',
            task,
            config,
            _FakeAcfunUploader,
            'modules.acfun_uploader.AcfunUploader',
        )

        # 与 _normalize_tags_list 对齐：去空、保序、不去重
        self.assertEqual(kwargs['tags'], ['AI1', 'AI2', 'AI1'])


class UploadPathWiringTests(unittest.TestCase):
    def test_上传路径都使用平台感知的标签解析(self):
        source = pathlib.Path(tm.__file__).read_text(encoding='utf-8')
        acfun_body = _function_source(source, '_do_upload_to_acfun')
        bilibili_body = _function_source(source, '_do_upload_to_bilibili')

        self.assertIn('resolve_upload_tags(', acfun_body)
        self.assertIn('PLATFORM_ACFUN', acfun_body)
        self.assertIn('resolve_upload_tags(', bilibili_body)
        self.assertIn('PLATFORM_BILIBILI', bilibili_body)
        # 回归护栏：上传路径不得再退回到只读任务标签（那样预设标签不会生效）
        for body in (acfun_body, bilibili_body):
            self.assertNotIn("_normalize_tags_list(task.get('tags_generated')", body)

    def test_标签阶段门控包含预设标签(self):
        source = pathlib.Path(tm.__file__).read_text(encoding='utf-8')
        self.assertIn(
            "if self.config.get('GENERATE_TAGS', True) or is_preset_tags_effective(self.config):",
            source,
        )


def _function_source(source, name):
    """按缩进截取某个方法（含其函数体）的源码，供源码级回归断言使用。"""
    lines = source.splitlines()
    start = None
    indent = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith(f'def {name}('):
            start = index
            indent = len(line) - len(line.lstrip())
            break
    if start is None:
        raise AssertionError(f'未找到函数 {name}')
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            end = index
            break
    return '\n'.join(lines[start:end])


class PlatformLimitsContractTests(unittest.TestCase):
    def test_平台上限与上传器实现一致(self):
        # 上传器里的硬编码上限一旦变化，tag_presets 的常量必须同步，否则会出现
        # 「解析时说能放 12 个、上传器只收 6 个」的静默丢失。
        root = pathlib.Path(tm.__file__).resolve().parents[1]
        acfun_source = (root / 'modules' / 'acfun_uploader.py').read_text(encoding='utf-8')
        bilibili_source = (root / 'modules' / 'bilibili_uploader.py').read_text(encoding='utf-8')
        self.assertIn(f'len(tags) > {tp.ACFUN_TAG_LIMIT}', acfun_source)
        self.assertIn(f'safe_tags[:{tp.BILIBILI_TAG_LIMIT}]', bilibili_source)
        self.assertIn(f'[:{tp.BILIBILI_TAG_MAX_LEN}]', bilibili_source)


if __name__ == '__main__':
    unittest.main()
