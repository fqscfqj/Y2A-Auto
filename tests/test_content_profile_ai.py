"""Tests for AI content profile generation and JSON parsing retry logic."""
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.ai_enhancer import (
    _normalize_content_profile,
    _request_json_object,
    _request_content_profile,
    _build_partition_analysis_payload,
)


def _make_message(text: str):
    """Create a mock chat completion message."""
    return SimpleNamespace(content=text, parsed=None, reasoning_content=None)


def _make_response(text: str):
    """Create a mock chat completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=_make_message(text))],
    )


def _make_response_with_parsed(parsed_dict: dict):
    """Create a mock response where message.parsed is already set."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(parsed_dict),
            parsed=parsed_dict,
            reasoning_content=None,
        ))],
    )


class NormalizeContentProfileTests(unittest.TestCase):
    """Unit tests for _normalize_content_profile."""

    def test_valid_profile_passthrough(self):
        raw = {
            "domain": "game",
            "subdomain": "rpg",
            "content_format": "gameplay",
            "entities": ["Skyrim", "Bethesda"],
            "game_mode": "single_player",
            "is_interview": False,
            "confidence": 0.95,
            "reason_summary": "这是一个RPG游戏实况视频",
        }
        result = _normalize_content_profile(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "game")
        self.assertEqual(result["content_format"], "gameplay")
        self.assertEqual(result["entities"], ["Skyrim", "Bethesda"])
        self.assertFalse(result["is_interview"])
        self.assertAlmostEqual(result["confidence"], 0.95)
        self.assertFalse(result["low_confidence"])

    def test_none_input_returns_none(self):
        self.assertIsNone(_normalize_content_profile(None))

    def test_non_dict_input_returns_none(self):
        self.assertIsNone(_normalize_content_profile("not a dict"))
        self.assertIsNone(_normalize_content_profile([1, 2, 3]))

    def test_all_unknown_returns_none(self):
        """Profile with all default/empty values should be rejected."""
        raw = {
            "domain": "other",
            "subdomain": "",
            "content_format": "other",
            "entities": [],
            "game_mode": "unknown",
            "is_interview": False,
            "confidence": 0.0,
            "reason_summary": "",
        }
        self.assertIsNone(_normalize_content_profile(raw))

    def test_partial_profile_accepted(self):
        """Even a minimal profile with just a domain is accepted."""
        raw = {"domain": "music", "entities": ["Taylor Swift"]}
        result = _normalize_content_profile(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "music")
        self.assertEqual(result["entities"], ["Taylor Swift"])

    def test_invalid_domain_falls_back_to_other(self):
        raw = {"domain": "invalid_domain", "entities": ["test"]}
        result = _normalize_content_profile(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "other")

    def test_entities_deduplication_and_limit(self):
        raw = {
            "domain": "game",
            "entities": ["A", "B", "A", "C", "D", "E", "F", "G", "H"],
        }
        result = _normalize_content_profile(raw)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["entities"]), 6)  # max 6
        self.assertEqual(result["entities"][0], "A")
        self.assertNotIn("A", result["entities"][1:])  # deduplicated

    def test_low_confidence_flag(self):
        raw = {"domain": "game", "confidence": 0.3, "entities": ["test"]}
        result = _normalize_content_profile(raw)
        self.assertIsNotNone(result)
        self.assertTrue(result["low_confidence"])

    def test_coerce_bool_is_interview(self):
        for val in [True, "true", "1", "yes"]:
            raw = {"domain": "game", "is_interview": val, "entities": ["x"]}
            result = _normalize_content_profile(raw)
            self.assertTrue(result["is_interview"], f"Failed for {val!r}")

        for val in [False, "false", "0", "no", None]:
            raw = {"domain": "game", "is_interview": val, "entities": ["x"]}
            result = _normalize_content_profile(raw)
            self.assertFalse(result["is_interview"], f"Failed for {val!r}")


