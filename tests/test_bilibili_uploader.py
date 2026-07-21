import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.bili_sdk.exceptions.NetworkException import NetworkException
from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
from modules.bili_sdk.video_uploader import (
    VideoUploader,
    VideoUploaderEvents,
    VideoUploaderPage,
)
from modules.bilibili_uploader import BilibiliUploader, _BilibiliChunkProgress


class _FakeResponse:
    def __init__(self, code=200, text="MULTIPART_PUT_SUCCESS"):
        self.code = code
        self._text = text

    def utf8_text(self):
        return self._text


class BilibiliSdkChunkRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.video_path = os.path.join(self.temp_dir.name, "video.mp4")
        with open(self.video_path, "wb") as stream:
            stream.write(b"abcdefgh")
        self.page = VideoUploaderPage(self.video_path, "test")
        self.uploader = object.__new__(VideoUploader)
        AsyncEvent.__init__(self.uploader)
        self.uploader.line = None
        self.preupload = {
            "endpoint": "//upload.example.com",
            "upos_uri": "upos://bucket/video.mp4",
            "upload_id": "upload-id",
            "chunk_size": 8,
            "auth": "auth",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_chunk_retries_transient_failures_then_succeeds(self):
        client = Mock()
        client.request = AsyncMock(
            side_effect=[
                _FakeResponse(503),
                _FakeResponse(503),
                _FakeResponse(200),
            ]
        )
        failures = []
        successes = []
        self.uploader.add_event_listener(
            VideoUploaderEvents.CHUNK_FAILED.value, failures.append
        )
        self.uploader.add_event_listener(
            VideoUploaderEvents.AFTER_CHUNK.value, successes.append
        )

        with patch(
            "modules.bili_sdk.video_uploader.get_client", return_value=client
        ), patch(
            "modules.bili_sdk.video_uploader.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            result = asyncio.run(
                self.uploader._upload_chunk(
                    self.page, 0, 0, 1, dict(self.preupload)
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(client.request.await_count, 3)
        self.assertEqual([item["attempt"] for item in failures], [1, 2])
        self.assertEqual([item["retrying"] for item in failures], [True, True])
        self.assertEqual([item["retry_delay_seconds"] for item in failures], [1, 2])
        self.assertEqual(len(successes), 1)
        self.assertEqual(sleep_mock.await_count, 2)

    def test_chunk_stops_after_three_transient_failures(self):
        client = Mock()
        client.request = AsyncMock(return_value=_FakeResponse(503))
        failures = []
        self.uploader.add_event_listener(
            VideoUploaderEvents.CHUNK_FAILED.value, failures.append
        )

        with patch(
            "modules.bili_sdk.video_uploader.get_client", return_value=client
        ), patch(
            "modules.bili_sdk.video_uploader.asyncio.sleep", new=AsyncMock()
        ):
            with self.assertRaises(NetworkException):
                asyncio.run(
                    self.uploader._upload_chunk(
                        self.page, 0, 0, 1, dict(self.preupload)
                    )
                )

        self.assertEqual(client.request.await_count, 3)
        self.assertEqual(len(failures), 3)
        self.assertFalse(failures[-1]["retrying"])
        self.assertEqual(failures[-1]["attempt"], 3)
        self.assertEqual(failures[-1]["status_code"], 503)

    def test_chunk_does_not_retry_client_error(self):
        client = Mock()
        client.request = AsyncMock(return_value=_FakeResponse(403))
        failures = []
        self.uploader.add_event_listener(
            VideoUploaderEvents.CHUNK_FAILED.value, failures.append
        )

        with patch(
            "modules.bili_sdk.video_uploader.get_client", return_value=client
        ):
            with self.assertRaises(NetworkException):
                asyncio.run(
                    self.uploader._upload_chunk(
                        self.page, 0, 0, 1, dict(self.preupload)
                    )
                )

        self.assertEqual(client.request.await_count, 1)
        self.assertEqual(len(failures), 1)
        self.assertFalse(failures[0]["retrying"])

    def test_upload_page_uses_one_thread_when_preupload_returns_zero(self):
        preupload = {
            **self.preupload,
            "chunk_size": 4,
            "threads": 0,
            "biz_id": 123,
        }
        self.uploader._preupload = AsyncMock(return_value=preupload)
        self.uploader._upload_chunk = AsyncMock(return_value={"ok": True})
        self.uploader._complete_page = AsyncMock(
            return_value={"filename": "video", "cid": 123}
        )

        result = asyncio.run(self.uploader._upload_page(self.page))

        self.assertEqual(result, {"filename": "video", "cid": 123})
        self.assertEqual(self.uploader._upload_chunk.await_count, 2)
        self.uploader._complete_page.assert_awaited_once()


class BilibiliProgressTests(unittest.TestCase):
    def test_progress_counts_unique_chunks_in_completion_order(self):
        tracker = _BilibiliChunkProgress()
        page = object()

        first = tracker.record(
            {"page": page, "chunk_number": 0, "total_chunk_count": 23}
        )
        duplicate = tracker.record(
            {"page": page, "chunk_number": 0, "total_chunk_count": 23}
        )
        out_of_order = tracker.record(
            {"page": page, "chunk_number": 22, "total_chunk_count": 23}
        )

        self.assertAlmostEqual(first, 95 / 23)
        self.assertEqual(duplicate, first)
        self.assertAlmostEqual(out_of_order, 2 * 95 / 23)

        final = out_of_order
        for chunk_number in range(1, 22):
            final = tracker.record(
                {
                    "page": page,
                    "chunk_number": chunk_number,
                    "total_chunk_count": 23,
                }
            )
        self.assertEqual(final, 95.0)

    def test_upload_video_emits_stage_progress_and_retry_log(self):
        progress = []
        logger = Mock()

        class FakePage:
            def __init__(self, path, title):
                self.path = path
                self.title = title

        class FakeUploader(AsyncEvent):
            def __init__(self, pages, meta, credential, cover):
                super().__init__()
                self.pages = pages

            async def start(self):
                page = self.pages[0]
                self.dispatch(
                    VideoUploaderEvents.CHUNK_FAILED.value,
                    {
                        "page": page,
                        "chunk_number": 0,
                        "total_chunk_count": 3,
                        "attempt": 1,
                        "max_attempts": 3,
                        "retrying": True,
                        "retry_delay_seconds": 1,
                        "info": "Status 503",
                    },
                )
                for chunk_number in (0, 0, 2, 1):
                    self.dispatch(
                        VideoUploaderEvents.AFTER_CHUNK.value,
                        {
                            "page": page,
                            "chunk_number": chunk_number,
                            "total_chunk_count": 3,
                        },
                    )
                self.dispatch(
                    VideoUploaderEvents.PRE_PAGE_SUBMIT.value, {"page": page}
                )
                self.dispatch(
                    VideoUploaderEvents.AFTER_PAGE_SUBMIT.value, {"page": page}
                )
                self.dispatch(VideoUploaderEvents.PRE_COVER.value, None)
                self.dispatch(VideoUploaderEvents.AFTER_COVER.value, {"url": "cover"})
                self.dispatch(VideoUploaderEvents.PRE_SUBMIT.value, {})
                self.dispatch(
                    VideoUploaderEvents.AFTER_SUBMIT.value,
                    {"bvid": "BV1test", "aid": 1},
                )
                return {"bvid": "BV1test", "aid": 1}

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "video.mp4")
            cover_path = os.path.join(temp_dir, "cover.jpg")
            for path in (video_path, cover_path):
                with open(path, "wb") as stream:
                    stream.write(b"test")

            with patch(
                "modules.bilibili_uploader.setup_task_logger", return_value=logger
            ), patch(
                "modules.bilibili_uploader.configure_bilibili_runtime"
            ), patch(
                "modules.bilibili_uploader.load_credential_from_file",
                return_value=object(),
            ), patch(
                "modules.bilibili_uploader.validate_credential_remote",
                return_value=(True, "ok"),
            ), patch(
                "modules.bilibili_uploader.video_uploader.VideoMeta",
                return_value=object(),
            ), patch(
                "modules.bilibili_uploader.video_uploader.VideoUploaderPage",
                FakePage,
            ), patch(
                "modules.bilibili_uploader.video_uploader.VideoUploader",
                FakeUploader,
            ):
                success, result = BilibiliUploader("cookies.json").upload_video(
                    video_path,
                    cover_path,
                    "标题",
                    "简介",
                    ["标签"],
                    21,
                    youtube_url="https://www.youtube.com/watch?v=test",
                    task_id="test-task",
                    progress_callback=progress.append,
                )

        self.assertTrue(success)
        self.assertEqual(result["bvid"], "BV1test")
        self.assertEqual(
            progress,
            ["0.0%", "31.7%", "63.3%", "95.0%", "96.0%", "98.0%", "99.0%", "100.0%"],
        )
        log_messages = [call.args[0] for call in logger.info.call_args_list]
        self.assertTrue(any("尝试 1/3，1 秒后重试" in item for item in log_messages))
        self.assertTrue(any("正在提交分P" in item for item in log_messages))
        self.assertTrue(any("正在提交Bilibili投稿" in item for item in log_messages))


if __name__ == "__main__":
    unittest.main()
