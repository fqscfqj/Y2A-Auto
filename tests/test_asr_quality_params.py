# -*- coding: utf-8 -*-
"""Tests for the ASR decoding-quality parameters, translation endpoint
routing, Voxtral language hint passthrough, window failure accounting and
language-detection sampling.

Every HTTP / SDK call is replaced by a fake so no network access happens.
"""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from modules.asr_api_client import AsrApiClient, AsrConfig
from modules.subtitle_pipeline_types import (
    AsrSegmentTiming,
    AsrTranscriptionResult,
    DetectedSpeechWindow,
)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _data_to_dict(data):
    """Collapse a multipart form list into an ordered mapping.

    Repeated keys (timestamp_granularities) are collected into a list.
    """
    result = {}
    for key, value in data:
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


class AsrQualityConfigTests(unittest.TestCase):
    def test_defaults_are_applied(self):
        config = AsrConfig()
        self.assertEqual(config.temperature, 0.0)
        self.assertIs(config.condition_on_previous_text, False)
        self.assertEqual(config.no_speech_threshold, 0.6)
        self.assertEqual(config.compression_ratio_threshold, 2.4)
        self.assertEqual(config.logprob_threshold, -1.0)

    def test_positional_and_keyword_construction_still_work(self):
        positional = AsrConfig(
            'whisper', 'key', 'http://localhost:8080/v1', 'whisper-1', 'zh',
            'prompt', False, 'segment,word', False, '', 3, 2.0, 3, 300.0,
            10800.0, True,
        )
        self.assertEqual(positional.provider, 'whisper')
        self.assertEqual(positional.max_workers, 3)
        # New fields live at the tail and therefore default for old callers.
        self.assertEqual(positional.temperature, 0.0)
        self.assertIs(positional.condition_on_previous_text, False)

        keyword = AsrConfig(
            api_key='key',
            temperature=0.2,
            condition_on_previous_text=True,
            no_speech_threshold=0.5,
            compression_ratio_threshold=2.0,
            logprob_threshold=-1.5,
        )
        self.assertEqual(keyword.temperature, 0.2)
        self.assertIs(keyword.condition_on_previous_text, True)
        self.assertEqual(keyword.logprob_threshold, -1.5)


class WhisperRawRequestTests(unittest.TestCase):
    def setUp(self):
        handle, self.wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(handle)

    def tearDown(self):
        try:
            os.remove(self.wav_path)
        except OSError:
            pass

    def _call(self, config, granularities=('segment', 'word')):
        client = AsrApiClient(config)
        payload = {
            'text': 'hello',
            'language': 'en',
            'duration': 1.0,
            'segments': [{'id': 0, 'start': 0.0, 'end': 1.0, 'text': 'hello'}],
        }
        fake_post = Mock(return_value=_FakeResponse(payload))
        with patch('modules.asr_api_client.requests.post', fake_post):
            result = client._request_whisper_raw_json(
                self.wav_path,
                'whisper-1',
                granularities=granularities,
            )
        _, kwargs = fake_post.call_args
        return result, fake_post.call_args[0][0], _data_to_dict(kwargs['data'])

    def test_quality_params_are_sent_on_raw_path(self):
        _result, _url, data = self._call(
            AsrConfig(api_key='', base_url='http://localhost:8080/v1')
        )
        self.assertIn('temperature', data)
        self.assertEqual(data['temperature'], '0')
        self.assertIn('condition_on_previous_text', data)
        self.assertEqual(data['condition_on_previous_text'], 'false')
        self.assertIn('no_speech_threshold', data)
        self.assertEqual(data['no_speech_threshold'], '0.6')
        self.assertIn('compression_ratio_threshold', data)
        self.assertEqual(data['compression_ratio_threshold'], '2.4')
        self.assertIn('logprob_threshold', data)
        self.assertEqual(data['logprob_threshold'], '-1')

    def test_config_overrides_are_honoured(self):
        _result, _url, data = self._call(
            AsrConfig(
                api_key='',
                temperature=0.25,
                condition_on_previous_text=True,
                no_speech_threshold=0.4,
                compression_ratio_threshold=1.8,
                logprob_threshold=-0.8,
            )
        )
        self.assertEqual(data['temperature'], '0.25')
        self.assertEqual(data['condition_on_previous_text'], 'true')
        self.assertEqual(data['no_speech_threshold'], '0.4')
        self.assertEqual(data['compression_ratio_threshold'], '1.8')
        self.assertEqual(data['logprob_threshold'], '-0.8')

    def test_none_quality_params_are_omitted_on_raw_path(self):
        _result, _url, data = self._call(
            AsrConfig(
                api_key='',
                temperature=None,
                condition_on_previous_text=None,
                no_speech_threshold=None,
                compression_ratio_threshold=None,
                logprob_threshold=None,
            )
        )
        for key in (
            'temperature',
            'condition_on_previous_text',
            'no_speech_threshold',
            'compression_ratio_threshold',
            'logprob_threshold',
        ):
            self.assertNotIn(key, data)

    def test_whisper_translations_endpoint_selected_when_translate(self):
        _result, url, data = self._call(
            AsrConfig(api_key='', base_url='http://localhost:8080/v1', translate=True)
        )
        self.assertTrue(url.endswith('/audio/translations'), url)
        self.assertNotIn('timestamp_granularities', data)

    def test_whisper_transcriptions_endpoint_when_not_translating(self):
        _result, url, data = self._call(
            AsrConfig(api_key='', base_url='http://localhost:8080/v1', translate=False)
        )
        self.assertTrue(url.endswith('/audio/transcriptions'), url)
        self.assertEqual(data['timestamp_granularities'], ['segment', 'word'])

    def test_translations_url_builder_normalises_custom_base(self):
        client = AsrApiClient(
            AsrConfig(api_key='', base_url='http://localhost:9000/audio/transcriptions')
        )
        self.assertEqual(
            client._build_whisper_translations_url(),
            'http://localhost:9000/audio/translations',
        )


