"""Tests for the named OrcaRouter provider wiring.

Covers:
- resolve_orca_llm_settings priority / defaults
- build_ai_openai_config auto-routing when ORCAROUTER_API_KEY is present
- create_translator_from_config with SUBTITLE_API_PROVIDER=orcarouter
- run_subtitle_qc / _call_ai_judge with SUBTITLE_QC_PROVIDER=orcarouter
"""
import unittest
from unittest.mock import patch

from modules.utils import (
    ORCAROUTER_DEFAULT_BASE_URL,
    ORCAROUTER_DEFAULT_MODEL,
    build_ai_openai_config,
    resolve_orca_llm_settings,
)


class ResolveOrcaLlmSettingsTests(unittest.TestCase):
    """Tests for resolve_orca_llm_settings."""

    def test_defaults(self):
        base_url, api_key, model = resolve_orca_llm_settings({})
        self.assertEqual(base_url, ORCAROUTER_DEFAULT_BASE_URL)
        self.assertEqual(api_key, '')
        self.assertEqual(model, ORCAROUTER_DEFAULT_MODEL)

    def test_orca_explicit_overrides(self):
        cfg = {
            'ORCAROUTER_API_KEY': 'sk-orca-test',
            'ORCAROUTER_BASE_URL': 'https://api.orcarouter.ai/v1',
            'ORCAROUTER_MODEL': 'orcarouter/deepseek-v4-pro',
            'OPENAI_API_KEY': 'sk-openai',
            'OPENAI_BASE_URL': 'https://api.openai.com/v1',
        }
        base_url, api_key, model = resolve_orca_llm_settings(cfg)
        self.assertEqual(base_url, 'https://api.orcarouter.ai/v1')
        self.assertEqual(api_key, 'sk-orca-test')
        self.assertEqual(model, 'orcarouter/deepseek-v4-pro')

    def test_falls_back_to_subtitle_and_global(self):
        cfg = {
            'ORCAROUTER_API_KEY': 'sk-orca-test',
            'SUBTITLE_OPENAI_BASE_URL': 'https://sub.example/v1',
            'SUBTITLE_OPENAI_MODEL_NAME': 'sub-model',
        }
        base_url, api_key, model = resolve_orca_llm_settings(cfg)
        self.assertEqual(base_url, 'https://sub.example/v1')
        self.assertEqual(api_key, 'sk-orca-test')
        self.assertEqual(model, 'sub-model')


class BuildAiOpenaiConfigTests(unittest.TestCase):
    """Tests for build_ai_openai_config."""

    def test_without_orca_key_keeps_openai(self):
        cfg = {
            'OPENAI_API_KEY': 'sk-openai',
            'OPENAI_BASE_URL': 'https://api.openai.com/v1',
            'OPENAI_MODEL_NAME': 'gpt-4o-mini',
        }
        result = build_ai_openai_config(cfg)
        self.assertEqual(result['OPENAI_API_KEY'], 'sk-openai')
        self.assertEqual(result['OPENAI_BASE_URL'], 'https://api.openai.com/v1')
        self.assertEqual(result['OPENAI_MODEL_NAME'], 'gpt-4o-mini')

    def test_with_orca_key_routes_to_orca(self):
        cfg = {
            'OPENAI_API_KEY': 'sk-openai',
            'ORCAROUTER_API_KEY': 'sk-orca-test',
            'ORCAROUTER_MODEL': 'orcarouter/auto',
        }
        result = build_ai_openai_config(cfg)
        self.assertEqual(result['OPENAI_API_KEY'], 'sk-orca-test')
        self.assertEqual(result['OPENAI_BASE_URL'], ORCAROUTER_DEFAULT_BASE_URL)
        self.assertEqual(result['OPENAI_MODEL_NAME'], ORCAROUTER_DEFAULT_MODEL)


