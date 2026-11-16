import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from modules.acfun_uploader import AcfunUploader, UploadStageResult
from modules.content_moderator import AlibabaCloudModerator
from modules.subtitle_translator import SubtitleItem, SubtitleTranslator, TranslationConfig


class _FailingWriter:
    def write_srt(self, *args, **kwargs):
        raise OSError("disk full")


class MediaFailureControlTests(unittest.TestCase):
    def test_subtitle_write_failure_keeps_existing_output_and_returns_false(self):
        translator = SubtitleTranslator.__new__(SubtitleTranslator)
        translator.writer = _FailingWriter()
        translator.logger = Mock()
        item = SubtitleItem(1, "00:00:00,000", "00:00:01,000", "source", "译文")
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "out.srt")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("old output")
            self.assertFalse(translator._write_translated_file([item], output))
            with open(output, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old output")

    def test_moderation_disabled_is_compatible_pass(self):
        moderator = AlibabaCloudModerator({"CONTENT_MODERATION_ENABLED": False})
        result = moderator.moderate_text("hello")
        self.assertTrue(result["pass"])
        self.assertEqual(result["status"], "disabled")

    def test_moderation_enabled_without_sdk_is_unavailable_fail_closed(self):
        with patch("modules.content_moderator.ALIBABA_CLOUD_AVAILABLE", False), patch(
            "modules.content_moderator._import_error", "missing sdk"
        ):
            moderator = AlibabaCloudModerator({"CONTENT_MODERATION_ENABLED": True})
            result = moderator.moderate_text("hello")
        self.assertFalse(result["pass"])
        self.assertEqual(result["status"], "unavailable")

    def test_upload_finish_failure_is_structured(self):
        uploader = AcfunUploader.__new__(AcfunUploader)
        uploader.FINISH_URL = "finish"
        uploader.logger = None
        response = SimpleNamespace(text='{"result": 7}', json=lambda: {"result": 7, "error_msg": "bad"})
        uploader.session = SimpleNamespace(post=Mock(return_value=response))
        result = uploader.upload_finish(12)
        self.assertIsInstance(result, UploadStageResult)
        self.assertFalse(result)
        self.assertEqual(result["status"], "failed")

    def test_create_video_blocks_when_upload_finish_fails(self):
        uploader = AcfunUploader.__new__(AcfunUploader)
        uploader.C_VIDEO_URL = "create"
        uploader.logger = None
        response = SimpleNamespace(text='{"result": 0}', json=lambda: {"result": 0, "videoId": 42})
        uploader.session = SimpleNamespace(post=Mock(return_value=response))
        uploader.upload_finish = Mock(return_value=UploadStageResult(ok=False, status="failed", error="finish failed"))
        video_id, error = uploader.create_video(9, "video.mp4")
        self.assertIsNone(video_id)
        self.assertIn("finish failed", error)
        uploader.upload_finish.assert_called_once()

    def test_create_douga_blocks_when_video_complete_fails(self):
        uploader = AcfunUploader.__new__(AcfunUploader)
        uploader.logger = None
        uploader.task_id = None
        uploader.get_token = Mock(return_value=("task", "token", 4))
        uploader.upload_chunk = Mock(return_value=UploadStageResult(ok=True, status="success"))
        uploader.complete_upload = Mock(return_value=UploadStageResult(ok=False, status="failed", error="complete failed"))
        uploader.create_video = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            video_path = os.path.join(tmp, "video.mp4")
            with open(video_path, "wb") as video:
                video.write(b"1234")
            ok, error = uploader.create_douga(video_path, "title", 1, "cover.jpg")
        self.assertFalse(ok)
        self.assertIn("complete failed", error)
        uploader.create_video.assert_not_called()


if __name__ == "__main__":
    unittest.main()
