import os
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch

from modules.vad_processor import VadConfig, VadProcessor


class VadProcessorTests(unittest.TestCase):
    def test_direct_defaults_match_production_window_limits(self):
        config = VadConfig()

        self.assertEqual(config.chunk_window_s, 15.0)
        self.assertEqual(config.max_segment_s, 15.0)
        self.assertEqual(config.max_segment_s_for_split, 15.0)

    def test_isolated_short_segment_does_not_span_long_silence(self):
        processor = VadProcessor(VadConfig())

        result = processor._apply_constraints(
            [(0.0, 1.0), (10.0, 10.3)],
            config=processor.config,
        )

        self.assertEqual(result, [(0.0, 1.0), (10.0, 10.3)])

    def test_leading_short_segment_does_not_expand_distant_next_segment(self):
        processor = VadProcessor(VadConfig())

        result = processor._apply_constraints(
            [(0.0, 0.3), (10.0, 11.0)],
            config=processor.config,
        )

        self.assertEqual(result, [(0.0, 0.3), (10.0, 11.0)])

    def test_nearby_segments_still_merge_within_configured_gap(self):
        processor = VadProcessor(VadConfig())

        result = processor._apply_constraints(
            [(0.0, 1.0), (1.2, 1.5)],
            config=processor.config,
        )

        self.assertEqual(result, [(0.0, 1.5)])

    def test_gap_merge_does_not_create_overlong_window(self):
        config = VadConfig(min_segment_s=0.0)
        processor = VadProcessor(config)

        result = processor._apply_constraints(
            [(0.0, 14.8), (15.0, 15.8)],
            config=config,
        )

        self.assertEqual(result, [(0.0, 14.8), (15.0, 15.8)])

    def test_overlapping_segments_are_unioned_before_force_split(self):
        config = VadConfig(min_segment_s=0.0)
        processor = VadProcessor(config)

        result = processor._apply_constraints(
            [(0.0, 14.8), (14.0, 16.0)],
            config=config,
        )

        self.assertEqual(result, [(0.0, 15.0), (15.0, 16.0)])

    def test_silero_seconds_use_centisecond_resolution(self):
        handle = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        wav_path = handle.name
        handle.close()
        self.addCleanup(lambda: os.path.exists(wav_path) and os.unlink(wav_path))

        with wave.open(wav_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b'\x00\x00' * 16000)

        get_speech_timestamps = Mock(return_value=[{'start': 0.12, 'end': 0.98}])
        processor = VadProcessor(VadConfig())
        with patch.object(
            processor,
            '_load_silero_vad',
            return_value=(object(), {'get_speech_timestamps': get_speech_timestamps}),
        ):
            result = processor._run_vad_on_audio(wav_path, 1.0, processor.config)

        self.assertEqual(result, [(0.12, 0.98)])
        self.assertEqual(get_speech_timestamps.call_args.kwargs['time_resolution'], 2)

    def test_adaptive_thresholds_preserve_explicit_zero_base(self):
        processor = VadProcessor(VadConfig(threshold=0.0))

        self.assertEqual(processor._build_relaxed_retry_config().threshold, 0.35)
        self.assertEqual(processor._build_refinement_config(processor.config).threshold, 0.08)


if __name__ == '__main__':
    unittest.main()
