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

    def test_extract_words_skips_token_only_format(self):
        """Backends like parakeet-crispasr return tokens with only text (no timing).
        _extract_words correctly skips them; the segment loop handles interpolation."""
        client = AsrApiClient(AsrConfig(api_key=''))
        raw_tokens = [
            {'token': 'hello'},
            {'token': 'world'},
            {'token': 'here'},
        ]
        words = client._extract_words(raw_tokens)
        self.assertEqual(words, [])

    def test_token_with_start_time_end_time_is_accepted(self):
        """Tokens using start_time/end_time naming are now recognized."""
        client = AsrApiClient(AsrConfig(api_key=''))
        raw_words = [
            {'token': 'hello', 'start_time': 0.1, 'end_time': 0.7},
            {'token': 'world', 'start_time': 0.8, 'end_time': 1.5},
        ]
        words = client._extract_words(raw_words)
        self.assertEqual(len(words), 2)
        self.assertEqual([w.text for w in words], ['hello', 'world'])
        self.assertAlmostEqual(words[0].start_s, 0.1)
        self.assertAlmostEqual(words[0].end_s, 0.7)

    def test_payload_with_token_only_segments_produces_result(self):
        """End-to-end: parakeet-style payload still produces a valid result."""
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=0.0, end_s=2.0, ownership_start_s=0.0, ownership_end_s=2.0)
        payload = {
            'text': 'hello world',
            'segments': [{
                'start': 0.0, 'end': 2.0, 'text': 'hello world',
                'tokens': [{'token': 'hello'}, {'token': 'world'}],
            }],
        }
        result = client._payload_to_transcription_result(
            payload,
            provider='parakeet',
            response_format='json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment',),
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, 'hello world')
        self.assertEqual(len(result.segments[0].words), 2)
        self.assertAlmostEqual(result.segments[0].words[0].start_s, 0.0)
        self.assertAlmostEqual(result.segments[0].words[0].end_s, 1.0)
        self.assertAlmostEqual(result.segments[0].words[1].start_s, 1.0)
        self.assertAlmostEqual(result.segments[0].words[1].end_s, 2.0)


if __name__ == '__main__':
    unittest.main()
