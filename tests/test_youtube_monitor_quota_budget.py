import datetime
import os
import sqlite3
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
        # 每次 search 只扣 1 次（真实用法）：逐次扣满后，下一次拒绝。
        for _ in range(budget):
            self.assertTrue(self.monitor._try_consume_quota(1))
        # 预算用尽后再次请求应被拒绝
        self.assertFalse(self.monitor._try_consume_quota(1))
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)

    def test_quota_budget_partial_consumption(self):
        for _ in range(10):
            self.assertTrue(self.monitor._try_consume_quota(1))
        self.assertEqual(self.monitor._quota_budget_remaining(), self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 10)
        for _ in range(5):
            self.assertTrue(self.monitor._try_consume_quota(1))

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
        for _ in range(10):
            self.assertTrue(self.monitor._try_consume_quota(1))
        m2 = YouTubeMonitor()
        self.assertEqual(
            m2._quota_budget_remaining(),
            self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 10,
        )
        # 新实例继续扣减同一份持久化预算
        for _ in range(5):
            self.assertTrue(m2._try_consume_quota(1))
        self.assertEqual(self.monitor._quota_budget_remaining(),
                         self.monitor._MONITOR_DAILY_SEARCH_BUDGET - 15)

    def test_consume_budget_atomic_cap(self):
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        # 逐次扣满
        for _ in range(budget):
            self.assertTrue(self.monitor._try_consume_quota(1))
        # 之后再扣被拒绝
        self.assertFalse(self.monitor._try_consume_quota(1))
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)

    def test_try_consume_quota_shared_budget_cap(self):
        # 单一全局令牌桶：搜索型配置共享同一天额度，不受配置数量/类型影响
        # （不再按「每配置份额」分配，故 10 个 playlist + 1 个搜索时，搜索配置
        #  仍能用满 95 次，而非旧模型被分母虚大压到 8 次而过度限流）。
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        for _ in range(budget):
            self.assertTrue(self.monitor._try_consume_quota(1))
        self.assertFalse(self.monitor._try_consume_quota(1))


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

    def test_429_triggers_day_circuit_breaker(self):
        from googleapiclient.errors import HttpError
        from modules.youtube_monitor import QuotaBudgetExhausted

        class _Resp:
            status = 429
            reason = "quotaExceeded"

        err = HttpError(_Resp(), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}', uri="http://x")
        err.error_details = [{"reason": "quotaExceeded"}]  # 模拟 googleapiclient 从响应解析
        req = self._FakeRequest([err, {"should-not-happen": True}])
        calls = []

        def consumer():
            calls.append(1)
            return True  # 预算充足，但远端 429 仍应触发当日熔断

        with self.assertRaises(QuotaBudgetExhausted):
            self.monitor._execute_with_retry(
                req, "search.list", max_attempts=3, backoff_seconds=0.01,
                quota_consumer=consumer, operation_type='search')
        # 429 + 当日配额耗尽 reason + search 操作 → 集中式当日熔断：
        # 只发 1 次请求、不重试；并标记当日耗尽（全局桶置满）。
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.monitor._last_fetch_quota_skipped)
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)

    def test_rate_limit_reason_retries_without_breaker(self):
        # 短时速率限制（rateLimitExceeded）仅重试，不触发当日熔断。
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 429
            reason = "rateLimitExceeded"

        err = HttpError(_Resp(), b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}', uri="http://x")
        err.error_details = [{"reason": "rateLimitExceeded"}]
        req = self._FakeRequest([err, err, {"ok": True}])
        calls = []

        def consumer():
            calls.append(1)
            return True

        # search 操作 + rateLimitExceeded：应重试并最终成功，且不熔断
        result = self.monitor._execute_with_retry(
            req, "search.list", max_attempts=3, backoff_seconds=0.001,
            quota_consumer=consumer, operation_type='search')
        self.assertEqual(result, {"ok": True})
        self.assertFalse(self.monitor._last_fetch_quota_skipped)
        self.assertGreater(self.monitor._quota_budget_remaining(), 0)

    def test_non_search_daily_quota_does_not_trip_breaker(self):
        # 非 search 操作（videos.list 等）即便触发配额错误，也不熔断 search 预算。
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 429
            reason = "quotaExceeded"

        err = HttpError(_Resp(), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}', uri="http://x")
        err.error_details = [{"reason": "quotaExceeded"}]
        req = self._FakeRequest([err])
        calls = []

        def consumer():
            calls.append(1)
            return True

        with self.assertRaises(HttpError):
            self.monitor._execute_with_retry(
                req, "videos.list", max_attempts=1, backoff_seconds=0.001,
                quota_consumer=consumer, operation_type='other')
        # 非 search 配额错误不熔断：breaker 标志未置、search 预算未受影响
        self.assertFalse(self.monitor._last_fetch_quota_skipped)
        self.assertEqual(self.monitor._quota_budget_remaining(), self.monitor._MONITOR_DAILY_SEARCH_BUDGET)

    def test_http_error_quota_reason_parsing(self):
        from googleapiclient.errors import HttpError

        # 生产形态：error_details 由 googleapiclient 从响应 JSON 解析而来
        class _Resp:
            status = 403
            reason = "dailyLimitExceeded"

        e1 = HttpError(_Resp(), b'x', uri="http://x")
        e1.error_details = [{"reason": "dailyLimitExceeded"}]
        self.assertEqual(self.monitor._http_error_quota_reason(e1), "dailyLimitExceeded")

        e2 = HttpError(_Resp(), b'x', uri="http://x")
        e2.error_details = [{"reason": "quotaExceeded"}]
        self.assertEqual(self.monitor._http_error_quota_reason(e2), "quotaExceeded")

        # 退化路径：error_details 为空/缺失，从错误文本抓 reason
        class _FakeErr(Exception):
            def __init__(self, error_details, text):
                self.error_details = error_details
                self.args = (text,)

            def __str__(self):
                return self.args[0]

        fe = _FakeErr(None, 'something reason: rateLimitExceeded happened')
        self.assertEqual(self.monitor._http_error_quota_reason(fe), "rateLimitExceeded")

        # error_details 无 reason 字段时也走文本退化
        fe2 = _FakeErr([{"message": "quota exceeded"}], 'error reason: quotaExceeded')
        self.assertEqual(self.monitor._http_error_quota_reason(fe2), "quotaExceeded")

    def test_per_config_fair_share_caps_single_config(self):
        # 每配置公平份额：N 个搜索配置时每个上限 = budget // N（最少 1）。
        # 单一高频配置无法吃光全局桶，从而不会饿死其它配置。
        budget = self.monitor._MONITOR_DAILY_SEARCH_BUDGET
        c1 = self.monitor.create_monitor_config(
            {"name": "auto-1", "schedule_type": "auto", "enabled": True, "keywords": "a,b"})
        c2 = self.monitor.create_monitor_config(
            {"name": "auto-2", "schedule_type": "auto", "enabled": True, "keywords": "x,y"})
        # 仅 2 个搜索配置 → 份额 = budget // 2
        share = budget // 2
        # c1 可消费到自己的份额
        for _ in range(share):
            self.assertTrue(self.monitor._try_consume_quota(1, c1))
        # 超过 c1 份额后，c1 被拒（但全局桶仍有余量，让给 c2）
        self.assertFalse(self.monitor._try_consume_quota(1, c1))
        # c2 仍能消费自己的份额（未被 c1 饿死）
        for _ in range(share):
            self.assertTrue(self.monitor._try_consume_quota(1, c2))
        # 两份份额用尽后，全局桶约剩 budget - 2*share（可能为 0 或少量），任何配置都拒
        self.assertFalse(self.monitor._try_consume_quota(1, c1))
        self.assertFalse(self.monitor._try_consume_quota(1, c2))
        # 总消耗不超过 budget
        self.assertLessEqual(budget - self.monitor._quota_budget_remaining(), budget)

    def test_circuit_breaker_fail_closed_on_persist_failure(self):
        # 持久化失败（如磁盘/进程异常）时绝不能静默放过：
        # 先置内存标志（fail-closed），再对 DB 写入重试；本进程仍停止发请求。
        self.monitor._quota_tables_ready = True  # 跳过 reset_tables，集中测试 INSERT 失败路径
        real_connect = sqlite3.connect

        def _boom(*a, **k):
            raise sqlite3.OperationalError("disk full")

        with patch("sqlite3.connect", side_effect=_boom):
            self.monitor._mark_quota_depleted_today()
        # 内存 fail-closed 标志已设置，且当日剩余为 0（不会提前释放额度）
        self.assertEqual(self.monitor._quota_depleted_day, self.monitor._quota_day_str())
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)
        # 退出 patch 后（connect 恢复），本进程基于内存标志仍然拒绝新请求
        self.assertFalse(self.monitor._try_consume_quota(1, 1))

    def test_circuit_breaker_blocks_subsequent_searches(self):
        # 当日熔断后，任何 search 入口的 _try_consume_quota / 前置检查都应短路，
        # 解决旧实现「第一个关键词 429 后第二个仍真实发请求」的缺陷。
        self.monitor._mark_quota_depleted_today()
        self.assertEqual(self.monitor._quota_budget_remaining(), 0)
        self.assertFalse(self.monitor._try_consume_quota(1))
        # _fetch_search_videos / _fetch_channel_search_videos 的前置检查也会跳过
        # （依赖 _quota_budget_remaining() <= 0），无需再发请求。


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