class WhisperSdkParamsTests(unittest.TestCase):
    def setUp(self):
        handle, self.wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(handle)

    def tearDown(self):
        try:
            os.remove(self.wav_path)
        except OSError:
            pass

    def _client_with_fake_sdk(self, config):
        client = AsrApiClient(config)
        sdk = Mock()
        sdk.audio.transcriptions.create = Mock(return_value={'text': 'x'})
        sdk.audio.translations.create = Mock(return_value={'text': 'x'})
        client.client = sdk
        return client, sdk

    def test_temperature_falls_back_to_config_default(self):
        client, sdk = self._client_with_fake_sdk(AsrConfig(api_key=''))
        client._request_whisper_response(self.wav_path, 'whisper-1', 'text', granularities=tuple())
        _args, kwargs = sdk.audio.transcriptions.create.call_args
        self.assertEqual(kwargs['temperature'], 0.0)

    def test_explicit_temperature_wins(self):
        client, sdk = self._client_with_fake_sdk(AsrConfig(api_key='', temperature=0.9))
        client._request_whisper_response(
            self.wav_path, 'whisper-1', 'verbose_json',
            granularities=('segment',), temperature=0,
        )
        _args, kwargs = sdk.audio.transcriptions.create.call_args
        self.assertEqual(kwargs['temperature'], 0)

    def test_none_temperature_config_is_omitted(self):
        client, sdk = self._client_with_fake_sdk(AsrConfig(api_key='', temperature=None))
        client._request_whisper_response(self.wav_path, 'whisper-1', 'text', granularities=tuple())
        _args, kwargs = sdk.audio.transcriptions.create.call_args
        self.assertNotIn('temperature', kwargs)

    def test_sdk_translation_call_omits_granularities(self):
        client, sdk = self._client_with_fake_sdk(AsrConfig(api_key='', translate=True))
        client._request_whisper_response(
            self.wav_path, 'whisper-1', 'verbose_json', granularities=('segment', 'word'),
        )
        _args, kwargs = sdk.audio.translations.create.call_args
        self.assertNotIn('timestamp_granularities', kwargs)


class VoxtralLanguageHintTests(unittest.TestCase):
    def setUp(self):
        handle, self.wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(handle)

    def tearDown(self):
        try:
            os.remove(self.wav_path)
        except OSError:
            pass

    def test_language_sent_even_with_granularities(self):
        client = AsrApiClient(
            AsrConfig(
                api_key='k',
                provider='voxtral',
                base_url='http://localhost:8080/v1',
                language='zh',
            )
        )
        client._language_hint = 'zh'
        fake_post = Mock(return_value=_FakeResponse({
            'text': '你好',
            'language': 'zh',
            'duration': 1.0,
            'segments': [{'id': 0, 'start': 0.0, 'end': 1.0, 'text': '你好'}],
        }))
        with patch('modules.asr_api_client.requests.post', fake_post), \
                patch.object(AsrApiClient, '_probe_wav_duration', return_value=1.0):
            result = client._transcribe_segment_voxtral(
                self.wav_path,
                window=None,
                granularity_candidates=(('segment',),),
                include_language_hint=True,
            )
        self.assertTrue(result.ok)
        _args, kwargs = fake_post.call_args
        data = _data_to_dict(kwargs['data'])
        self.assertEqual(data.get('language'), 'zh')
        self.assertEqual(data.get('timestamp_granularities'), 'segment')


