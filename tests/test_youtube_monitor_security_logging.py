import os
import sys
import types
import unittest
from unittest.mock import patch


def _install_lightweight_stubs_for_optional_deps():
    if "modules.utils" not in sys.modules:
        modules_utils = types.ModuleType("modules.utils")

        def _stub_get_app_subdir(subdir_name):
            return os.path.join(os.getcwd(), subdir_name)

        modules_utils.get_app_subdir = _stub_get_app_subdir
        sys.modules["modules.utils"] = modules_utils

    if "modules.config_manager" not in sys.modules:
        modules_config_manager = types.ModuleType("modules.config_manager")

        def _stub_load_config():
            return {}

        modules_config_manager.load_config = _stub_load_config
        sys.modules["modules.config_manager"] = modules_config_manager

    if "modules.task_manager" not in sys.modules:
        modules_task_manager = types.ModuleType("modules.task_manager")

        def _stub_add_task(*args, **kwargs):
            return None

        modules_task_manager.add_task = _stub_add_task
        sys.modules["modules.task_manager"] = modules_task_manager

    if "httplib2" not in sys.modules:
        httplib2_module = types.ModuleType("httplib2")

        class _StubHttpLib2Error(Exception):
            pass

        class _StubHttp:
            def __init__(self, timeout=None, proxy_info=None):
                self.timeout = timeout
                self.proxy_info = proxy_info

        def _stub_proxy_info_from_url(url, method=None):
            return {"url": url, "method": method}

        httplib2_module.Http = _StubHttp
        httplib2_module.HttpLib2Error = _StubHttpLib2Error
        httplib2_module.proxy_info_from_url = _stub_proxy_info_from_url
        sys.modules["httplib2"] = httplib2_module

    if "apscheduler.schedulers.background" not in sys.modules:
        apscheduler_module = types.ModuleType("apscheduler")
        apscheduler_schedulers_module = types.ModuleType("apscheduler.schedulers")
        apscheduler_background_module = types.ModuleType("apscheduler.schedulers.background")
        apscheduler_executors_module = types.ModuleType("apscheduler.executors")
        apscheduler_pool_module = types.ModuleType("apscheduler.executors.pool")
        apscheduler_base_module = types.ModuleType("apscheduler.schedulers.base")

        class _StubBackgroundScheduler:
            def __init__(self, *args, **kwargs):
                self.running = False

            def start(self):
                self.running = True

            def shutdown(self, *args, **kwargs):
                self.running = False

        class _StubThreadPoolExecutor:
            def __init__(self, *args, **kwargs):
                pass

        class _StubSchedulerNotRunningError(Exception):
            pass

        apscheduler_background_module.BackgroundScheduler = _StubBackgroundScheduler
        apscheduler_pool_module.ThreadPoolExecutor = _StubThreadPoolExecutor
        apscheduler_base_module.SchedulerNotRunningError = _StubSchedulerNotRunningError

        sys.modules["apscheduler"] = apscheduler_module
        sys.modules["apscheduler.schedulers"] = apscheduler_schedulers_module
        sys.modules["apscheduler.schedulers.background"] = apscheduler_background_module
        sys.modules["apscheduler.executors"] = apscheduler_executors_module
        sys.modules["apscheduler.executors.pool"] = apscheduler_pool_module
        sys.modules["apscheduler.schedulers.base"] = apscheduler_base_module

    if "googleapiclient.discovery" not in sys.modules:
        googleapiclient_module = types.ModuleType("googleapiclient")
        discovery_module = types.ModuleType("googleapiclient.discovery")
        errors_module = types.ModuleType("googleapiclient.errors")
        http_module = types.ModuleType("googleapiclient.http")

        def _stub_build(*args, **kwargs):
            return object()

        class _StubHttpError(Exception):
            pass

        discovery_module.build = _stub_build
        errors_module.HttpError = _StubHttpError
        http_module.DEFAULT_HTTP_TIMEOUT_SEC = 120

        sys.modules["googleapiclient"] = googleapiclient_module
        sys.modules["googleapiclient.discovery"] = discovery_module
        sys.modules["googleapiclient.errors"] = errors_module
        sys.modules["googleapiclient.http"] = http_module


try:
    from modules.youtube_monitor import YouTubeMonitor
except ModuleNotFoundError:
    _install_lightweight_stubs_for_optional_deps()
    sys.modules.pop("modules.youtube_monitor", None)
    from modules.youtube_monitor import YouTubeMonitor


class YouTubeMonitorSecurityLoggingTests(unittest.TestCase):
    def _new_monitor_without_init(self) -> YouTubeMonitor:
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        monitor.api_key = None
        monitor.youtube = None
        monitor.youtube_http = None
        monitor._api_proxy_enabled = False
        monitor._api_proxy_url = None
        monitor._api_proxy_display_url = None
        monitor._last_api_init_error = None
        return monitor

    def test_describe_network_mode_uses_proxy_host_and_port_only(self):
        monitor = self._new_monitor_without_init()
        runtime_config = {
            "YOUTUBE_API_PROXY_ENABLED": True,
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
            "YOUTUBE_API_PROXY_USERNAME": "alice",
            "YOUTUBE_API_PROXY_PASSWORD": "topsecret",
        }

        monitor._build_youtube_http(runtime_config)
        mode = monitor._describe_api_network_mode()

        self.assertEqual(mode, "代理 http://proxy.example.com:7890")
        self.assertNotIn("alice", mode)
        self.assertNotIn("topsecret", mode)

    @patch("modules.youtube_monitor.build")
    def test_init_error_does_not_leak_credentials(self, mock_build):
        mock_build.side_effect = RuntimeError(
            "proxy connect failed for http://alice:topsecret@proxy.example.com:7890"
        )
        monitor = self._new_monitor_without_init()
        runtime_config = {
            "YOUTUBE_API_KEY": "yt-api-key",
            "YOUTUBE_API_PROXY_ENABLED": True,
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
            "YOUTUBE_API_PROXY_USERNAME": "alice",
            "YOUTUBE_API_PROXY_PASSWORD": "topsecret",
        }

        success, detail = monitor._init_youtube_api(runtime_config)
        self.assertFalse(success)
        self.assertIn("RuntimeError", detail)
        self.assertNotIn("alice", detail)
        self.assertNotIn("topsecret", detail)
        self.assertNotIn("topsecret", monitor._last_api_init_error or "")


if __name__ == "__main__":
    unittest.main()

