import logging
import threading
import unittest
from types import SimpleNamespace

from modules import utils
from modules.subtitle_translator import LLMRequester


class _CompatError(RuntimeError):
    def __init__(self, message, status_code=400, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _FakeCompletions:
    def __init__(self, responder=None):
        self.calls = []
        self._responder = responder

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._responder:
            return self._responder(kwargs, len(self.calls))
        return SimpleNamespace(choices=[])


class _FakeClient:
    def __init__(self, base_url='https://gateway.example/v1', responder=None):
        self.base_url = base_url
        self.completions = _FakeCompletions(responder)
        self.chat = SimpleNamespace(completions=self.completions)


def _base_kwargs(**overrides):
    kwargs = {
        'model': 'test-model',
        'messages': [
            {'role': 'system', 'content': 'Return JSON only.'},
            {'role': 'user', 'content': '{"text":"hello"}'},
        ],
        'max_tokens': 100,
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
    }
    kwargs.update(overrides)
    return kwargs


class OpenAIChatCompatibilityTests(unittest.TestCase):
    def setUp(self):
        with utils._OPENAI_COMPATIBILITY_LOCK:
            utils._OPENAI_COMPATIBILITY_CACHE.clear()

    def test_non_deepseek_request_does_not_receive_private_thinking_parameter(self):
        client = _FakeClient()

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(), thinking_enabled=False,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_deepseek_request_receives_disable_thinking_parameter(self):
        client = _FakeClient(base_url='https://api.deepseek.com/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='deepseek-chat'), thinking_enabled=False,
        )

        self.assertEqual(
            client.completions.calls[0]['extra_body']['thinking'],
            {'type': 'disabled', 'enabled': False},
        )

    def test_enabled_thinking_does_not_inject_or_disable_provider_parameter(self):
        client = _FakeClient(base_url='https://api.deepseek.com/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='deepseek-reasoner'), thinking_enabled=True,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_deepseek_thinking_rejection_retries_without_private_parameter(self):
        def responder(kwargs, _call_number):
            if 'extra_body' in kwargs:
                raise _CompatError("Unknown parameter: 'thinking'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(
            base_url='https://api.deepseek.com/v1',
            responder=responder,
        )

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='deepseek-chat'), thinking_enabled=False,
        )

        self.assertEqual(len(client.completions.calls), 2)
        self.assertIn('extra_body', client.completions.calls[0])
        self.assertNotIn('extra_body', client.completions.calls[1])

    def test_retries_without_unsupported_response_format_and_caches_capability(self):
        def responder(kwargs, _call_number):
            if 'response_format' in kwargs:
                raise _CompatError("Unsupported parameter: 'response_format'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)
        logger = logging.getLogger('test_openai_compat_response_format')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(), logger=logger,
        )
        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(), logger=logger,
        )

        self.assertEqual(len(client.completions.calls), 3)
        self.assertIn('response_format', client.completions.calls[0])
        self.assertNotIn('response_format', client.completions.calls[1])
        self.assertNotIn('response_format', client.completions.calls[2])

    def test_switches_from_max_tokens_to_max_completion_tokens(self):
        def responder(kwargs, _call_number):
            if 'max_tokens' in kwargs:
                raise _CompatError(
                    "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
                )
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 2)
        self.assertEqual(client.completions.calls[1]['max_completion_tokens'], 100)
        self.assertNotIn('max_tokens', client.completions.calls[1])

    def test_switches_from_max_completion_tokens_to_legacy_max_tokens(self):
        def responder(kwargs, _call_number):
            if 'max_completion_tokens' in kwargs:
                raise _CompatError("Unknown parameter: 'max_completion_tokens'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)
        kwargs = _base_kwargs()
        kwargs.pop('max_tokens')
        kwargs['max_completion_tokens'] = 100

        utils.openai_chat_create_with_thinking_control(
            client,
            kwargs,
        )

        first_call = client.completions.calls[0]
        self.assertIn('max_completion_tokens', first_call)
        self.assertEqual(client.completions.calls[1]['max_tokens'], 100)

    def test_removes_unsupported_temperature(self):
        def responder(kwargs, _call_number):
            if 'temperature' in kwargs:
                raise _CompatError(
                    "Unsupported value: 'temperature' does not support 0.2 with this model."
                )
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 2)
        self.assertNotIn('temperature', client.completions.calls[1])

    def test_system_role_falls_back_to_developer_then_inline_user_instruction(self):
        def responder(kwargs, _call_number):
            roles = [message['role'] for message in kwargs['messages']]
            if 'system' in roles:
                raise _CompatError(
                    "Unsupported value: 'messages[0].role' does not support 'system' with this model."
                )
            if 'developer' in roles:
                raise _CompatError(
                    "Unsupported value: 'messages[0].role' does not support 'developer' with this model."
                )
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 3)
        self.assertEqual(client.completions.calls[1]['messages'][0]['role'], 'developer')
        self.assertEqual(
            [message['role'] for message in client.completions.calls[2]['messages']],
            ['user'],
        )
        self.assertIn('Return JSON only.', client.completions.calls[2]['messages'][0]['content'])

    def test_multiple_explicit_parameter_errors_are_adapted_sequentially(self):
        def responder(kwargs, _call_number):
            if 'response_format' in kwargs:
                raise _CompatError("Unknown parameter: 'response_format'")
            if 'max_tokens' in kwargs:
                raise _CompatError("Unsupported parameter: 'max_tokens'")
            if 'temperature' in kwargs:
                raise _CompatError("Unsupported parameter: 'temperature'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 4)
        final_call = client.completions.calls[-1]
        self.assertNotIn('response_format', final_call)
        self.assertNotIn('max_tokens', final_call)
        self.assertEqual(final_call['max_completion_tokens'], 100)
        self.assertNotIn('temperature', final_call)

    def test_authentication_error_is_not_retried(self):
        def responder(_kwargs, _call_number):
            raise _CompatError('Incorrect API key provided', status_code=401)

        client = _FakeClient(responder=responder)

        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 1)

    def test_server_error_is_not_retried_as_parameter_compatibility(self):
        def responder(_kwargs, _call_number):
            raise _CompatError('Internal server error mentioning max_tokens', status_code=500)

        client = _FakeClient(responder=responder)

        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 1)


class OpenAIResponseCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _make_requester(client):
        requester = LLMRequester.__new__(LLMRequester)
        requester.client = client
        requester.openai_config = {
            'OPENAI_MODEL_NAME': 'test-model',
            'OPENAI_THINKING_ENABLED': False,
        }
        requester._log_lock = threading.Lock()
        requester._capability_lock = threading.Lock()
        requester._json_mode_disabled = False
        requester.logger = logging.getLogger('test_translation_compatibility')
        return requester

    def test_extracts_mapping_message_and_nested_text_content_parts(self):
        message = {
            'content': [
                {'type': 'text', 'text': {'value': '{"translations":'}},
                {'type': 'text', 'text': '["你好"]}'},
            ]
        }

        self.assertEqual(
            utils.extract_chat_message_json(message),
            {'translations': ['你好']},
        )

    def test_translation_parser_accepts_top_level_array(self):
        requester = LLMRequester.__new__(LLMRequester)
        requester._log_lock = threading.Lock()
        requester.logger = logging.getLogger('test_translation_array')

        parsed = requester._parse_structured_translation_result(
            {'content': '["你好", "世界"]'}, 2, 'array',
        )

        self.assertEqual(parsed, ['你好', '世界'])

    def test_translation_parser_accepts_result_objects(self):
        requester = LLMRequester.__new__(LLMRequester)
        requester._log_lock = threading.Lock()
        requester.logger = logging.getLogger('test_translation_result_objects')

        parsed = requester._parse_structured_translation_result(
            {
                'content': (
                    '{"results":['
                    '{"translated_text":"第一句"},'
                    '{"translation":"第二句"}'
                    ']}'
                )
            },
            2,
            'objects',
        )

        self.assertEqual(parsed, ['第一句', '第二句'])

    def test_translation_parser_accepts_numbered_plain_text(self):
        requester = LLMRequester.__new__(LLMRequester)
        requester._log_lock = threading.Lock()
        requester.logger = logging.getLogger('test_translation_numbered')

        parsed = requester._parse_structured_translation_result(
            {'content': '1. 第一句\n2. 第二句'}, 2, 'numbered',
        )

        self.assertEqual(parsed, ['第一句', '第二句'])

    def test_unparseable_json_mode_retries_plain_and_reuses_working_mode(self):
        def responder(kwargs, call_number):
            if call_number == 1:
                content = 'Sorry,\nI cannot return that format.'
            else:
                content = '{"translations":["你好"]}'
            return SimpleNamespace(
                choices=[SimpleNamespace(message={'content': content})]
            )

        client = _FakeClient(responder=responder)
        requester = self._make_requester(client)

        first = requester._request_translation_result(
            model_name='test-model',
            system_prompt='Translate and return JSON.',
            user_prompt='hello',
            expected_count=1,
            batch_id='first',
            scene_name='subtitle_test',
        )
        second = requester._request_translation_result(
            model_name='test-model',
            system_prompt='Translate and return JSON.',
            user_prompt='hello',
            expected_count=1,
            batch_id='second',
            scene_name='subtitle_test',
        )

        self.assertEqual(first, ['你好'])
        self.assertEqual(second, ['你好'])
        self.assertEqual(len(client.completions.calls), 3)
        self.assertIn('response_format', client.completions.calls[0])
        self.assertNotIn('response_format', client.completions.calls[1])
        self.assertNotIn('response_format', client.completions.calls[2])


if __name__ == '__main__':
    unittest.main()
