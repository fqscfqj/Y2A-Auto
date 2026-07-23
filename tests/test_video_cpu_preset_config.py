import unittest

from modules.config_manager import DEFAULT_CONFIG, normalize_video_cpu_preset


class VideoCpuPresetConfigTests(unittest.TestCase):
    def test_defaults_are_registered_for_persistence(self):
        self.assertEqual(DEFAULT_CONFIG['VIDEO_CPU_PRESET'], 'medium')
        self.assertEqual(DEFAULT_CONFIG['VIDEO_CPU_PRESET_HD'], 'veryfast')

    def test_normalizes_supported_preset(self):
        self.assertEqual(normalize_video_cpu_preset('  FAST  '), 'fast')

    def test_invalid_preset_uses_requested_fallback(self):
        self.assertEqual(normalize_video_cpu_preset('invalid', 'veryfast'), 'veryfast')


if __name__ == '__main__':
    unittest.main()
