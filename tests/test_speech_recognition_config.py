import unittest

from modules.speech_recognition import create_speech_recognizer_from_config


class SpeechRecognitionConfigTests(unittest.TestCase):
    def test_whisper_config_maps_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'WHISPER_TIMESTAMP_GRANULARITIES': 'word',
        }, task_id='unit-test-whisper')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'whisper')
        self.assertEqual(recognizer.config.api_provider, 'whisper')
        self.assertEqual(recognizer.config.whisper_timestamp_granularities, 'word')

    def test_voxtral_config_keeps_voxtral_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'voxtral',
            'VOXTRAL_TIMESTAMP_GRANULARITIES': 'segment,word',
            'VOXTRAL_BASE_URL': 'https://api.mistral.ai/v1',
        }, task_id='unit-test-voxtral')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'voxtral')
        self.assertEqual(recognizer.config.api_provider, 'voxtral')
        self.assertEqual(recognizer.config.voxtral_timestamp_granularities, 'segment,word')


if __name__ == '__main__':
    unittest.main()