class RequestJsonObjectTests(unittest.TestCase):
    """Tests for _request_json_object retry logic."""

    def _make_client(self, responses):
        """Create a mock OpenAI client that returns responses in order."""
        client = MagicMock()
        client.chat.completions.create = MagicMock(side_effect=responses)
        return client

    def test_valid_json_first_try(self):
        """Normal case: model returns valid JSON on first try."""
        valid = {"domain": "game", "content_format": "gameplay"}
        client = self._make_client([_make_response(json.dumps(valid))])
        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control",
                   side_effect=client.chat.completions.create.side_effect):
            result = _request_json_object(
                client, "test-model", "system prompt", {},
                max_tokens=220, temperature=0.0, thinking_enabled=False,
                logger_obj=logging.getLogger("test"),
                scene_name="test_scene",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "game")

    def test_parsed_attribute_used_first(self):
        """If message.parsed is already a dict, use it directly."""
        valid = {"domain": "music", "entities": ["test"]}
        client = self._make_client([_make_response_with_parsed(valid)])
        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control",
                   side_effect=client.chat.completions.create.side_effect):
            result = _request_json_object(
                client, "test-model", "system prompt", {},
                max_tokens=220, temperature=0.0, thinking_enabled=False,
                logger_obj=logging.getLogger("test"),
                scene_name="test_scene",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "music")

    def test_json_with_surrounding_text(self):
        """Model returns JSON wrapped in explanation text."""
        valid = {"domain": "game", "entities": ["Skyrim"]}
        text = f"Here is the analysis:\n```json\n{json.dumps(valid)}\n```\nDone."
        client = self._make_client([_make_response(text)])
        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control",
                   side_effect=client.chat.completions.create.side_effect):
            result = _request_json_object(
                client, "test-model", "system prompt", {},
                max_tokens=220, temperature=0.0, thinking_enabled=False,
                logger_obj=logging.getLogger("test"),
                scene_name="test_scene",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "game")


class RequestContentProfileTests(unittest.TestCase):
    """Integration tests for _request_content_profile with mocked API."""

    def _make_openai_config(self):
        return {
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_MODEL_NAME": "gpt-4o-mini",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_THINKING_ENABLED": False,
        }

    def _make_metadata(self):
        return {
            "title": "Can You Live An Average Life In Skyrim?",
            "description": "A fun gameplay video about living an average life in the RPG game Skyrim by Bethesda.",
        }

    @patch("modules.ai_enhancer.get_openai_client")
    @patch("modules.ai_enhancer._request_chat_completion")
    def test_content_profile_success(self, mock_chat, mock_client):
        """Full pipeline: metadata → content_profile → valid result."""
        valid_response = {
            "domain": "game",
            "subdomain": "rpg",
            "content_format": "gameplay",
            "entities": ["Skyrim", "Bethesda"],
            "game_mode": "single_player",
            "is_interview": False,
            "confidence": 0.95,
            "reason_summary": "RPG游戏实况",
        }
        mock_chat.return_value = _make_response(json.dumps(valid_response))
        mock_client.return_value = MagicMock()

        result = _request_content_profile(
            metadata=self._make_metadata(),
            openai_config=self._make_openai_config(),
            logger=logging.getLogger("test"),
            scene_name="test_profile",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "game")
        self.assertEqual(result["content_format"], "gameplay")

    @patch("modules.ai_enhancer.get_openai_client")
    @patch("modules.ai_enhancer._request_chat_completion")
    def test_content_profile_non_json_then_success(self, mock_chat, mock_client):
        """First call returns junk, retry returns valid JSON.

        This simulates the real-world scenario where the model occasionally
        returns non-JSON content on the first attempt.
        """
        junk_response = _make_response("I analyzed the video and it seems to be about gaming.")
        valid_response_data = {
            "domain": "game",
            "subdomain": "rpg",
            "content_format": "gameplay",
            "entities": ["Skyrim"],
            "game_mode": "single_player",
            "is_interview": False,
            "confidence": 0.9,
            "reason_summary": "RPG游戏",
        }
        mock_chat.side_effect = [
            junk_response,
            _make_response(json.dumps(valid_response_data)),
        ]
        mock_client.return_value = MagicMock()

        result = _request_content_profile(
            metadata=self._make_metadata(),
            openai_config=self._make_openai_config(),
            logger=logging.getLogger("test"),
            scene_name="test_profile_retry",
        )
        # With retry, this should succeed
        self.assertIsNotNone(result)
        self.assertEqual(result["domain"], "game")

    @patch("modules.ai_enhancer.get_openai_client")
    @patch("modules.ai_enhancer._request_chat_completion")
    def test_content_profile_all_fail_returns_none(self, mock_chat, mock_client):
        """All attempts return non-JSON → gracefully returns None."""
        mock_chat.return_value = _make_response("Sorry, I can't analyze this.")
        mock_client.return_value = MagicMock()

        result = _request_content_profile(
            metadata=self._make_metadata(),
            openai_config=self._make_openai_config(),
            logger=logging.getLogger("test"),
            scene_name="test_profile_fail",
        )
        self.assertIsNone(result)

    def test_content_profile_no_api_key(self):
        """No API key → returns None immediately."""
        result = _request_content_profile(
            metadata=self._make_metadata(),
            openai_config={"OPENAI_API_KEY": ""},
            logger=logging.getLogger("test"),
            scene_name="test_no_key",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
