import datetime
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
        # 超过 30 天（43200 分钟）截断为 43200；低于 1 截断为 1
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 100000}), 43200)
        self.assertEqual(self.monitor._resolve_schedule_interval_minutes({"schedule_interval": 43200}), 43200)
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


class QuotaPersistenceAndAtomicTests(unittest.TestCase):
    """PR #127 第三轮：预算 SQLite 持久化 + PT 配额日 + 跨实例可见 + 原子扣减。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=lambda sub: (os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
                                     or os.path.join(self.tmpdir, sub)),
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor, "_init_youtube_api",
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

    def test_quota_day_is_pt_aligned_date_string(self):
        day = self.monitor._quota_day_str()
        # YYYY-MM-DD 且为合法日期（洛杉矶本地日期）
        self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        datetime.datetime.strptime(day, "%Y-%m-%d")

    def test_quota_budget_persists_across_instances(self):
        # 扣减后重建实例（模拟进程重启），剩余量应保留而非归零
        self.assertEqual(self.monitor._consume_quota_budget(10), 10)
        m2 = YouTubeMonitor()
        self.assertEqual(
            m2._quota_budget_remaining(),
            self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 10,
        )
        # 新实例继续扣减同一份持久化预算
        self.assertEqual(m2._consume_quota_budget(5), 5)
        self.assertEqual(self.monitor._quota_budget_remaining(),
                         self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 15)

    def test_consume_budget_atomic_cap(self):
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        # 第一次打满
        self.assertEqual(self.monitor._consume_quota_budget(budget + 50), budget)
        # 之后再扣 0
        self.assertEqual(self.monitor._consume_quota_budget(1), 0)
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)

    def test_try_consume_quota_shared_budget_cap(self):
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        cid = self.monitor.create_monitor_config(
            {"name": "auto", "schedule_type": "auto", "enabled": True})
        for _ in range(budget):
            self.assertTrue(self.monitor._try_consume_quota(cid, 1))
        self.assertFalse(self.monitor._try_consume_quota(cid, 1))


class ExecuteWithRetryQuotaTests(unittest.TestCase):
    """PR #127 第三轮：每次实际 attempt 原子扣减 + 429 不重试 + 预算耗尽即止。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=lambda sub: (os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
                                     or os.path.join(self.tmpdir, sub)),
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor, "_init_youtube_api",
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

    class _FakeRequest:
        def __init__(self, behavior):
            self._behavior = behavior  # list of outcomes

        def execute(self):
            outcome = self._behavior.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    def test_retries_deduct_budget_per_attempt(self):
        import ssl
        from modules.youtube_monitor import QuotaBudgetExhausted
        req = self._FakeRequest([ssl.SSLError("boom"), ssl.SSLError("boom"), {"ok": True}])
        calls = []

        def consumer():
            calls.append(1)
            return True

        result = self.monitor._execute_with_retry(
            req, "search.list", max_attempts=3, backoff_seconds=0.01,
            quota_consumer=consumer)
        self.assertEqual(result, {"ok": True})
        # 3 次 attempt 各扣一次 → 重试没有绕过预算
        self.assertEqual(len(calls), 3)

    def test_budget_exhausted_stops_without_executing(self):
        from modules.youtube_monitor import QuotaBudgetExhausted
        req = self._FakeRequest([])  # execute 不应被调用
        calls = []

        def consumer():
            calls.append(1)
            return False

        with self.assertRaises(QuotaBudgetExhausted):
            self.monitor._execute_with_retry(
                req, "search.list", quota_consumer=consumer)
        self.assertEqual(len(calls), 1)

    def test_429_raises_without_retry(self):
        from googleapiclient.errors import HttpError
        from modules.youtube_monitor import QuotaBudgetExhausted

        class _Resp:
            status = 429
            reason = "quotaExceeded"

        err = HttpError(_Resp(), b"{}", uri="http://x")
        req = self._FakeRequest([err, {"should-not-happen": True}])
        calls = []

        def consumer():
            calls.append(1)
            return True

        with self.assertRaises(HttpError):
            self.monitor._execute_with_retry(
                req, "search.list", max_attempts=3, backoff_seconds=0.01,
                quota_consumer=consumer)
        # 429 当日跳过：只扣 1 次、不重试
        self.assertEqual(len(calls), 1)


class KeywordRotationCursorTests(unittest.TestCase):
    """PR #127 第三轮：关键词轮转游标持久化、每轮推进。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=lambda sub: (os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
                                     or os.path.join(self.tmpdir, sub)),
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor, "_init_youtube_api",
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

    def test_cursor_advances_each_round_and_persists(self):
        # 10 个关键词、每轮 2 个：0 → 2 → 4 → ...（每轮推进，而非按天固定）
        self.assertEqual(self.monitor._next_keyword_rotation_start(1, 10, 2), 0)
        self.assertEqual(self.monitor._next_keyword_rotation_start(1, 10, 2), 2)
        self.assertEqual(self.monitor._next_keyword_rotation_start(1, 10, 2), 4)
        # 新实例（模拟重启）读到同一游标，继续推进
        m2 = YouTubeMonitor()
        self.assertEqual(m2._next_keyword_rotation_start(1, 10, 2), 6)

    def test_cursor_wraps_around(self):
        # 2 个关键词、每轮 2 个：0 → 0（覆盖全部后回到开头）
        self.assertEqual(self.monitor._next_keyword_rotation_start(9, 2, 2), 0)
        self.assertEqual(self.monitor._next_keyword_rotation_start(9, 2, 2), 0)


class DisableCancelsImmediateTests(unittest.TestCase):
    """PR #127 第三轮：禁用配置取消立即任务 + run_monitor 入口复核状态。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=lambda sub: (os.makedirs(os.path.join(self.tmpdir, sub), exist_ok=True)
                                     or os.path.join(self.tmpdir, sub)),
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor, "_init_youtube_api",
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

    def test_remove_schedule_cancels_immediate_job(self):
        cid = self.monitor.create_monitor_config(
            {"name": "auto", "schedule_type": "auto", "enabled": True})
        # 先移除 interval，再启动 → 模拟首次启动，立即任务被安排
        self.monitor._remove_schedule(cid)
        self.monitor.start_all_schedules(immediate=True)
        immediate_id = f"monitor_immediate_{cid}"
        interval_id = f"monitor_{cid}"
        self.assertIsNotNone(self.monitor.scheduler.get_job(immediate_id))
        self.assertIsNotNone(self.monitor.scheduler.get_job(interval_id))
        # 禁用/删除时 _remove_schedule 必须同时取消立即任务
        self.monitor._remove_schedule(cid)
        self.assertIsNone(self.monitor.scheduler.get_job(immediate_id))
        self.assertIsNone(self.monitor.scheduler.get_job(interval_id))

    def test_run_monitor_skips_when_config_disabled(self):
        # 让 API 对象存在以绕过 init 检查，走到 enabled 复核
        self.monitor.youtube = object()
        cid = self.monitor.create_monitor_config(
            {"name": "disabled", "schedule_type": "manual", "enabled": False})
        with patch.object(self.monitor, "_fetch_trending_videos",
                          side_effect=AssertionError("禁用配置不应抓取")):
            ok, msg = self.monitor.run_monitor(cid)
        self.assertFalse(ok)
        self.assertIn("已禁用", msg)


if __name__ == "__main__":
    unittest.main()
