"""回归测试：下载日志不得回显携带代理凭据的 yt-dlp 命令。

对应 CodeQL 告警 #86 / #87（py/clear-text-logging-sensitive-data）：
`download_video_data` 曾经把完整命令（含 `--proxy` 里的明文用户名/密码）写进日志，
并且 `subprocess.CalledProcessError` 的字符串形式同样携带完整命令。

这里用 Mock 记录日志调用而不是真实 logger：同目录的
`test_speech_pipeline_wiring.py` 会调用 `logging.disable(logging.CRITICAL)`，
依赖全局 logging 状态会让本测试变得依赖执行顺序。
"""

import contextlib
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from modules import youtube_handler

PROXY_USERNAME = "unit-test-proxy-user"
PROXY_PASSWORD = "unit-test-proxy-password"
PROXY_URL = "http://proxy.example.com:7890"
PROXY_URL_WITH_AUTH = f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@proxy.example.com:7890"
WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

_LOG_METHODS = ("debug", "info", "warning", "error", "exception", "critical")


def _proxy_config():
    return {
        'YOUTUBE_PROXY_ENABLED': True,
        'YOUTUBE_PROXY_URL': PROXY_URL,
        'YOUTUBE_PROXY_USERNAME': PROXY_USERNAME,
        'YOUTUBE_PROXY_PASSWORD': PROXY_PASSWORD,
    }


def _render_log_call(call):
    """把一次 logger 调用还原成接近真实日志的一行文本。"""
    parts = []
    if call.args:
        template, values = call.args[0], call.args[1:]
        rendered = None
        if isinstance(template, str) and values:
            try:
                rendered = template % tuple(values)
            except (TypeError, ValueError):
                rendered = None
        parts.append(rendered if rendered is not None else " ".join(str(arg) for arg in call.args))
    if call.kwargs:
        parts.append(repr(call.kwargs))
    return " ".join(parts)


class _FakeProcess:
    """最小可用的 Popen 替身，让带进度回调的下载分支能够走通。"""

    def __init__(self, lines=(), returncode=0):
        self.pid = 4321
        self.returncode = returncode
        self.stdout = iter(lines)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15


class DownloadLogRedactionTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.logger = mock.Mock(name='task_logger')

    def logs(self):
        chunks = []
        for method_name in _LOG_METHODS:
            for call in getattr(self.logger, method_name).call_args_list:
                chunks.append(_render_log_call(call))
        return "\n".join(chunks)

    def assert_no_credentials_in_logs(self):
        logs = self.logs()
        self.assertNotIn(PROXY_PASSWORD, logs)
        self.assertNotIn(PROXY_USERNAME, logs)
        return logs

    def _base_patches(self):
        return [
            mock.patch.object(youtube_handler, 'load_config', return_value=_proxy_config()),
            mock.patch.object(
                youtube_handler,
                'get_app_subdir',
                side_effect=lambda name: str(pathlib.Path(self._temp_dir.name) / name),
            ),
            mock.patch.object(youtube_handler, 'setup_task_logger', return_value=self.logger),
            mock.patch.object(youtube_handler, '_find_yt_dlp_command', return_value=['yt-dlp']),
            mock.patch.object(
                youtube_handler,
                'test_video_availability',
                return_value=(True, "id\ttitle", None),
            ),
            mock.patch.object(youtube_handler, 'get_ffmpeg_path', return_value=None),
            mock.patch.object(youtube_handler, 'is_docker_env', return_value=False),
        ]

    def test_progress_path_does_not_log_proxy_credentials(self):
        with contextlib.ExitStack() as stack:
            for patch in self._base_patches():
                stack.enter_context(patch)
            popen_mock = stack.enter_context(
                mock.patch.object(
                    youtube_handler.subprocess,
                    'Popen',
                    return_value=_FakeProcess(),
                )
            )

            youtube_handler.download_video_data(
                WATCH_URL,
                task_id='progress-path',
                progress_callback=lambda info: None,
            )

        logs = self.assert_no_credentials_in_logs()
        self.assertIn("执行 yt-dlp 下载命令", logs)
        self.assertIn("下载命令已就绪", logs)

        # 脱敏只作用于日志：真实命令仍必须把代理凭据交给 yt-dlp，否则代理会失效
        self.assertTrue(popen_mock.called)
        command = popen_mock.call_args.args[0]
        self.assertIn('--proxy', command)
        self.assertIn(PROXY_URL_WITH_AUTH, command)

    def test_failure_path_does_not_log_proxy_credentials(self):
        def _fail(cmd, **kwargs):
            # 模拟真实代码路径：异常对象本身携带完整命令（含代理凭据）
            raise subprocess.CalledProcessError(1, cmd, output="")

        with contextlib.ExitStack() as stack:
            for patch in self._base_patches():
                stack.enter_context(patch)
            stack.enter_context(
                mock.patch.object(youtube_handler.subprocess, 'run', side_effect=_fail)
            )

            success, error = youtube_handler.download_video_data(
                WATCH_URL,
                task_id='failure-path',
            )

        logs = self.assert_no_credentials_in_logs()
        self.assertFalse(success)
        self.assertNotIn(PROXY_PASSWORD, error)
        self.assertNotIn(PROXY_USERNAME, error)
        self.assertIn("非零退出码 1", logs)


if __name__ == "__main__":
    unittest.main()
