import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _install_stubs():
    if "modules.utils" not in sys.modules:
        modules_utils = types.ModuleType("modules.utils")
        modules_utils.get_app_subdir = lambda subdir_name: os.path.join(os.getcwd(), "temp", "unit-tests", subdir_name)
        sys.modules["modules.utils"] = modules_utils

    if "modules.config_manager" not in sys.modules:
        modules_config_manager = types.ModuleType("modules.config_manager")
        modules_config_manager.load_config = lambda: {}
        sys.modules["modules.config_manager"] = modules_config_manager

    if "modules.task_manager" not in sys.modules:
        modules_task_manager = types.ModuleType("modules.task_manager")
        modules_task_manager.add_task = lambda *args, **kwargs: None
        sys.modules["modules.task_manager"] = modules_task_manager

    if "httplib2" not in sys.modules:
        httplib2_module = types.ModuleType("httplib2")

        class _StubHttpLib2Error(Exception):
            pass

        class _StubHttp:
            def __init__(self, timeout=None, proxy_info=None):
                self.timeout = timeout
                self.proxy_info = proxy_info

        httplib2_module.Http = _StubHttp
        httplib2_module.HttpLib2Error = _StubHttpLib2Error
        httplib2_module.proxy_info_from_url = lambda url, method=None: {"url": url, "method": method}
        sys.modules["httplib2"] = httplib2_module

    if "apscheduler.schedulers.background" not in sys.modules:
        apscheduler_module = types.ModuleType("apscheduler")
        apscheduler_schedulers_module = types.ModuleType("apscheduler.schedulers")
        apscheduler_background_module = types.ModuleType("apscheduler.schedulers.background")

        class _StubBackgroundScheduler:
            def __init__(self, *args, **kwargs):
                self.running = False
                self._jobs = {}

            def add_job(self, func=None, trigger=None, minutes=None, id=None, args=None, replace_existing=False):
                if id is not None:
                    self._jobs[id] = {
                        "func": func,
                        "trigger": trigger,
                        "minutes": minutes,
                        "args": args or [],
                    }

            def get_job(self, job_id):
                return self._jobs.get(job_id)

            def remove_job(self, job_id):
                self._jobs.pop(job_id, None)

            def start(self):
                self.running = True

            def shutdown(self, *args, **kwargs):
                self.running = False

        apscheduler_background_module.BackgroundScheduler = _StubBackgroundScheduler
        sys.modules["apscheduler"] = apscheduler_module
        sys.modules["apscheduler.schedulers"] = apscheduler_schedulers_module
        sys.modules["apscheduler.schedulers.background"] = apscheduler_background_module

    if "googleapiclient.discovery" not in sys.modules:
        googleapiclient_module = types.ModuleType("googleapiclient")
        discovery_module = types.ModuleType("googleapiclient.discovery")
        errors_module = types.ModuleType("googleapiclient.errors")
        http_module = types.ModuleType("googleapiclient.http")

        class _StubHttpError(Exception):
            pass

        discovery_module.build = lambda *args, **kwargs: object()
        errors_module.HttpError = _StubHttpError
        http_module.DEFAULT_HTTP_TIMEOUT_SEC = 120

        sys.modules["googleapiclient"] = googleapiclient_module
        sys.modules["googleapiclient.discovery"] = discovery_module
        sys.modules["googleapiclient.errors"] = errors_module
        sys.modules["googleapiclient.http"] = http_module


try:
    from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor
except ModuleNotFoundError:
    _install_stubs()
    sys.modules.pop("modules.youtube_monitor", None)
    from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor


class YouTubeMonitorConfigSqlDedupTests(unittest.TestCase):
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

    def test_create_monitor_config_persists_shared_defaults(self):
        config_id = self.monitor.create_monitor_config({"name": "create-defaults"})

        config = self.monitor.get_monitor_config(config_id)

        self.assertEqual(config["name"], "create-defaults")
        self.assertEqual(config["monitor_type"], "youtube_search")
        self.assertEqual(config["channel_mode"], "latest")
        self.assertEqual(config["schedule_interval"], 120)
        self.assertEqual(config["rate_limit_requests"], 100)
        self.assertEqual(config["video_types"], "video,short,live")

    def test_update_monitor_config_preserves_existing_offset_and_video_types_when_omitted(self):
        config_id = self.monitor.create_monitor_config({
            "name": "historical-original",
            "channel_ids": "channel-1",
            "channel_mode": "historical",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "historical_offset": 5,
            "video_types": "live",
        })

        self.monitor.update_monitor_config(config_id, {
            "name": "historical-updated",
            "channel_ids": "channel-1",
            "channel_mode": "historical",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        })

        config = self.monitor.get_monitor_config(config_id)

        self.assertEqual(config["name"], "historical-updated")
        self.assertEqual(config["historical_offset"], 5)
        self.assertEqual(config["video_types"], "live")

    def test_restore_single_config_reuses_target_id_and_defaults(self):
        restored_id = self.monitor._restore_single_config({"name": "restored-config"}, 31)

        config = self.monitor.get_monitor_config(restored_id)

        self.assertEqual(restored_id, 31)
        self.assertEqual(config["name"], "restored-config")
        self.assertEqual(config["schedule_type"], "manual")
        self.assertEqual(config["rate_limit_requests"], 100)
        self.assertEqual(config["video_types"], "video,short,live")


if __name__ == "__main__":
    unittest.main()
