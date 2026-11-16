"""Focused regressions for reasoning controls and Responses adaptation."""
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openai import APIStatusError

from modules import ai_fallback_client as afc
from modules import utils


class _StatusError(APIStatusError):
    def __init__(self, message, status_code):
        self.status_code = status_code
        self.message = message
        self.request = None
        self.body = None

    def __str__(self):
        return self.message


class _Completions:
    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.responder:
            return self.responder(kwargs, len(self.calls))
        return SimpleNamespace(choices=[])


class _RawClient:
    def __init__(self, base_url='https://example.test/v1', responder=None):
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=_Completions(responder))
        self.responses = SimpleNamespace(create=self._responses_create)
        self.response_calls = []
        self.response_result = {
            'id': 'resp_ok', 'status': 'completed', 'error': None,
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'ok'}]}],
        }

    def _responses_create(self, **kwargs):
        self.response_calls.append(dict(kwargs))
        return self.response_result


class ReasoningControlTests(unittest.TestCase):
    def setUp(self):
        with utils._OPENAI_COMPATIBILITY_LOCK:
            utils._OPENAI_COMPATIBILITY_CACHE.clear()
            utils._OPENAI_COMPATIBILITY_WARNED.clear()

    def tearDown(self):
        with utils._OPENAI_COMPATIBILITY_LOCK:
            utils._OPENAI_COMPATIBILITY_CACHE.clear()
            utils._OPENAI_COMPATIBILITY_WARNED.clear()

    def test_reasoning_effort_normalization_allowlist(self):
        self.assertEqual(utils.normalize_reasoning_effort(' HIGH '), 'high')
        self.assertEqual(utils.normalize_reasoning_effort('none'), 'auto')
        self.assertEqual(utils.normalize_reasoning_effort('untrusted'), 'auto')

    def test_off_flag_removes_conflicting_explicit_controls(self):
        client = _RawClient('https://api.deepseek.com/v1')
        utils.openai_chat_create_with_thinking_control(
            client,
            {
                'model': 'deepseek-reasoner',
                'messages': [{'role': 'user', 'content': 'hello'}],
                'reasoning_effort': 'high',
                'reasoning': {'effort': 'high', 'summary': 'auto'},
                'extra_body': {
                    'thinking': {'enabled': True},
                    'enable_thinking': True,
                    'chat_template_kwargs': {'enable_thinking': True},
                },
            },
            thinking_enabled=False,
        )
        request = client.chat.completions.calls[-1]
        self.assertNotIn('reasoning_effort', request)
        self.assertNotIn('reasoning', request)
        self.assertEqual(request['extra_body'], {'thinking': {'type': 'disabled'}})

    def test_enabled_effort_is_sent_for_chat_and_responses(self):
        chat = _RawClient()
        chat.reasoning_effort = 'high'
        utils.openai_chat_create_with_thinking_control(
            chat, {'model': 'm', 'messages': []}, thinking_enabled=True
        )
        self.assertEqual(chat.chat.completions.calls[0]['reasoning_effort'], 'high')

        response_raw = _RawClient()
        endpoint = {
            'api_mode': 'responses', 'model': 'response-model',
            'reasoning_effort': 'medium', 'base_url': 'https://example.test/v1',
        }
        client = afc.ResponsesChatClient(response_raw, endpoint)
        utils.openai_chat_create_with_thinking_control(
            client, {'model': 'ignored', 'messages': []}, thinking_enabled=True
        )
        self.assertEqual(response_raw.response_calls[0]['reasoning'], {'effort': 'medium'})

    def test_fallback_uses_its_own_model_and_effort(self):
        primary = _RawClient('https://primary.test/v1')
        fallback = _RawClient('https://fallback.test/v1')

        def make_client(endpoint, **_kwargs):
            return fallback if endpoint['model'] == 'fallback-model' else primary

        config = {
            'OPENAI_API_KEY': 'not-a-real-key',
            'OPENAI_BASE_URL': 'https://primary.test/v1',
            'OPENAI_MODEL_NAME': 'primary-model',
            'OPENAI_REASONING_EFFORT': 'low',
            'FALLBACK_OPENAI_API_KEY': 'not-a-real-fallback-key',
            'FALLBACK_OPENAI_BASE_URL': 'https://fallback.test/v1',
            'FALLBACK_OPENAI_MODEL_NAME': 'fallback-model',
            'FALLBACK_OPENAI_REASONING_EFFORT': 'high',
        }
        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', side_effect=make_client):
            client = afc.get_ai_client(config)
        primary.chat.completions.responder = lambda _kw, _n: (_ for _ in ()).throw(
            _StatusError('service unavailable', 503)
        )
        utils.openai_chat_create_with_thinking_control(
            client,
            {'model': 'caller-model', 'messages': []},
            thinking_enabled=True,
        )
        self.assertEqual(primary.chat.completions.calls[0]['model'], 'primary-model')
        self.assertEqual(primary.chat.completions.calls[0]['reasoning_effort'], 'low')
        self.assertEqual(fallback.chat.completions.calls[0]['model'], 'fallback-model')
        self.assertEqual(fallback.chat.completions.calls[0]['reasoning_effort'], 'high')

    def test_responses_incomplete_content_filter_is_not_stop(self):
        incomplete = {
            'status': 'incomplete',
            'incomplete_details': {'reason': 'content_filter'},
            'output_text': '{"partial":',
            'output': [],
        }
        normalized = afc._as_chat_completion(incomplete)
        self.assertEqual(normalized.choices[0].finish_reason, 'content_filter')
        self.assertNotEqual(normalized.choices[0].finish_reason, 'stop')
        self.assertEqual(normalized.choices[0].message.content, '{"partial":')

    def test_responses_refusal_is_preserved(self):
        response = {
            'status': 'completed',
            'output': [{'type': 'message', 'content': [
                {'type': 'refusal', 'refusal': 'refused'}]}],
        }
        normalized = afc._as_chat_completion(response)
        self.assertEqual(normalized.choices[0].message.refusal, 'refused')
        self.assertEqual(normalized.choices[0].finish_reason, 'stop')

    def test_endpoint_effort_and_connect_timeout_are_normalized(self):
        with patch.object(afc, '_load_global_config', return_value={}):
            endpoint = afc._build_endpoint('OPENAI_', {
                'OPENAI_API_KEY': 'placeholder',
                'OPENAI_REASONING_EFFORT': 'XHIGH',
                'AI_FAILOVER_TIMEOUT_SECONDS': '12.5',
                'OPENAI_BASE_URL': 'https://user:password@example.test/v1?api_key=secret',
            })
        self.assertEqual(endpoint['reasoning_effort'], 'xhigh')
        self.assertEqual(endpoint['connect_timeout'], 12.5)
        self.assertEqual(endpoint['label'], 'openai:example.test')
        self.assertNotIn('password', endpoint['label'])
        self.assertNotIn('secret', endpoint['label'])

    def test_fallback_logs_do_not_include_endpoint_or_exception_text(self):
        primary = _RawClient()
        fallback = _RawClient()
        primary.chat.completions.responder = lambda _kw, _n: (_ for _ in ()).throw(
            _StatusError('request failed https://user:pw@host.test/path?api_key=secret', 503)
        )
        endpoints = [
            {'api_key': 'placeholder', 'base_url': 'https://user:pw@host.test/path?api_key=secret',
             'label': 'openai:https://user:pw@host.test/path?api_key=secret',
             'model': 'p', 'api_mode': 'chat_completions', 'reasoning_effort': 'auto'},
            {'api_key': 'placeholder', 'base_url': 'https://fallback.test/v1',
             'label': 'fallback_openai:https://fallback.test/v1',
             'model': 'f', 'api_mode': 'chat_completions', 'reasoning_effort': 'auto'},
        ]
        with patch.object(afc, '_make_raw_client', side_effect=[primary, fallback]):
            client = afc.FallbackChatClient(endpoints)
        captured = []
        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())
        handler = _Capture()
        afc.logger.addHandler(handler)
        try:
            client.chat.completions.create(model='p', messages=[])
        finally:
            afc.logger.removeHandler(handler)
        log_text = '\n'.join(captured)
        self.assertNotIn('secret', log_text)
        self.assertNotIn('user:pw', log_text)
        self.assertNotIn('request failed', log_text)
        self.assertIn('端点 #1', log_text)


if __name__ == '__main__':
    unittest.main()
