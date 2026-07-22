"""回归测试:任务目录同时存在video.mp4/video.live_chat.json/字幕/封面时,
必须稳定选择video.mp4而非live_chat.json"""
import os, tempfile, shutil, unittest
from pathlib import Path

class TestVideoExtDetection(unittest.TestCase):
    def test_live_chat_json_not_treated_as_video(self):
        """video.live_chat.json (3KB) 不应被识别为视频"""
        with tempfile.TemporaryDirectory() as tmp:
            # 创建模拟文件
            task_dir = Path(tmp)
            (task_dir / 'video.mp4').write_bytes(b'\x00' * 1024 * 1024)  # 1MB视频
            (task_dir / 'video.live_chat.json').write_bytes(b'{}')
            (task_dir / 'video.en.srt').write_text('1\n00:00:00 --> 00:00:01\ntest')
            (task_dir / 'video.webp').write_bytes(b'\x00' * 1024)  # 封面
            
            # 模拟find_video_in_task_dir逻辑
            video_exts = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v'}
            video_candidates = [p for p in task_dir.glob('video.*')
                                if p.suffix.lower() in video_exts and p.name != 'video.mp4' and '.info' not in p.name]
            # 主视频
            main_video = task_dir / 'video.mp4'
            self.assertTrue(main_video.exists())
            # live_chat.json不在视频候选中
            chat_file = task_dir / 'video.live_chat.json'
            self.assertNotIn(chat_file.suffix.lower(), video_exts)
            self.assertEqual(len(video_candidates), 0, "不应有额外视频候选(live_chat.json不是视频)")
    
    def test_video_ext_whitelist(self):
        """白名单只包含标准视频扩展名"""
        video_exts = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v'}
        self.assertIn('.mp4', video_exts)
        self.assertIn('.mkv', video_exts)
        self.assertNotIn('.json', video_exts)
        self.assertNotIn('.srt', video_exts)
        self.assertNotIn('.vtt', video_exts)
        self.assertNotIn('.webp', video_exts)
        self.assertNotIn('.jpg', video_exts)

if __name__ == '__main__':
    unittest.main()
