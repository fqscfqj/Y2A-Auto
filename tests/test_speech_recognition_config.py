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

    def test_whisper_inherits_key_and_normalizes_global_responses_endpoint(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'OPENAI_API_KEY': 'global-llm-key',
            'OPENAI_BASE_URL': 'https://llm.example/v1/responses',
        }, task_id='unit-test-whisper-isolated')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.api_key, 'global-llm-key')
        self.assertEqual(recognizer.config.base_url, 'https://llm.example/v1')

    def test_whisper_keeps_inheriting_custom_global_api_root(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'OPENAI_API_KEY': 'gateway-key',
            'OPENAI_BASE_URL': 'https://gateway.example/custom/v1',
        }, task_id='unit-test-whisper-custom-global')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.api_key, 'gateway-key')
        self.assertEqual(recognizer.config.base_url, 'https://gateway.example/custom/v1')

    def test_whisper_uses_its_own_endpoint_and_key(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'WHISPER_API_KEY': 'asr-key',
            'WHISPER_BASE_URL': 'https://asr.example/v1',
            'OPENAI_API_KEY': 'global-llm-key',
            'OPENAI_BASE_URL': 'https://llm.example/v1/responses',
        }, task_id='unit-test-whisper-own-config')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.api_key, 'asr-key')
        self.assertEqual(recognizer.config.base_url, 'https://asr.example/v1')

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