class WindowFailureAccountingTests(unittest.TestCase):
    def _run(self, fail_count, window_count):
        config = AsrConfig(api_key='', max_workers=4)
        client = AsrApiClient(config)
        # Pretend capability negotiation already happened so no serial probe
        # consumes the first window.
        client._capability_cache.transcription_format = 'verbose_json'

        windows = [
            (
                DetectedSpeechWindow(
                    start_s=float(i),
                    end_s=float(i) + 1.0,
                    ownership_start_s=float(i),
                    ownership_end_s=float(i) + 1.0,
                ),
                f'w{i}.wav',
            )
            for i in range(window_count)
        ]

        call_counter = {'n': 0}

        def fake_transcribe_window(wav_path, window=None, segment_info=None):
            index = call_counter['n']
            call_counter['n'] += 1
            if index < fail_count:
                return AsrTranscriptionResult(
                    provider='whisper',
                    response_format='',
                    timestamp_mode='none',
                    window=window,
                    failure_token='asr_failed',
                )
            return AsrTranscriptionResult(
                provider='whisper',
                response_format='verbose_json',
                timestamp_mode='segment',
                text='ok',
                segments=[AsrSegmentTiming(start_s=0.0, end_s=1.0, text='ok')],
                window=window,
            )

        client.transcribe_window = fake_transcribe_window
        results = client.transcribe_windows_concurrent(windows)
        return client, results

    def test_twenty_percent_failures_abort_batch(self):
        client, results = self._run(fail_count=4, window_count=20)

        self.assertEqual(len(results), 20)
        # Threshold is max(3, int(20 * 0.15)) == 3, so the batch aborts on the
        # third observed failure and the remaining windows get failure stubs.
        self.assertEqual(client.last_window_count, 20)
        self.assertEqual(client.last_failed_count, 3)
        self.assertAlmostEqual(client.last_failure_ratio, 3 / 20)
        self.assertGreaterEqual(client.last_failure_ratio, 0.15)
        self.assertTrue(any(r.failure_token == 'asr_failed' and not r.ok for r in results))

    def test_no_failures_report_zero_ratio(self):
        client, results = self._run(fail_count=0, window_count=20)

        self.assertEqual(len(results), 20)
        self.assertEqual(client.last_failed_count, 0)
        self.assertEqual(client.last_window_count, 20)
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertTrue(all(r.ok for r in results))

    def test_empty_batch_resets_counters(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        client.last_failure_ratio = 0.9
        client.last_failed_count = 9
        client.last_window_count = 10

        self.assertEqual(client.transcribe_windows_concurrent([]), [])
        self.assertEqual(client.last_failure_ratio, 0.0)
        self.assertEqual(client.last_failed_count, 0)
        self.assertEqual(client.last_window_count, 0)


class LanguageSamplingTests(unittest.TestCase):
    def test_five_evenly_spread_samples_are_used(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        segments = [(float(i), float(i) + 1.0) for i in range(40)]
        clipped = []

        def extract_clip(_audio_wav, start_s, end_s):
            clipped.append((start_s, end_s))
            return f'{start_s:.1f}-{end_s:.1f}'

        client.detect_language = lambda clip: 'en'

        language = client.detect_language_from_segments('audio.wav', segments, extract_clip)

        self.assertEqual(language, 'en')
        self.assertEqual(len(clipped), 5)
        self.assertEqual(clipped[0][0], 0.0)
        self.assertEqual(clipped[1][0], 10.0)
        self.assertEqual(clipped[2][0], 20.0)
        self.assertEqual(clipped[3][0], 30.0)
        self.assertEqual(clipped[4][0], 39.0)

    def test_duplicate_indices_collapse_for_short_input(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        segments = [(float(i), float(i) + 1.0) for i in range(2)]
        clipped = []

        def extract_clip(_audio_wav, start_s, end_s):
            clipped.append((start_s, end_s))
            return f'{start_s:.1f}-{end_s:.1f}'

        client.detect_language = lambda clip: 'zh'

        language = client.detect_language_from_segments('audio.wav', segments, extract_clip)

        self.assertEqual(language, 'zh')
        self.assertEqual(len(clipped), 2)

    def test_empty_detection_logs_warning_and_returns_empty_string(self):
        logger = Mock()
        client = AsrApiClient(AsrConfig(api_key=''), logger=logger)
        client.detect_language = lambda clip: ''

        def extract_clip(_audio_wav, start_s, end_s):
            return 'clip'

        language = client.detect_language_from_segments('audio.wav', [(0.0, 1.0)], extract_clip)

        self.assertEqual(language, '')
        self.assertTrue(logger.warning.called)
        self.assertIn('Language detection', logger.warning.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
