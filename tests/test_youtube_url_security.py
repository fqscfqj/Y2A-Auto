import logging
import pathlib
import tempfile
import unittest
from unittest import mock

from modules import youtube_handler


class YouTubeUrlSecurityTests(unittest.TestCase):
    def test_normalizes_supported_video_and_playlist_urls(self):
        self.assertEqual(
            youtube_handler.normalize_youtube_url(
                " https://WWW.YouTube.com/watch?v=dQw4w9WgXcQ#tracking "
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            youtube_handler.normalize_youtube_url("youtu.be/dQw4w9WgXcQ?t=12"),
            "https://youtu.be/dQw4w9WgXcQ?t=12",
        )
        self.assertEqual(
            youtube_handler.normalize_youtube_url(
                "https://www.youtube.com/playlist?list=PL_test-01"
            ),
            "https://www.youtube.com/playlist?list=PL_test-01",
        )
        self.assertEqual(
            youtube_handler._is_safe_playlist_url(
                "https://www.youtube.com/watch?v=abc123&list=PL_test-01",
                logging.getLogger("test"),
            ),
            "https://www.youtube.com/watch?v=abc123&list=PL_test-01",
        )

    def test_rejects_private_ip_and_deceptive_hosts(self):
        rejected = (
            "https://127.0.0.1/watch?v=abc123",
            "https://10.0.0.1/watch?v=abc123",
            "https://[::1]/watch?v=abc123",
            "https://169.254.169.254/watch?v=abc123",
            "https://www.youtube.com.evil.example/watch?v=abc123",
            "https://youtube.com@127.0.0.1/watch?v=abc123",
            "https://www.youtube.com:444/watch?v=abc123",
            "https://example.com/watch?v=abc123",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(youtube_handler.normalize_youtube_url(url))

    def test_video_precheck_rejects_unsafe_url_without_running_process(self):
        with mock.patch.object(youtube_handler.subprocess, "run") as run_mock:
            available, info, error = youtube_handler.test_video_availability(
                "https://127.0.0.1/watch?v=abc123",
                ["yt-dlp"],
                logger=mock.Mock(),
            )
        self.assertFalse(available)
        self.assertIsNone(info)
        self.assertIn("URL未通过安全校验", error)
        run_mock.assert_not_called()

    def test_download_command_keeps_certificate_validation(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            youtube_handler, "get_app_subdir", side_effect=lambda name: str(pathlib.Path(temp_dir) / name)
        ), mock.patch.object(youtube_handler, "setup_task_logger", return_value=logger), mock.patch.object(
            youtube_handler, "_find_yt_dlp_command", return_value=["yt-dlp"]
        ), mock.patch.object(
            youtube_handler, "test_video_availability", return_value=(True, "id\ttitle", None)
        ), mock.patch.object(youtube_handler, "get_ffmpeg_path", return_value=None), mock.patch.object(
            youtube_handler, "is_docker_env", return_value=False
        ), mock.patch.object(youtube_handler.subprocess, "Popen") as popen_mock, mock.patch.object(
            youtube_handler, "load_config", return_value={}
        ):
            process = mock.Mock(returncode=0, stdout=iter(()), wait=mock.Mock(return_value=0))
            popen_mock.return_value = process
            youtube_handler.download_video_data(
                "https://www.youtube.com/watch?v=abc123",
                task_id="cert-check",
                progress_callback=lambda _info: None,
            )

        command = popen_mock.call_args.args[0]
        self.assertNotIn("--no-check-certificates", command)


if __name__ == "__main__":
    unittest.main()
