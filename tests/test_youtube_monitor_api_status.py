import os
import sys
import types
import unittest
from unittest.mock import patch


def _install_stubs():
    if "modules.utils" not in sys.modules:
        modules_utils = types.ModuleType("modules.utils")

        def _stub_get_app_subdir(subdir_name):
            return os.path.join(os.getcwd(), "temp", "unit-tests", subdir_name)

        modules_utils.get_app_subdir = _stub_get_app_subdir
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
    from modules.youtube_monitor import (
        API_INIT_STATUS_DIRECT_READY,
        API_INIT_STATUS_INIT_FAILED,
        API_INIT_STATUS_MISSING_API_KEY,
        API_INIT_STATUS_PROXY_READY,
        YouTubeMonitor,
        get_api_init_status_message,
    )
except ModuleNotFoundError:
    _install_stubs()
    sys.modules.pop("modules.youtube_monitor", None)
    from modules.youtube_monitor import (
        API_INIT_STATUS_DIRECT_READY,
        API_INIT_STATUS_INIT_FAILED,
        API_INIT_STATUS_MISSING_API_KEY,
        API_INIT_STATUS_PROXY_READY,
        YouTubeMonitor,
        get_api_init_status_message,
    )


class YouTubeMonitorApiStatusTests(unittest.TestCase):
    def _new_monitor_without_init(self) -> YouTubeMonitor:
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        monitor.api_key = None
        monitor.youtube = None
        monitor.youtube_http = None
        monitor._api_proxy_enabled = False
        monitor._last_api_init_error = None
        return monitor

    @patch("modules.youtube_monitor.build", return_value=object())
    def test_init_returns_direct_ready_for_direct_connection(self, mock_build):
        monitor = self._new_monitor_without_init()
        success, status = monitor._init_youtube_api({"YOUTUBE_API_KEY": "key"})

        self.assertTrue(success)
        self.assertEqual(status, API_INIT_STATUS_DIRECT_READY)
        self.assertEqual(get_api_init_status_message(status), "YouTube API 初始化成功，当前为直连模式")
        mock_build.assert_called_once()

    @patch("modules.youtube_monitor.build", return_value=object())
    def test_init_returns_proxy_ready_when_independent_proxy_enabled(self, mock_build):
        monitor = self._new_monitor_without_init()
        success, status = monitor._init_youtube_api({
            "YOUTUBE_API_KEY": "key",
            "YOUTUBE_API_PROXY_ENABLED": True,
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
            "YOUTUBE_API_PROXY_USERNAME": "alice",
            "YOUTUBE_API_PROXY_PASSWORD": "topsecret",
        })

        self.assertTrue(success)
        self.assertEqual(status, API_INIT_STATUS_PROXY_READY)
        self.assertEqual(get_api_init_status_message(status), "YouTube API 初始化成功，独立代理已启用")
        mock_build.assert_called_once()

    @patch("modules.youtube_monitor.build")
    def test_init_failure_returns_fixed_status_without_secret_leakage(self, mock_build):
        mock_build.side_effect = RuntimeError(
            "proxy connect failed for http://alice:topsecret@proxy.example.com:7890"
        )
        monitor = self._new_monitor_without_init()
        success, status = monitor._init_youtube_api({
            "YOUTUBE_API_KEY": "key",
            "YOUTUBE_API_PROXY_ENABLED": True,
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
            "YOUTUBE_API_PROXY_USERNAME": "alice",
            "YOUTUBE_API_PROXY_PASSWORD": "topsecret",
        })

        self.assertFalse(success)
        self.assertEqual(status, API_INIT_STATUS_INIT_FAILED)
        self.assertEqual(monitor._last_api_init_error, API_INIT_STATUS_INIT_FAILED)
        message = get_api_init_status_message(status)
        self.assertNotIn("alice", message)
        self.assertNotIn("topsecret", message)
        self.assertNotIn("proxy.example.com", message)

    def test_missing_api_key_returns_fixed_status(self):
        monitor = self._new_monitor_without_init()
        success, status = monitor._init_youtube_api({})

        self.assertFalse(success)
        self.assertEqual(status, API_INIT_STATUS_MISSING_API_KEY)
        self.assertEqual(monitor._last_api_init_error, API_INIT_STATUS_MISSING_API_KEY)


if __name__ == "__main__":
    unittest.main()
