# -*- coding: utf-8 -*-
"""VIDEO_CPU_CODEC 的配置层回归测试。

复审指出：全仓库没有测试引用 `normalize_video_cpu_codec`，也没驱动
`load_config` / `update_config` 的 `VIDEO_CPU_CODEC` 分支 —— 同族的
`normalize_video_cpu_preset` 有专门文件（tests/test_video_cpu_preset_config.py），
新增分支同样需要守护。这里补齐三条路径：默认值登记、`load_config` 归一化、
`update_config` 归一化。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules import config_manager
from modules.config_manager import (
    DEFAULT_CONFIG,
    load_config,
    normalize_video_cpu_codec,
    update_config,
)


class NormalizeVideoCpuCodecTests(unittest.TestCase):
    def test_default_is_registered(self):
        self.assertEqual(DEFAULT_CONFIG['VIDEO_CPU_CODEC'], 'x264')

    def test_supported_values(self):
        for value in ('x264', 'x265', ' X265 ', 'X264'):
            self.assertIn(normalize_video_cpu_codec(value), ('x264', 'x265'))
        self.assertEqual(normalize_video_cpu_codec('  X265  '), 'x265')

    def test_invalid_values_fall_back_to_the_historical_default(self):
        for bad in ('libx265', 'hevc', 'h265', '', None, 265, True, []):
            self.assertEqual(normalize_video_cpu_codec(bad), 'x264', repr(bad))

    def test_explicit_default_is_respected(self):
        self.assertEqual(normalize_video_cpu_codec('bogus', 'x265'), 'x265')

    def test_invalid_default_itself_falls_back(self):
        self.assertEqual(normalize_video_cpu_codec('bogus', 'nope'), 'x264')


class _ConfigFileMixin:
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-cpu-codec-config-')
        self.config_path = os.path.join(self.tmpdir, 'config.json')
        # load_config / update_config 都通过 get_app_subdir('config') 定位 config.json
        self._path_patch = patch.object(
            config_manager, 'get_app_subdir', side_effect=lambda _name: self.tmpdir)
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_config(self, data):
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)

    def read_config(self):
        with open(self.config_path, 'r', encoding='utf-8') as handle:
            return json.load(handle)


class LoadConfigCpuCodecTests(_ConfigFileMixin, unittest.TestCase):
    def test_load_normalizes_persisted_value(self):
        self.write_config({'VIDEO_CPU_CODEC': 'X265'})
        self.assertEqual(load_config()['VIDEO_CPU_CODEC'], 'x265')

    def test_load_repairs_invalid_persisted_value(self):
        self.write_config({'VIDEO_CPU_CODEC': 'libx265'})
        self.assertEqual(load_config()['VIDEO_CPU_CODEC'], 'x264')


class UpdateConfigCpuCodecTests(_ConfigFileMixin, unittest.TestCase):
    def test_update_normalizes_new_value(self):
        self.write_config({})
        update_config({'VIDEO_CPU_CODEC': ' X265 '})
        self.assertEqual(self.read_config()['VIDEO_CPU_CODEC'], 'x265')

    def test_update_repairs_invalid_new_value(self):
        self.write_config({'VIDEO_CPU_CODEC': 'x265'})
        update_config({'VIDEO_CPU_CODEC': 'hevc'})
        self.assertEqual(self.read_config()['VIDEO_CPU_CODEC'], 'x264')

    def test_persisted_value_survives_a_reload(self):
        self.write_config({})
        update_config({'VIDEO_CPU_CODEC': 'x265'})
        self.assertEqual(load_config()['VIDEO_CPU_CODEC'], 'x265')


if __name__ == '__main__':
    unittest.main()
