import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules import ai_fallback_client as afc
from modules import utils


class _FakeResource:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeRawClient:
    def __init__(self, *, responses_result=None, chat_result=None, error=None):
        self.base_url = 'https://gateway.example/v1/'
        self.responses = _FakeResource(responses_result, error)
        self.chat = SimpleNamespace(
            completions=_FakeResource(chat_result, error),
        )


class _StatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class ResponsesUrlTests(unittest.TestCase):
    def test_full_responses_url_is_detected_and_normalized(self):
        endpoint = afc._build_endpoint('OPENAI_', {
            'OPENAI_API_KEY': 'test-key',
            'OPENAI_BASE_URL': 'https://gateway.example/custom/v1/responses/',
            'OPENAI_MODEL_NAME': 'test-model',
        })

        self.assertEqual(endpoint['api_mode'], 'responses')
        self.assertEqual(endpoint['base_url'], 'https://gateway.example/custom/v1')
        self.assertEqual(
            utils.normalize_openai_base_url('https://gateway.example/v1/responses'),
            'https://gateway.example/v1',
        )

    def test_root_url_remains_chat_completions_for_backward_compatibility(self):
        endpoint = afc._build_endpoint('OPENAI_', {
            'OPENAI_API_KEY': 'test-key',
            'OPENAI_BASE_URL': 'https://gateway.example/v1',
        })

        self.assertEqual(endpoint['api_mode'], 'chat_completions')


