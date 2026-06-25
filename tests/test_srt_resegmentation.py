"""Tests for SRT re-segmentation adapter functions in ai_segmentation.py."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules.subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    DetectedSpeechWindow,
)


class TestSrtToAsrResults(unittest.TestCase):
    """Tests for srt_to_asr_results()."""

    def _write_srt(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix='.srt')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    def test_parses_standard_srt(self):
        from modules.ai_segmentation import srt_to_asr_results

        srt = (
            "1\n00:00:01,000 --> 00:00:03,500\nHello world\n\n"
            "2\n00:00:03,500 --> 00:00:06,000\nThis is a test\n\n"
            "3\n00:00:06,000 --> 00:00:09,200\nGoodbye\n"
        )
        path = self._write_srt(srt)
        try:
            results = srt_to_asr_results(path)
            self.assertEqual(len(results), 3)

            # Check first result
            r0 = results[0]
            self.assertIsInstance(r0, AsrTranscriptionResult)
            self.assertEqual(r0.provider, 'srt_file')
            self.assertEqual(r0.timestamp_mode, 'segment')
            self.assertEqual(len(r0.segments), 1)
            self.assertAlmostEqual(r0.segments[0].start_s, 1.0, places=2)
            self.assertAlmostEqual(r0.segments[0].end_s, 3.5, places=2)
            self.assertEqual(r0.segments[0].text, 'Hello world')
            self.assertIsNotNone(r0.window)
            self.assertAlmostEqual(r0.window.start_s, 1.0, places=2)
            self.assertAlmostEqual(r0.window.end_s, 3.5, places=2)
        finally:
            os.unlink(path)

    def test_returns_empty_for_empty_file(self):
        from modules.ai_segmentation import srt_to_asr_results

        path = self._write_srt("")
        try:
            results = srt_to_asr_results(path)
            self.assertEqual(results, [])
        finally:
            os.unlink(path)

    def test_skips_invalid_cues(self):
        from modules.ai_segmentation import srt_to_asr_results

        # Cue with empty text should be skipped
        srt = (
            "1\n00:00:01,000 --> 00:00:03,000\n\n\n"
            "2\n00:00:03,000 --> 00:00:06,000\nValid text\n"
        )
        path = self._write_srt(srt)
        try:
            results = srt_to_asr_results(path)
            # Only the valid cue should be returned
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].segments[0].text, 'Valid text')
        finally:
            os.unlink(path)

    def test_handles_multiline_cue_text(self):
        from modules.ai_segmentation import srt_to_asr_results

        srt = "1\n00:00:01,000 --> 00:00:05,000\nLine one\nLine two\n"
        path = self._write_srt(srt)
        try:
            results = srt_to_asr_results(path)
            self.assertEqual(len(results), 1)
            # Multi-line text should be joined with space
            text = results[0].segments[0].text
            self.assertIn('Line one', text)
            self.assertIn('Line two', text)
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        from modules.ai_segmentation import srt_to_asr_results

        results = srt_to_asr_results('/nonexistent/path.srt')
        self.assertEqual(results, [])


class TestResegmentSrtFile(unittest.TestCase):
    """Tests for resegment_srt_file()."""

    def _write_srt(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix='.srt')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    def test_returns_none_when_segmenter_fails(self):
        from modules.ai_segmentation import AISegmentationConfig, resegment_srt_file

        srt = "1\n00:00:01,000 --> 00:00:03,000\nHello\n"
        path = self._write_srt(srt)
        try:
            # Config with enabled=True but no model configured → AISegmentationError
            config = AISegmentationConfig(enabled=True, api_key='', model_name='')
            result = resegment_srt_file(path, config)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_returns_none_for_empty_srt(self):
        from modules.ai_segmentation import AISegmentationConfig, resegment_srt_file

        path = self._write_srt("")
        try:
            config = AISegmentationConfig(enabled=True, api_key='fake', model_name='fake')
            config.resolved_api_key = 'fake'
            config.resolved_model_name = 'fake'
            result = resegment_srt_file(path, config)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    @patch('modules.ai_segmentation.AISegmenter')
    def test_writes_resegmented_file(self, mock_segmenter_cls):
        from modules.ai_segmentation import AISegmentationConfig, resegment_srt_file

        srt = (
            "1\n00:00:01,000 --> 00:00:05,000\nHello world this is a test\n\n"
            "2\n00:00:05,000 --> 00:00:10,000\nAnother sentence here\n"
        )
        path = self._write_srt(srt)
        try:
            # Mock the segmenter to return re-segmented cues
            mock_segmenter = MagicMock()
            mock_segmenter.segment.return_value = [
                AlignedSubtitleCue(start_s=1.0, end_s=3.0, text='Hello world'),
                AlignedSubtitleCue(start_s=3.0, end_s=5.0, text='this is a test'),
                AlignedSubtitleCue(start_s=5.0, end_s=7.5, text='Another sentence'),
                AlignedSubtitleCue(start_s=7.5, end_s=10.0, text='here'),
            ]
            mock_segmenter_cls.return_value = mock_segmenter

            config = AISegmentationConfig(enabled=True, api_key='fake', model_name='fake')
            config.resolved_api_key = 'fake'
            config.resolved_model_name = 'fake'

            result = resegment_srt_file(path, config)
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith('.resegmented.srt'))
            self.assertTrue(os.path.exists(result))

            # Verify the resegmented file content
            with open(result, encoding='utf-8') as f:
                content = f.read()
            self.assertIn('Hello world', content)
            self.assertIn('this is a test', content)
            self.assertIn('Another sentence', content)
            self.assertIn('here', content)

            # Should have 4 cues now instead of 2
            cue_count = content.count('-->')
            self.assertEqual(cue_count, 4)

            # Cleanup
            os.unlink(result)
        finally:
            os.unlink(path)

    @patch('modules.ai_segmentation.AISegmenter')
    def test_segmenter_receives_correct_input(self, mock_segmenter_cls):
        from modules.ai_segmentation import AISegmentationConfig, resegment_srt_file

        srt = "1\n00:00:01,000 --> 00:00:03,000\nTest\n"
        path = self._write_srt(srt)
        try:
            mock_segmenter = MagicMock()
            mock_segmenter.segment.return_value = [
                AlignedSubtitleCue(start_s=1.0, end_s=3.0, text='Test'),
            ]
            mock_segmenter_cls.return_value = mock_segmenter

            config = AISegmentationConfig(enabled=True, api_key='fake', model_name='fake')
            config.resolved_api_key = 'fake'
            config.resolved_model_name = 'fake'

            resegment_srt_file(path, config)

            # Verify segmenter was called with correct AsrTranscriptionResult
            mock_segmenter.segment.assert_called_once()
            call_args = mock_segmenter.segment.call_args[0][0]
            self.assertEqual(len(call_args), 1)
            self.assertIsInstance(call_args[0], AsrTranscriptionResult)
            self.assertEqual(call_args[0].segments[0].text, 'Test')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
