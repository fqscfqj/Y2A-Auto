import unittest

from modules.asr_api_client import AsrApiClient, AsrConfig
from modules.subtitle_pipeline_types import DetectedSpeechWindow


class AsrApiClientTests(unittest.TestCase):
    def test_whisper_granularity_candidates_prefer_word_then_fallback(self):
        client = AsrApiClient(AsrConfig(api_key='', timestamp_granularities='segment,word'))

        self.assertEqual(
            client._whisper_granularity_candidates(),
            [('segment', 'word'), ('segment',), tuple()],
        )

    def test_whisper_granularity_candidates_accept_word_only_config(self):
        client = AsrApiClient(AsrConfig(api_key='', timestamp_granularities='word'))

        self.assertEqual(
            client._whisper_granularity_candidates(),
            [('segment', 'word'), ('segment',), tuple()],
        )

    def test_payload_to_transcription_result_uses_top_level_word_timings(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=10.0, end_s=12.0, ownership_start_s=10.0, ownership_end_s=12.0)
        payload = {
            'text': 'hello world',
            'language': 'en',
            'segments': [{'start': 0.0, 'end': 2.0, 'text': 'hello world'}],
            'words': [
                {'word': 'hello', 'start': 0.1, 'end': 0.7},
                {'word': 'world', 'start': 0.8, 'end': 1.5},
            ],
        }

        result = client._payload_to_transcription_result(
            payload,
            provider='whisper',
            response_format='verbose_json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment', 'word'),
        )

        self.assertEqual(result.timestamp_mode, 'word')
        self.assertEqual(result.language, 'en')
        self.assertEqual(len(result.segments), 1)
        self.assertEqual([word.text for word in result.segments[0].words], ['hello', 'world'])

    def test_detect_language_from_segments_uses_three_point_majority(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        detected_by_clip = {
            '0.0-1.0': 'en',
            '2.0-3.0': 'zh',
            '4.0-5.0': 'zh',
        }

        def extract_clip(_audio_wav, start_s, end_s):
            return f'{start_s:.1f}-{end_s:.1f}'

        client.detect_language = lambda clip: detected_by_clip.get(clip, '')

        language = client.detect_language_from_segments(
            'audio.wav',
            [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 5.0)],
            extract_clip,
        )

        self.assertEqual(language, 'zh')


if __name__ == '__main__':
    unittest.main()