class ResponsesRequestTests(unittest.TestCase):
    def _config(self, **overrides):
        config = {
            'OPENAI_API_KEY': 'test-key',
            'OPENAI_BASE_URL': 'https://gateway.example/v1/responses',
            'OPENAI_MODEL_NAME': 'response-model',
        }
        config.update(overrides)
        return config

    def test_single_endpoint_converts_request_and_normalizes_output(self):
        raw_response = {
            'id': 'resp_test',
            'model': 'response-model',
            'usage': {'total_tokens': 12},
            'output': [{
                'type': 'message',
                'role': 'assistant',
                'content': [
                    {'type': 'output_text', 'text': '{"translations":["你好"]}'},
                ],
            }],
        }
        raw_client = _FakeRawClient(responses_result=raw_response)

        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', return_value=raw_client):
            client = afc.get_ai_client(self._config())

        response = client.chat.completions.create(
            model='caller-model',
            messages=[
                {'role': 'system', 'content': 'Return JSON only.'},
                {'role': 'user', 'content': 'hello'},
            ],
            max_tokens=256,
            response_format={'type': 'json_object'},
            temperature=0.2,
        )

        self.assertIsInstance(client, afc.ResponsesChatClient)
        self.assertEqual(len(raw_client.responses.calls), 1)
        request = raw_client.responses.calls[0]
        self.assertEqual(request['model'], 'response-model')
        self.assertEqual(request['instructions'], 'Return JSON only.')
        self.assertEqual(request['input'], [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(request['max_output_tokens'], 256)
        self.assertEqual(request['text'], {'format': {'type': 'json_object'}})
        self.assertEqual(request['temperature'], 0.2)
        self.assertIs(request['store'], False)
        self.assertNotIn('messages', request)
        self.assertNotIn('max_tokens', request)
        self.assertNotIn('response_format', request)
        self.assertEqual(
            utils.extract_chat_message_json(response.choices[0].message),
            {'translations': ['你好']},
        )
        self.assertIs(response.raw_response, raw_response)

    def test_sdk_output_text_property_is_used(self):
        raw_response = SimpleNamespace(
            id='resp_property',
            model='response-model',
            usage=None,
            output_text='plain response text',
            output=[],
        )
        normalized = afc._as_chat_completion(raw_response)

        self.assertEqual(normalized.choices[0].message.content, 'plain response text')

    def test_multimodal_content_and_json_schema_are_converted(self):
        request = afc._responses_create_kwargs({
            'model': 'vision-model',
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'describe this image'},
                    {
                        'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,abc', 'detail': 'low'},
                    },
                ],
            }],
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'result',
                    'schema': {'type': 'object'},
                    'strict': True,
                },
            },
        })

        self.assertEqual(request['input'][0]['content'], [
            {'type': 'input_text', 'text': 'describe this image'},
            {
                'type': 'input_image',
                'image_url': 'data:image/png;base64,abc',
                'detail': 'low',
            },
        ])
        self.assertEqual(request['text']['format'], {
            'type': 'json_schema',
            'name': 'result',
            'schema': {'type': 'object'},
            'strict': True,
        })

    def test_explicit_store_setting_is_preserved(self):
        request = afc._responses_create_kwargs({
            'model': 'response-model',
            'messages': [{'role': 'user', 'content': 'hello'}],
            'store': True,
        })

        self.assertIs(request['store'], True)

    def test_fallback_can_switch_from_responses_to_chat_completions(self):
        primary = _FakeRawClient(error=_StatusError('service unavailable', 503))
        chat_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='fallback'))],
        )
        fallback = _FakeRawClient(chat_result=chat_response)

        def make_client(endpoint, **_kwargs):
            return primary if endpoint['api_mode'] == 'responses' else fallback

        config = self._config(
            FALLBACK_OPENAI_API_KEY='fallback-key',
            FALLBACK_OPENAI_BASE_URL='https://fallback.example/v1/chat/completions',
            FALLBACK_OPENAI_MODEL_NAME='fallback-model',
        )
        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', side_effect=make_client):
            client = afc.get_ai_client(config)

        response = client.chat.completions.create(
            model='ignored',
            messages=[{'role': 'user', 'content': 'hello'}],
        )

        self.assertEqual(response.choices[0].message.content, 'fallback')
        self.assertEqual(primary.responses.calls[0]['model'], 'response-model')
        self.assertEqual(fallback.chat.completions.calls[0]['model'], 'fallback-model')

    def test_failed_responses_server_error_switches_to_fallback(self):
        primary = _FakeRawClient(responses_result={
            'id': 'resp_failed',
            'status': 'failed',
            'error': {'code': 'server_error', 'message': 'provider unavailable'},
            'output': [],
        })
        chat_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='fallback'))],
        )
        fallback = _FakeRawClient(chat_result=chat_response)

        def make_client(endpoint, **_kwargs):
            return primary if endpoint['api_mode'] == 'responses' else fallback

        config = self._config(
            FALLBACK_OPENAI_API_KEY='fallback-key',
            FALLBACK_OPENAI_BASE_URL='https://fallback.example/v1/chat/completions',
            FALLBACK_OPENAI_MODEL_NAME='fallback-model',
        )
        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', side_effect=make_client):
            client = afc.get_ai_client(config)

        response = client.chat.completions.create(
            model='ignored',
            messages=[{'role': 'user', 'content': 'hello'}],
        )

        self.assertEqual(response.choices[0].message.content, 'fallback')
        self.assertEqual(len(fallback.chat.completions.calls), 1)

    def test_failed_responses_request_error_does_not_switch_to_fallback(self):
        primary = _FakeRawClient(responses_result={
            'id': 'resp_failed',
            'status': 'failed',
            'error': {'code': 'invalid_prompt', 'message': 'invalid input'},
            'output': [],
        })
        fallback = _FakeRawClient(chat_result=SimpleNamespace(choices=[]))

        def make_client(endpoint, **_kwargs):
            return primary if endpoint['api_mode'] == 'responses' else fallback

        config = self._config(
            FALLBACK_OPENAI_API_KEY='fallback-key',
            FALLBACK_OPENAI_BASE_URL='https://fallback.example/v1/chat/completions',
        )
        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', side_effect=make_client):
            client = afc.get_ai_client(config)

        with self.assertRaises(afc.ResponsesResultError) as raised:
            client.chat.completions.create(
                model='ignored',
                messages=[{'role': 'user', 'content': 'hello'}],
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(fallback.chat.completions.calls, [])

    def test_empty_fallback_url_inherits_primary_responses_protocol(self):
        config = self._config(
            FALLBACK_OPENAI_API_KEY='fallback-key',
            FALLBACK_OPENAI_BASE_URL='',
            FALLBACK_OPENAI_MODEL_NAME='',
        )
        with patch.object(afc, '_load_global_config', return_value={}), \
             patch.object(afc, '_make_raw_client', side_effect=lambda ep, **_kw: ep):
            client = afc.get_ai_client(config)

        self.assertEqual(client._endpoints[1]['api_mode'], 'responses')
        self.assertEqual(client._endpoints[1]['base_url'], 'https://gateway.example/v1')


if __name__ == '__main__':
    unittest.main()