class SearchConsumerPredicateTests(unittest.TestCase):
    """PR #127 第五轮：搜索消费者谓词修复（分母覆盖真实 search 消费者）。"""

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

    def test_is_search_consumer_matches_dispatch(self):
        # 分流谓词：无 channel_ids -> 走 search.list（是消费者）
        self.assertTrue(self.monitor._is_search_consumer(
            {"enabled": True, "keywords": "a", "channel_mode": "latest"}))
        # 有 channel_ids + channel_mode=='search' -> 走频道内 search.list（是消费者）
        self.assertTrue(self.monitor._is_search_consumer(
            {"enabled": True, "channel_ids": "UC123", "channel_mode": "search"}))
        # 有 channel_ids + channel_mode='latest' -> 走 playlist（非 search 消费者）
        self.assertFalse(self.monitor._is_search_consumer(
            {"enabled": True, "channel_ids": "UC123", "channel_mode": "latest"}))
        # 有 channel_ids + channel_mode='historical' -> 走 playlist（非 search 消费者）
        self.assertFalse(self.monitor._is_search_consumer(
            {"enabled": True, "channel_ids": "UC123", "channel_mode": "historical"}))
        # 未启用 -> 不是消费者（无论何种模式）
        self.assertFalse(self.monitor._is_search_consumer(
            {"enabled": False, "keywords": "a"}))

    def test_search_config_count_uses_correct_denominator(self):
        # 建 2 个「频道搜索」配置（channel_ids + channel_mode='search'）：
        # 旧实现用错误字段 channel_id（单数）会漏掉它们，导致 count=0、份额=95、互相饿死。
        c1 = self.monitor.create_monitor_config(
            {"name": "ch1", "schedule_type": "auto", "enabled": True,
             "channel_ids": "UC123", "channel_mode": "search"})
        c2 = self.monitor.create_monitor_config(
            {"name": "ch2", "schedule_type": "auto", "enabled": True,
             "channel_ids": "UC456", "channel_mode": "search"})
        # 另加一个 playlist 频道配置（latest），不应计入分母
        self.monitor.create_monitor_config(
            {"name": "pl", "schedule_type": "auto", "enabled": True,
             "channel_ids": "UC789", "channel_mode": "latest"})
        # 另加停用配置，不应计入
        self.monitor.create_monitor_config(
            {"name": "off", "schedule_type": "auto", "enabled": False,
             "keywords": "z"})
        count = self.monitor._monitor_search_config_count()
        self.assertEqual(count, 2, "2 个频道搜索配置应被计入，其余不计入")
        # 份额 = budget // 2，而非 95（旧实现会算成 95）
        share = self.monitor._per_config_share()
        self.assertEqual(share, self.monitor._MONITOR_DAILY_SEARCH_BUDGET // 2)

    def test_manual_search_config_counted_to_avoid_starvation(self):
        # manual 关键词配置同样消费 search，若不计入分母会被手动运行抢满 95 次。
        c_auto = self.monitor.create_monitor_config(
            {"name": "auto", "schedule_type": "auto", "enabled": True, "keywords": "a"})
        c_manual = self.monitor.create_monitor_config(
            {"name": "manual", "schedule_type": "manual", "enabled": True, "keywords": "b"})
        count = self.monitor._monitor_search_config_count()
        self.assertEqual(count, 2)
        share = self.monitor._per_config_share()
        # manual 与 auto 各获 budget//2，auto 不会被 manual 抢空
        for _ in range(share):
            self.assertTrue(self.monitor._try_consume_quota(1, c_manual))
        self.assertFalse(self.monitor._try_consume_quota(1, c_manual))
        # auto 仍有自己的份额
        for _ in range(share):
            self.assertTrue(self.monitor._try_consume_quota(1, c_auto))


class HttpErrorReasonTypeSafeTests(unittest.TestCase):
    """PR #127 第五轮：_http_error_quota_reason 对 error_details 形态健壮。"""

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

    def test_reason_from_list_of_dicts(self):
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 403
            reason = "Forbidden"

        e = HttpError(_Resp(), b'x', uri="http://x")
        e.error_details = [{"reason": "quotaExceeded"}]
        self.assertEqual(self.monitor._http_error_quota_reason(e), "quotaExceeded")

    def test_reason_from_string_error_details_does_not_crash(self):
        # 仅 error.message 时 googleapiclient 会把 error_details 设为字符串：
        # 旧实现 `for d in details: (d or {}).get(...)` 会遍历字符并 AttributeError。
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 500
            reason = "Server Error"

        e = HttpError(_Resp(), b'{"error":{"code":500,"message":"backend unavailable"}}', uri="http://x")
        # error_details 是字符串（仅 message），不应崩溃，且没有 quota reason -> None
        self.assertIsNone(self.monitor._http_error_quota_reason(e))

    def test_reason_from_string_json_with_reason(self):
        # error_details 是字符串形式的 JSON，含 errors[] -> 应能取出 reason
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 403
            reason = "Forbidden"

        e = HttpError(_Resp(), b'x', uri="http://x")
        e.error_details = '{"errors":[{"reason":"dailyLimitExceeded"}]}'
        self.assertEqual(self.monitor._http_error_quota_reason(e), "dailyLimitExceeded")

    def test_reason_from_dict_error_details(self):
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 403
            reason = "Forbidden"

        e = HttpError(_Resp(), b'x', uri="http://x")
        e.error_details = {"errors": [{"reason": "rateLimitExceeded"}]}
        self.assertEqual(self.monitor._http_error_quota_reason(e), "rateLimitExceeded")


class CursorShareGateTests(unittest.TestCase):
    """PR #127 第五轮：本配置份额耗尽时不应空推进游标。"""

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

    def test_config_share_remaining_false_blocks_cursor_advance(self):
        # 全局桶仍有余额，但本配置份额已耗尽：不能空推进游标。
        # 需要 ≥2 个 search 消费者，这样耗尽单个配置份额后全局桶仍有余额（share < budget）。
        c1 = self.monitor.create_monitor_config(
            {"name": "kw1", "schedule_type": "auto", "enabled": True, "keywords": "a,b,c,d"})
        self.monitor.create_monitor_config(
            {"name": "kw2", "schedule_type": "auto", "enabled": True, "keywords": "x,y"})
        share = self.monitor._per_config_share()  # budget // 2
        # 先耗尽 c1 的份额
        for _ in range(share):
            self.assertTrue(self.monitor._try_consume_quota(1, c1))
        self.assertFalse(self.monitor._try_consume_quota(1, c1))
        # 此时全局仍有余额（2*share <= budget，通常 < budget），但 c1 份额为 0
        self.assertGreater(self.monitor._quota_budget_remaining(), 0)
        self.assertFalse(self.monitor._config_share_remaining(c1))
        # 关键断言：即便高频调度再次进入 _fetch_search_videos，也应在「发起请求前」
        # 的份额闸门处短路返回，不调用搜索、不推进游标。
        self.monitor.youtube = object()  # 绕过 init 检查，走到份额闸门
        result = self.monitor._fetch_search_videos(
            {"id": c1, "name": "kw1", "enabled": True, "keywords": "a,b,c,d", "max_results": 10,
             "region_code": "US", "order_by": "viewCount", "time_period": 7, "video_types": "video",
             "category_id": ""},
            "2026-08-22T00:00")
        self.assertEqual(result, [])
        # 游标未被推进：仍停留在初始 0（未被推进到 2）——证明份额耗尽时不空转游标
        self.assertEqual(self.monitor._next_keyword_rotation_start(c1, 4, 2), 0)
        self.assertTrue(self.monitor._last_fetch_quota_skipped)


if __name__ == "__main__":
    unittest.main()