class SubtitleTranslatorOrcaTests(unittest.TestCase):
    """Tests for create_translator_from_config with SUBTITLE_API_PROVIDER=orcarouter."""

    def _config(self, **overrides):
        cfg = {
            'SUBTITLE_API_PROVIDER': 'orcarouter',
            'ORCAROUTER_API_KEY': 'sk-orca-test',
            'ORCAROUTER_BASE_URL': 'https://api.orcarouter.ai/v1',
            'ORCAROUTER_MODEL': 'orcarouter/auto',
            'SUBTITLE_TRANSLATION_ENABLED': True,
            'SUBTITLE_BATCH_SIZE': 3,
            'SUBTITLE_MAX_RETRIES': 3,
            'SUBTITLE_RETRY_DELAY': 2,
            'SUBTITLE_MAX_WORKERS': 2,
            'SUBTITLE_OPENAI_THINKING_ENABLED': False,
            'OPENAI_TIMEOUT_SECONDS': 600,
        }
        cfg.update(overrides)
        return cfg

    def test_translator_resolves_orca(self):
        from modules.subtitle_translator import create_translator_from_config

        translator = create_translator_from_config(self._config(), task_id='t1')
        self.assertIsNotNone(translator)
        self.assertEqual(translator.config.api_provider, 'orcarouter')
        self.assertEqual(translator.config.base_url, 'https://api.orcarouter.ai/v1')
        self.assertEqual(translator.config.api_key, 'sk-orca-test')
        self.assertEqual(translator.config.model_name, 'orcarouter/auto')

    def test_translator_openai_does_not_auto_route_with_orca_key(self):
        from modules.subtitle_translator import create_translator_from_config

        # SUBTITLE_API_PROVIDER=openai 时不因全局 ORCAROUTER_API_KEY 而自动路由，
        # 命名 OrcaRouter 接入需显式选择 orcarouter 提供商。
        cfg = self._config(SUBTITLE_API_PROVIDER='openai', OPENAI_API_KEY='sk-openai')
        translator = create_translator_from_config(cfg, task_id='t2')
        self.assertIsNotNone(translator)
        self.assertEqual(translator.config.api_provider, 'openai')
        self.assertEqual(translator.config.base_url, 'https://api.openai.com/v1')
        self.assertEqual(translator.config.api_key, 'sk-openai')

    def test_translator_openai_without_orca_key(self):
        from modules.subtitle_translator import create_translator_from_config

        cfg = {
            'SUBTITLE_API_PROVIDER': 'openai',
            'OPENAI_API_KEY': 'sk-openai',
            'OPENAI_BASE_URL': 'https://api.openai.com/v1',
            'OPENAI_MODEL_NAME': 'gpt-4o-mini',
            'SUBTITLE_BATCH_SIZE': 3,
            'SUBTITLE_MAX_RETRIES': 3,
            'SUBTITLE_RETRY_DELAY': 2,
            'SUBTITLE_MAX_WORKERS': 2,
        }
        translator = create_translator_from_config(cfg, task_id='t3')
        self.assertIsNotNone(translator)
        self.assertEqual(translator.config.api_provider, 'openai')
        self.assertEqual(translator.config.base_url, 'https://api.openai.com/v1')


class SubtitleQcOrcaTests(unittest.TestCase):
    """Tests for run_subtitle_qc / _call_ai_judge with SUBTITLE_QC_PROVIDER=orcarouter."""

    @staticmethod
    def _qc_config(**overrides):
        cfg = {
            'SUBTITLE_QC_PROVIDER': 'orcarouter',
            'ORCAROUTER_API_KEY': 'sk-orca-test',
            'ORCAROUTER_BASE_URL': 'https://api.orcarouter.ai/v1',
            'ORCAROUTER_MODEL': 'orcarouter/auto',
            'SUBTITLE_QC_THRESHOLD': 0.60,
            'SUBTITLE_QC_SAMPLE_MAX_ITEMS': 80,
            'SUBTITLE_QC_MAX_CHARS': 9000,
        }
        cfg.update(overrides)
        return cfg

    def test_call_ai_judge_uses_orca(self):
        from modules.subtitle_qc import _call_ai_judge

        with patch('modules.subtitle_qc._build_openai_client') as mock_build, \
             patch('modules.utils.openai_chat_create_with_thinking_control') as mock_create:
            mock_client = mock_build.return_value
            mock_create.return_value = type('Resp', (), {
                'choices': [type('Choice', (), {
                    'message': type('Msg', (), {
                        'content': '{"passed":true,"score":0.9,"reason":"ok"}',
                        'parsed': None,
                        'reasoning_content': None,
                    })
                })]
            })()
            passed, score, raw, status = _call_ai_judge('sample', {}, self._qc_config())
            self.assertEqual(status, 'ok')
            self.assertTrue(passed)
            self.assertAlmostEqual(score, 0.9)
            build_kwargs = mock_build.call_args[1]
            self.assertEqual(build_kwargs['base_url'], 'https://api.orcarouter.ai/v1')
            self.assertEqual(build_kwargs['api_key'], 'sk-orca-test')

    def test_run_subtitle_qc_orca_provider_allowed(self):
        from modules.subtitle_qc import run_subtitle_qc
        import tempfile, os

        srt_content = (
            "1\n00:00:00,000 --> 00:00:02,000\n"
            "Welcome to today's tutorial on building a neural network from scratch using PyTorch.\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n"
            "We will cover the core concepts of tensor operations and automatic differentiation.\n\n"
            "3\n00:00:04,000 --> 00:00:06,000\n"
            "By the end of this video you will understand the full training loop implementation.\n"
        )
        with tempfile.NamedTemporaryFile('w', suffix='.srt', delete=False, encoding='utf-8') as f:
            f.write(srt_content)
            path = f.name
        try:
            with patch('modules.subtitle_qc._sample_items') as mock_sample, \
                 patch('modules.subtitle_qc._call_ai_judge') as mock_judge:
                mock_sample.return_value = ('sample text', {'count': 2})
                mock_judge.return_value = (True, 0.9, {'passed': True}, 'ok')
                result = run_subtitle_qc(path, self._qc_config())
                self.assertTrue(result.passed)
                mock_judge.assert_called_once()
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
