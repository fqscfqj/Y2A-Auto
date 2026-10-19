# -*- coding: utf-8 -*-
"""BILIBILI_SUBMIT_API 的配置层回归测试。

复审指出：`update_config` 原样保存 `bcut` / `bcut-pc` / `bcutpc` 这类别名，
而设置页的 <option> 只认 auto / bcut_pc / web，于是手改 config.json 会出现
「实际走必剪、页面却选中自动」的回显错位。这里守护三条路径：
默认值登记、`update_config` 落盘归一化、`load_config` 读取归一化。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules import config_manager
from modules.config_manager import DEFAULT_CONFIG, load_config, update_config


class _ConfigFileMixin:
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-submit-api-config-')
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


class SubmitApiDefaultTests(unittest.TestCase):
    def test_default_is_auto(self):
        self.assertEqual(DEFAULT_CONFIG['BILIBILI_SUBMIT_API'], 'auto')

    def test_normalizer_returns_canonical_values(self):
        for value in ('bcut', 'bcut-pc', 'BCUTPC', ' bcut_pc '):
            self.assertEqual(
                config_manager._normalize_submit_api_value(value), 'bcut_pc', repr(value))
        self.assertEqual(
            config_manager._normalize_submit_api_value('WEB'), 'web')
        self.assertEqual(
            config_manager._normalize_submit_api_value('unknown'), 'auto')
        self.assertEqual(config_manager._normalize_submit_api_value(None), 'auto')


class UpdateConfigSubmitApiTests(_ConfigFileMixin, unittest.TestCase):
    def test_update_persists_canonical_value(self):
        self.write_config({})
        update_config({'BILIBILI_SUBMIT_API': 'bcut-pc'})
        self.assertEqual(self.read_config()['BILIBILI_SUBMIT_API'], 'bcut_pc')

    def test_update_repairs_unrecognized_value(self):
        self.write_config({})
        update_config({'BILIBILI_SUBMIT_API': 'whatever'})
        self.assertEqual(self.read_config()['BILIBILI_SUBMIT_API'], 'auto')

    def test_web_value_survives_round_trip(self):
        self.write_config({})
        update_config({'BILIBILI_SUBMIT_API': 'web'})
        self.assertEqual(load_config()['BILIBILI_SUBMIT_API'], 'web')


class LoadConfigSubmitApiTests(_ConfigFileMixin, unittest.TestCase):
    def test_load_normalizes_alias_written_by_hand(self):
        self.write_config({'BILIBILI_SUBMIT_API': 'bcut'})
        self.assertEqual(load_config()['BILIBILI_SUBMIT_API'], 'bcut_pc')
        # 归一化结果会回写，下次启动不再反复纠正
        self.assertEqual(self.read_config()['BILIBILI_SUBMIT_API'], 'bcut_pc')

    def test_load_repairs_invalid_persisted_value(self):
        self.write_config({'BILIBILI_SUBMIT_API': 'bcut_pc_v2'})
        self.assertEqual(load_config()['BILIBILI_SUBMIT_API'], 'auto')


if __name__ == '__main__':
    unittest.main()
