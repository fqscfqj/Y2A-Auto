import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

try:
    from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor
except ModuleNotFoundError:
    # 兜底打桩（与仓库其它 monitor 测试一致的轻量 stub 方案）
    if "modules.utils" not in sys.modules:
        modules_utils = types.ModuleType("modules.utils")
        modules_utils.get_app_subdir = lambda subdir_name: os.path.join(os.getcwd(), "temp", "unit-tests", subdir_name)
        sys.modules["modules.utils"] = modules_utils
    if "modules.config_manager" not in sys.modules:
        modules_config_manager = types.ModuleType("modules.config_manager")
        modules_config_manager.load_config = lambda: {}
        sys.modules["modules.config_manager"] = modules_config_manager
    sys.modules.pop("modules.youtube_monitor", None)
    from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor


class YoutubeMonitorQuotaBudgetTests(unittest.TestCase):
    """PR #127 跟进：调度间隔尊重用户配置 + 跨配置共享每日配额预算 + 立即检查不绕过预算。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=self._get_app_subdir,
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor,
            "_init_youtube_api",
            return_value=(False, API_INIT_STATUS_MISSING_API_KEY),
        )
        self.get_app_subdir_patcher.start()
        self.init_api_patcher.start()
        self.monitor = YouTubeMonitor()

    def tearDown(self):
        try:
            scheduler = getattr(self.monitor, "scheduler", None)
            if scheduler and getattr(scheduler, "running", False):
                scheduler.shutdown(wait=False)
        finally:
            self.get_app_subdir_patcher.stop()
            self.init_api_patcher.stop()
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_app_subdir(self, subdir_name):
        path = os.path.join(self.tmpdir, subdir_name)
        os.makedirs(path, exist_ok=True)
        return path

    # ---- 调度间隔：尊重用户配置（修复行为回归） ----

    def test_interval_honors_configured_value(self):
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 120}), 120)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 60}), 60)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 1440}), 1440)

    def test_interval_clamps_to_bounds(self):
        # 超过每日（1440）截断为 1440；低于 1 截断为 1
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 100000}), 1440)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 0}), 1)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": -30}), 1)

    def test_interval_falls_back_to_default_on_invalid(self):
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": "abc"}), 120)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({}), 120)

    # ---- MONITOR_SEARCHES_PER_RUN 严格校验 ----

    def test_searches_per_run_default_and_strict(self):
        with patch("modules.youtube_monitor.load_config", return_value={}):
            self.assertEqual(self.monitor._resolve_monitor_searches_per_run(), 2)

        # 正整数保持不变（但受上限 10 约束）
        with patch("modules.youtube_monitor.load_config", return_value={"MONITOR_SEARCHES_PER_RUN": 5}):
            self.assertEqual(self.monitor._resolve_monitor_searches_per_run(), 5)

        # 超过上限 10 -> 截断为 10
        with patch("modules.youtube_monitor.load_config", return_value={"MONITOR_SEARCHES_PER_RUN": 999}):
            self.assertEqual(self.monitor._resolve_monitor_searches_per_run(), 10)

        # 非正整数 -> 回退 2
        with patch("modules.youtube_monitor.load_config", return_value={"MONITOR_SEARCHES_PER_RUN": 0}):
            self.assertEqual(self.monitor._resolve_monitor_searches_per_run(), 1)  # max(0,1)=1
        with patch("modules.youtube_monitor.load_config", return_value={"MONITOR_SEARCHES_PER_RUN": "oops"}):
            self.assertEqual(self.monitor._resolve_monitor_searches_per_run(), 2)

    # ---- 跨配置共享的每日配额预算 ----

    def test_quota_budget_caps_daily_searches(self):
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        self.assertGreater(budget, 0)
        # 第一次把预算打满
        allowed = self.monitor._consume_quota_budget(budget + 50)
        self.assertEqual(allowed, budget)
        # 之后再请求应被拒绝
        self.assertEqual(self.monitor._consume_quota_budget(10), 0)
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)

    def test_quota_budget_partial_consumption(self):
        self.assertEqual(self.monitor._consume_quota_budget(10), 10)
        self.assertEqual(self.monitor._quota_budget_remaining(), self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 10)
        self.assertEqual(self.monitor._consume_quota_budget(5), 5)

    # ---- 立即检查不绕过预算：仅首次（定时任务不存在时）安排 ----

    def test_start_all_schedules_immediate_only_when_job_absent(self):
        # 创建两个自动调度配置
        c1 = self.monitor.create_monitor_config({"name": "auto-1", "schedule_type": "auto", "enabled": True})
        c2 = self.monitor.create_monitor_config({"name": "auto-2", "schedule_type": "auto", "enabled": True})

        scheduler = self.monitor.scheduler

        def immediate_job_ids():
            return {jid.id for jid in scheduler.get_jobs() if jid.id.startswith("monitor_immediate_")}

        def interval_job_ids():
            return {jid.id for jid in scheduler.get_jobs() if jid.id.startswith("monitor_") and not jid.id.startswith("monitor_immediate_")}

        # 配置创建时已自动排好 interval 任务；模拟「定时任务此前不存在」的场景：
        # 先移除 interval 任务，再启动，验证此时会安排立即检查。
        self.monitor._remove_schedule(c1)
        self.monitor._remove_schedule(c2)
        self.assertEqual(len(interval_job_ids()), 0, "移除后不应有定时任务")

        # 第一次启动：定时任务此前不存在 -> 安排立即检查
        self.monitor.start_all_schedules(immediate=True)
        first_immediate = immediate_job_ids()
        first_interval = interval_job_ids()
        self.assertEqual(len(first_interval), 2, "应有两个定时任务")
        self.assertEqual(len(first_immediate), 2, "首次启动应为两个配置都安排立即检查")

        # 第二次启动（模拟设置保存触发的重启）：定时任务已存在 -> 不应再新增立即检查
        self.monitor.start_all_schedules(immediate=True)
        second_immediate = immediate_job_ids()
        self.assertEqual(second_immediate, first_immediate, "再次启动时不应新增立即检查（避免绕过每日预算）")
        self.assertEqual(len(interval_job_ids()), 2)

        # immediate=False 时也不应新增
        self.monitor.start_all_schedules(immediate=False)
        self.assertEqual(immediate_job_ids(), first_immediate)


if __name__ == "__main__":
    unittest.main()
