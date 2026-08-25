import logging
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

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
            utils._OPENAI_COMPATIBILITY_WARNED.clear()

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

    def test_all_output_token_limits_are_removed_before_request(self):
        client = _FakeClient()
        kwargs = _base_kwargs(
            max_tokens=160,
            max_completion_tokens=320,
            max_output_tokens=640,
        )

        utils.openai_chat_create_with_thinking_control(
            client,
            kwargs,
            thinking_enabled=True,
        )

        request = client.completions.calls[0]
        self.assertNotIn('max_tokens', request)
        self.assertNotIn('max_completion_tokens', request)
        self.assertNotIn('max_output_tokens', request)

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

    def test_qwen_dashscope_uses_enable_thinking_parameter(self):
        client = _FakeClient(base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='qwen-plus'), thinking_enabled=False,
        )

        self.assertEqual(
            client.completions.calls[0]['extra_body'],
            {'enable_thinking': False},
        )

    def test_qwen_on_unknown_vllm_endpoint_does_not_receive_private_parameter(self):
        client = _FakeClient(base_url='http://model-server.example/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='Qwen/Qwen3-8B'), thinking_enabled=False,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_qwen_proxy_hostname_does_not_trigger_private_parameter(self):
        client = _FakeClient(base_url='https://qwen-proxy.example.com/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='unrelated-model'), thinking_enabled=False,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_openrouter_qwen_model_does_not_receive_dashscope_parameter(self):
        client = _FakeClient(base_url='https://openrouter.ai/api/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='qwen/qwen3-235b-a22b'), thinking_enabled=False,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_known_provider_requires_matching_model_family(self):
        client = _FakeClient(base_url='https://api.xiaomimimo.com/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='unrelated-model'), thinking_enabled=False,
        )

        self.assertNotIn('extra_body', client.completions.calls[0])

    def test_qwen_thinking_rejection_retries_without_private_parameter(self):
        def responder(kwargs, _call_number):
            if 'extra_body' in kwargs:
                raise _CompatError("Unknown parameter: 'enable_thinking'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(
            base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
            responder=responder,
        )

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='qwen-plus'), thinking_enabled=False,
        )

        self.assertEqual(len(client.completions.calls), 2)
        self.assertNotIn('extra_body', client.completions.calls[1])

    def test_mimo_uses_strict_disabled_thinking_object(self):
        client = _FakeClient(base_url='https://api.xiaomimimo.com/v1')

        utils.openai_chat_create_with_thinking_control(
            client, _base_kwargs(model='mimo-v2.5'), thinking_enabled=False,
        )

        self.assertEqual(
            client.completions.calls[0]['extra_body'],
            {'thinking': {'type': 'disabled'}},
        )

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

    def test_bare_quoted_system_word_does_not_trigger_role_fallback(self):
        def responder(_kwargs, _call_number):
            raise _CompatError("Unsupported metadata value 'system'")

        client = _FakeClient(responder=responder)

        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 1)

    def test_inline_instructions_creates_user_message_when_missing(self):
        kwargs = {
            'messages': [
                {'role': 'system', 'content': 'Return JSON only.'},
                {'role': 'assistant', 'content': 'Previous output'},
            ]
        }

        utils._inline_instruction_messages(kwargs)

        self.assertEqual(kwargs['messages'][0], {
            'role': 'user',
            'content': 'Return JSON only.',
        })
        self.assertEqual(kwargs['messages'][1]['role'], 'assistant')

    def test_multiple_explicit_parameter_errors_are_adapted_sequentially(self):
        def responder(kwargs, _call_number):
            if 'response_format' in kwargs:
                raise _CompatError("Unknown parameter: 'response_format'")
            if 'temperature' in kwargs:
                raise _CompatError("Unsupported parameter: 'temperature'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 3)
        final_call = client.completions.calls[-1]
        self.assertNotIn('response_format', final_call)
        self.assertNotIn('max_tokens', final_call)
        self.assertNotIn('max_completion_tokens', final_call)
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

    def test_not_found_is_not_retried_as_parameter_compatibility(self):
        def responder(_kwargs, _call_number):
            raise _CompatError("Unknown parameter: 'response_format'", status_code=404)

        client = _FakeClient(responder=responder)

        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 1)

    def test_generic_schema_error_falls_back_to_minimal_request_and_caches_success(self):
        def responder(kwargs, _call_number):
            if set(kwargs) != {'model', 'messages'}:
                raise _CompatError('Param Incorrect: request schema validation failed')
            roles = [message['role'] for message in kwargs['messages']]
            if roles != ['user']:
                raise _CompatError('Param Incorrect: message schema validation failed')
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)

        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())
        utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 3)
        minimal_call = client.completions.calls[1]
        self.assertEqual(set(minimal_call), {'model', 'messages'})
        self.assertEqual(
            [message['role'] for message in minimal_call['messages']],
            ['user'],
        )
        self.assertIn('Return JSON only.', minimal_call['messages'][0]['content'])
        self.assertEqual(set(client.completions.calls[2]), {'model', 'messages'})

    def test_failed_compatibility_attempt_is_not_cached(self):
        def failing_responder(kwargs, _call_number):
            if 'response_format' in kwargs:
                raise _CompatError("Unsupported parameter: 'response_format'")
            raise _CompatError('Incorrect API key provided', status_code=401)

        failing_client = _FakeClient(responder=failing_responder)
        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(
                failing_client,
                _base_kwargs(),
            )

        succeeding_client = _FakeClient()
        utils.openai_chat_create_with_thinking_control(
            succeeding_client,
            _base_kwargs(),
        )

        self.assertIn('response_format', succeeding_client.completions.calls[0])

    def test_generic_5xx_schema_text_does_not_trigger_minimal_retry(self):
        def responder(_kwargs, _call_number):
            raise _CompatError(
                'Internal server error: request schema unavailable',
                status_code=500,
            )

        client = _FakeClient(responder=responder)

        with self.assertRaises(_CompatError):
            utils.openai_chat_create_with_thinking_control(client, _base_kwargs())

        self.assertEqual(len(client.completions.calls), 1)

    def test_fallback_warning_includes_scene_name(self):
        def responder(kwargs, _call_number):
            if 'response_format' in kwargs:
                raise _CompatError("Unsupported parameter: 'response_format'")
            return SimpleNamespace(choices=[])

        client = _FakeClient(responder=responder)
        logger = logging.getLogger('test_openai_compat_scene')

        with self.assertLogs(logger, level='WARNING') as captured:
            utils.openai_chat_create_with_thinking_control(
                client,
                _base_kwargs(),
                logger=logger,
                scene_name='subtitle_translation',
            )

        self.assertIn('[subtitle_translation]', captured.output[0])

    def test_cached_capabilities_expire(self):
        key = 'https://gateway.example/v1:test-model'
        with patch('modules.utils.time.monotonic', return_value=100.0):
            utils._cache_compatibility_actions(key, {'drop_response_format'})
            self.assertEqual(
                utils._get_cached_compatibility_actions(key),
                {'drop_response_format'},
            )

        with patch(
            'modules.utils.time.monotonic',
            return_value=100.0 + utils._OPENAI_COMPATIBILITY_CACHE_TTL_SECONDS + 1,
        ):
            self.assertEqual(utils._get_cached_compatibility_actions(key), set())

    def test_concurrent_cache_reads_and_writes_preserve_actions(self):
        key = 'https://gateway.example/v1:concurrent-model'
        actions = ('drop_response_format', 'drop_temperature')

        def update_cache(index):
            utils._cache_compatibility_actions(key, {actions[index % 2]})
            return utils._get_cached_compatibility_actions(key)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(update_cache, range(64)))

        self.assertTrue(all(isinstance(result, set) for result in results))
        self.assertEqual(utils._get_cached_compatibility_actions(key), set(actions))


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

    def test_normalizes_full_chat_completions_url_for_openai_sdk(self):
        self.assertEqual(
            utils.normalize_openai_base_url(
                'https://gateway.example/custom/v1/chat/completions/'
            ),
            'https://gateway.example/custom/v1',
        )
        self.assertEqual(
            utils.normalize_openai_base_url('https://gateway.example/custom/v1/'),
            'https://gateway.example/custom/v1',
        )

    def test_base_url_with_query_is_preserved_and_warned(self):
        url = 'https://azure.example/openai/deployments/demo/chat/completions?api-version=2024-10-21'

        with self.assertLogs(utils._OPENAI_URL_LOGGER, level='WARNING') as captured:
            normalized = utils.normalize_openai_base_url(url)

        self.assertEqual(normalized, url)
        self.assertIn('含查询参数', captured.output[0])

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

    def test_valid_empty_translation_does_not_retry_plain_json(self):
        def responder(_kwargs, _call_number):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message={'content': '{"translations":[""]}'},
                )]
            )

        client = _FakeClient(responder=responder)
        requester = self._make_requester(client)

        result = requester._request_translation_result(
            model_name='test-model',
            system_prompt='Translate and return JSON.',
            user_prompt='hello',
            expected_count=1,
            batch_id='empty',
            scene_name='subtitle_test',
        )

        self.assertEqual(result, [''])
        self.assertEqual(len(client.completions.calls), 1)
        self.assertFalse(requester._json_mode_disabled)


if __name__ == '__main__':
    unittest.main()
