"""SUBTITLE_QC_TIMEOUT_SECONDS 根因修复链路测试。

原问题（reviewer）：该独立超时键从未进入 DEFAULT_CONFIG，因此：
  1. 被 _prune_unknown_config_keys 当作未知键删除；
  2. 不在设置页、不被 _perform_settings_save 持久化与校验；
  3. 即便手工写入 config.json，下次保存也会被 prune 抹掉，永远无法真正生效。

本测试覆盖「声明 → 保存 → 落盘 → load_config 读回 → 质检客户端消费」的完整链路，
以及越界 / 负值 / 非数字值的回退，从根因上证明该键现在可持久化且安全可用。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import app as web_app
from modules import config_manager as cm
from modules.config_manager import load_config, update_config, _prune_unknown_config_keys


class SubtitleQcTimeoutRootCauseTests(unittest.TestCase):
    """声明层根因：键必须在 DEFAULT_CONFIG 且不被 prune 删除。"""

    def test_default_config_declares_qc_timeout(self):
        self.assertIn('SUBTITLE_QC_TIMEOUT_SECONDS', cm.DEFAULT_CONFIG)
        self.assertEqual(cm.DEFAULT_CONFIG['SUBTITLE_QC_TIMEOUT_SECONDS'], 120)

    def test_prune_keeps_qc_timeout_but_drops_unknown(self):
        # 旧实现未知键会被删除，新实现 SUBTITLE_QC_TIMEOUT_SECONDS 必须保留
        clean, removed = _prune_unknown_config_keys({
            'SUBTITLE_QC_TIMEOUT_SECONDS': '200',
            'SOME_RANDOM_LEAKED_KEY': 'x',
        })
        self.assertIn('SUBTITLE_QC_TIMEOUT_SECONDS', clean)
        self.assertEqual(clean['SUBTITLE_QC_TIMEOUT_SECONDS'], '200')
        self.assertIn('SOME_RANDOM_LEAKED_KEY', removed)


class SubtitleQcTimeoutPersistenceChainTests(unittest.TestCase):
    """保存 → 落盘 → 读回 → 消费 全链路（模拟真实设置页提交）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="y2a-qc-timeout-")
        self._patchers = []
        # 1) 配置目录指向临时目录，隔离 update_config/load_config 的读写文件
        p_subdir = patch.object(cm, "get_app_subdir",
                                lambda name: os.path.join(self._tmp, name))
        p_subdir.start(); self._patchers.append(p_subdir)
        # 2) 屏蔽 _perform_settings_save 的副作用：任务处理器 / 通知 / YouTube 调度
        p_conf = patch.object(web_app, "configure_app", return_value=None)
        p_conf.start(); self._patchers.append(p_conf)
        p_sync = patch.object(web_app, "_sync_notification_service", return_value=None)
        p_sync.start(); self._patchers.append(p_sync)
        p_gtp = patch("modules.task_manager.get_global_task_processor", return_value=None)
        p_gtp.start(); self._patchers.append(p_gtp)
        p_reload = patch.object(web_app.youtube_monitor, "reload_api_client",
                                return_value=(False, "missing_api_key"))
        p_reload.start(); self._patchers.append(p_reload)
        p_start = patch.object(web_app.youtube_monitor, "start_all_schedules", return_value=None)
        p_start.start(); self._patchers.append(p_start)
        p_stop = patch.object(web_app.youtube_monitor, "stop_all_schedules", return_value=None)
        p_stop.start(); self._patchers.append(p_stop)
        # 不设 SPEECH_RECOGNITION_ENABLED / SUBTITLE_EMBED_IN_VIDEO → 跳过 FFmpeg 分支

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save_and_trace(self, form_value):
        result = web_app._perform_settings_save(
            {'SUBTITLE_QC_TIMEOUT_SECONDS': form_value}, {})
        self.assertTrue(result['success'], msg=result.get('messages'))
        saved = result['updated_config']
        # load_config 再次读盘，模拟进程重启后读取（含 prune + DEFAULT 合并）
        reloaded = load_config()
        # 质检客户端消费该值
        captured = {}
        with patch("modules.ai_fallback_client.get_ai_client",
                   side_effect=lambda cfg: captured.setdefault('cfg', dict(cfg)) or MagicMock()):
            from modules.subtitle_qc import _build_openai_client
            _build_openai_client("k", "https://x/v1", "m")
        return saved, reloaded, captured['cfg']

    def test_first_update_does_not_mutate_module_defaults(self):
        self.assertFalse(os.path.exists(os.path.join(self._tmp, 'config', 'config.json')))

        update_config({'SUBTITLE_QC_TIMEOUT_SECONDS': '200'})

        self.assertEqual(cm.DEFAULT_CONFIG['SUBTITLE_QC_TIMEOUT_SECONDS'], 120)

    def test_valid_value_persists_and_is_used(self):
        saved, reloaded, used = self._save_and_trace('200')
        self.assertEqual(saved.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '200')
        self.assertEqual(reloaded.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '200')
        self.assertEqual(used['OPENAI_TIMEOUT_SECONDS'], 200)

    def test_reload_after_restart_reads_persisted_value(self):
        # 第一轮保存，第二轮全新 load（验证已落盘而非仅在内存）
        self._save_and_trace('300')
        fresh = load_config()
        self.assertEqual(fresh.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '300')

    def test_out_of_range_high_clamped_to_default_120(self):
        saved, reloaded, used = self._save_and_trace('999')
        self.assertEqual(saved.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '120')
        self.assertEqual(used['OPENAI_TIMEOUT_SECONDS'], 120)

    def test_negative_clamped_to_default_120(self):
        saved, reloaded, used = self._save_and_trace('-5')
        self.assertEqual(saved.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '120')
        self.assertEqual(used['OPENAI_TIMEOUT_SECONDS'], 120)

    def test_non_numeric_clamped_to_default_120(self):
        saved, reloaded, used = self._save_and_trace('abc')
        self.assertEqual(saved.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '120')
        self.assertEqual(used['OPENAI_TIMEOUT_SECONDS'], 120)

    def test_below_min_clamped_to_default_120(self):
        # 低于下限 10（如 5）→ 回退 120
        saved, reloaded, used = self._save_and_trace('5')
        self.assertEqual(saved.get('SUBTITLE_QC_TIMEOUT_SECONDS'), '120')
        self.assertEqual(used['OPENAI_TIMEOUT_SECONDS'], 120)


if __name__ == '__main__':
    unittest.main()
