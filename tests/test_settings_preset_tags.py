"""设置页「标签预设」（Issue #139）保存路径的行为锁定测试。

覆盖四类行为：

1. 归一化：多种分隔符统一成「每行一个」落盘，重复保存幂等；
2. 数量裁决：超过 12 个只保留前 12 个并告警；超过 6 个提示 AcFun 只上传前 6 个；
3. 长度裁决：超长标签保留原文（上传时按平台截断），但必须给出可见告警；
4. 「勾选启用但留空」等同未启用，且用户能看到这条提示。

断言的是「提交值 → 落盘值」与「用户可见消息」这类外部可观测行为，
不复用 app.py / modules.tag_presets.py 的内部实现细节。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import app as web_app
from modules import config_manager as cm


class _SettingsSaveTestCase(unittest.TestCase):
    """把配置目录指向临时目录，并屏蔽 _perform_settings_save 的外部副作用。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a-preset-tags-')
        self._patchers = []
        for patcher in (
            patch.object(cm, 'get_app_subdir',
                         lambda name: os.path.join(self._tmp, name)),
            patch.object(web_app, 'configure_app', return_value=None),
            patch.object(web_app, '_sync_notification_service', return_value=None),
            patch('modules.task_manager.get_global_task_processor', return_value=None),
            patch.object(web_app.youtube_monitor, 'reload_api_client',
                         return_value=(False, 'missing_api_key')),
            patch.object(web_app.youtube_monitor, 'start_all_schedules', return_value=None),
            patch.object(web_app.youtube_monitor, 'stop_all_schedules', return_value=None),
        ):
            patcher.start()
            self._patchers.append(patcher)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save(self, form):
        result = web_app._perform_settings_save(dict(form), {})
        self.assertTrue(result['success'], msg=result.get('messages'))
        return result

    @staticmethod
    def _warnings(result):
        return [msg['text'] for msg in result['messages'] if msg['category'] == 'warning']


class PresetTagsSettingsSaveTests(_SettingsSaveTestCase):
    def test_多种分隔符统一成每行一个(self):
        result = self._save({
            'PRESET_TAGS_ENABLED': 'on',
            'PRESET_TAGS': 'ASMR, 搬运、助眠;放松\n音乐',
        })

        self.assertEqual(result['updated_config']['PRESET_TAGS'], 'ASMR\n搬运\n助眠\n放松\n音乐')
        self.assertTrue(result['updated_config']['PRESET_TAGS_ENABLED'])
        self.assertEqual(self._warnings(result), [])
        self.assertEqual(result['final_level'], 'success')

    def test_重复保存幂等(self):
        first = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': 'ASMR, 搬运'})
        saved_text = first['updated_config']['PRESET_TAGS']

        second = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': saved_text})

        self.assertEqual(second['updated_config']['PRESET_TAGS'], saved_text)
        self.assertEqual(self._warnings(second), [])

    def test_超过十二个只保留前十二个且告警(self):
        submitted = ','.join(f'标签{i}' for i in range(15))
        result = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': submitted})

        saved = result['updated_config']['PRESET_TAGS'].split('\n')
        self.assertEqual(saved, [f'标签{i}' for i in range(12)])
        warnings = self._warnings(result)
        self.assertTrue(any('12' in text for text in warnings), warnings)
        self.assertTrue(any('AcFun' in text for text in warnings), warnings)
        self.assertEqual(result['final_level'], 'warning')

    def test_超过六个提示AcFun只上传前六个(self):
        result = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': 'a,b,c,d,e,f,g'})

        self.assertEqual(result['updated_config']['PRESET_TAGS'].split('\n'), list('abcdefg'))
        warnings = self._warnings(result)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('AcFun', warnings[0])

    def test_超长标签保留原文但告警(self):
        long_tag = 'x' * 25
        result = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': f'ASMR,{long_tag}'})

        self.assertIn(long_tag, result['updated_config']['PRESET_TAGS'].split('\n'))
        self.assertTrue(any('20' in text for text in self._warnings(result)),
                        self._warnings(result))

    def test_勾选启用但留空时提示等同未启用(self):
        result = self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': '  ,、\n '})

        self.assertEqual(result['updated_config']['PRESET_TAGS'], '')
        warnings = self._warnings(result)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('等同于未启用', warnings[0])

    def test_未提交预设键时不改动已有配置(self):
        self._save({'PRESET_TAGS_ENABLED': 'on', 'PRESET_TAGS': 'ASMR'})

        # 只提交其它键：既不能清空预设，也不能产生与预设相关的提示
        result = self._save({'PRESET_TAGS': 'ASMR'})

        self.assertEqual(result['updated_config']['PRESET_TAGS'], 'ASMR')
        self.assertEqual(self._warnings(result), [])

    def test_未勾选时不产生任何预设提示(self):
        result = self._save({'PRESET_TAGS': 'ASMR, 搬运'})

        self.assertEqual(result['updated_config']['PRESET_TAGS'], 'ASMR\n搬运')
        self.assertFalse(result['updated_config']['PRESET_TAGS_ENABLED'])
        self.assertEqual(self._warnings(result), [])


if __name__ == '__main__':
    unittest.main()
