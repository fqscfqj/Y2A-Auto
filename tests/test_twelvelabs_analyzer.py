#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""TwelveLabs Pegasus 视频理解模块测试。

- 无网络单元测试：通过 mock 验证解析逻辑与降级行为，始终运行。
- 在线契约测试：仅在设置了 TWELVELABS_API_KEY 时运行，校验真实 Pegasus 返回。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from modules import twelvelabs_analyzer as tla
from modules.twelvelabs_analyzer import TwelveLabsAnalyzer, _parse_moderation_json


class ParseModerationJsonTest(unittest.TestCase):
    def test_parses_plain_json(self):
        out = _parse_moderation_json('{"safe": false, "labels": ["暴力"], "reason": "打斗"}')
        self.assertEqual(out, {"safe": False, "labels": ["暴力"], "reason": "打斗"})

    def test_parses_fenced_json_block(self):
        text = '```json\n{"safe": true, "labels": [], "reason": "正常"}\n```'
        self.assertEqual(_parse_moderation_json(text).get("safe"), True)

    def test_extracts_embedded_json(self):
        text = '分析结果如下：{"safe": false, "labels": ["色情"]} 仅供参考'
        out = _parse_moderation_json(text)
        self.assertFalse(out["safe"])
        self.assertEqual(out["labels"], ["色情"])

    def test_returns_none_on_garbage(self):
        self.assertIsNone(_parse_moderation_json("这不是 JSON"))
        self.assertIsNone(_parse_moderation_json(""))


class AnalyzeVideoUnitTest(unittest.TestCase):
    """用 mock 的 SDK 客户端验证上传/审核/描述/清理流程，无需网络。"""

    def _make_analyzer(self):
        analyzer = TwelveLabsAnalyzer.__new__(TwelveLabsAnalyzer)
        analyzer.config = {}
        analyzer.task_id = None
        analyzer.logger = MagicMock()
        analyzer.model_name = "pegasus1.5"
        analyzer.client = MagicMock()
        # 资产上传：直接 ready
        asset = MagicMock(id="asset123", status="ready")
        analyzer.client.assets.create.return_value = asset
        analyzer.client.assets.retrieve.return_value = asset
        return analyzer

    def test_skips_when_file_missing(self):
        with patch.object(tla, "TWELVELABS_AVAILABLE", True):
            analyzer = self._make_analyzer()
            result = analyzer.analyze_video("/no/such/file.mp4")
        self.assertFalse(result["available"])
        self.assertTrue(result["moderation"]["pass"])  # 跳过时按通过处理

    def test_skips_when_file_too_large(self):
        with patch.object(tla, "TWELVELABS_AVAILABLE", True), \
                patch("os.path.exists", return_value=True), \
                patch("os.path.getsize", return_value=300 * 1024 * 1024):
            analyzer = self._make_analyzer()
            result = analyzer.analyze_video("/big.mp4")
        self.assertFalse(result["available"])
        analyzer.client.assets.create.assert_not_called()

    def test_unsafe_video_fails_moderation(self):
        analyzer = self._make_analyzer()
        analyzer.client.analyze.return_value = MagicMock(
            data='{"safe": false, "labels": ["暴力"], "reason": "含打斗画面"}'
        )
        with patch.object(tla, "TWELVELABS_AVAILABLE", True), \
                patch("os.path.exists", return_value=True), \
                patch("os.path.getsize", return_value=1024), \
                patch("builtins.open", new_callable=MagicMock):
            result = analyzer.analyze_video("/v.mp4", want_description=False)
        self.assertTrue(result["available"])
        self.assertFalse(result["moderation"]["pass"])
        # 资产应被清理
        analyzer.client.assets.delete.assert_called_once_with(asset_id="asset123")

    def test_safe_video_passes_and_describes(self):
        analyzer = self._make_analyzer()
        analyzer.client.analyze.side_effect = [
            MagicMock(data='{"safe": true, "labels": [], "reason": "正常"}'),
            MagicMock(data="一只猫在窗台上晒太阳。"),
        ]
        with patch.object(tla, "TWELVELABS_AVAILABLE", True), \
                patch("os.path.exists", return_value=True), \
                patch("os.path.getsize", return_value=1024), \
                patch("builtins.open", new_callable=MagicMock):
            result = analyzer.analyze_video("/v.mp4")
        self.assertTrue(result["moderation"]["pass"])
        self.assertEqual(result["description"], "一只猫在窗台上晒太阳。")


@unittest.skipUnless(
    os.environ.get("TWELVELABS_API_KEY"),
    "需要 TWELVELABS_API_KEY 才能运行在线契约测试",
)
class TwelveLabsLiveContractTest(unittest.TestCase):
    """真实调用 Pegasus，校验 SDK 契约（asset 上传 + analyze 返回文本）。"""

    def test_describe_generated_clip(self):
        import shutil
        import subprocess
        import tempfile

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("需要 ffmpeg 生成测试视频")

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=24",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", tmp.name],
                check=True,
            )
            analyzer = TwelveLabsAnalyzer({}, task_id=None)
            result = analyzer.analyze_video(tmp.name)
            self.assertTrue(result["available"], result.get("reason"))
            self.assertIn("pass", result["moderation"])
            self.assertIsInstance(result["description"], str)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
