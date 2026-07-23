"""YouTube 下载产物识别回归测试。"""

import unittest

from modules.youtube_handler import _is_video_output_file


class TestVideoExtDetection(unittest.TestCase):
    def test_accepts_supported_video_outputs(self):
        for filename in ('video.mp4', 'video.MKV', 'video.f399.webm', 'video.mov'):
            with self.subTest(filename=filename):
                self.assertTrue(_is_video_output_file(filename))

    def test_rejects_live_chat_and_other_sidecar_files(self):
        for filename in (
            'video.live_chat.json',
            'video.info.json',
            'video.en.srt',
            'video.zh.vtt',
            'video.webp',
            'video.jpg',
            'cover.mp4',
        ):
            with self.subTest(filename=filename):
                self.assertFalse(_is_video_output_file(filename))


if __name__ == '__main__':
    unittest.main()
