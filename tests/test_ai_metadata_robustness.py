"""AI metadata accepts only complete, typed output; no real endpoints are used."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules import ai_enhancer as ai


def response(text, finish_reason="stop", refusal=None):
    message = SimpleNamespace(content=text, parsed=None, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(
        message=message, finish_reason=finish_reason,
    )])


def request_json():
    return ai._request_json_object(
        object(), "model", "Return JSON", {}, temperature=0,
        thinking_enabled=False, logger_obj=logging.getLogger("test"),
        scene_name="metadata_test",
    )


def test_truncated_json_is_retried_not_salvaged():
    with patch.object(ai, "_request_chat_completion", side_effect=[
        response('{"title":"partial"}', "length"),
        response('{"title":"complete"}'),
    ]) as call:
        assert request_json() == {"title": "complete"}
    assert call.call_count == 2


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls"])
def test_raw_text_rejects_incomplete_output(reason):
    with patch.object(ai, "_request_chat_completion", return_value=response("0 2", reason)):
        assert ai._request_raw_text(
            object(), "model", "indices", {}, temperature=0,
            thinking_enabled=False, logger_obj=None, scene_name="segment",
        ) == ""


def test_refusal_does_not_become_a_json_result():
    with patch.object(ai, "_request_chat_completion", return_value=response(
        '{"title":"unsafe"}', refusal="refused",
    )):
        assert request_json() is None


def test_timeout_does_not_repeat_expensive_generation_in_plain_mode():
    with patch.object(ai, "_request_chat_completion", side_effect=TimeoutError("timeout")) as call:
        with pytest.raises(TimeoutError):
            request_json()
    assert call.call_count == 1


def test_unrelated_invalid_parameter_is_not_treated_as_json_mode_failure():
    with patch.object(ai, "_request_chat_completion", side_effect=ValueError(
        "invalid parameter: image_url",
    )) as call:
        with pytest.raises(ValueError):
            request_json()
    assert call.call_count == 1


@pytest.mark.parametrize("value", [True, 7, 0.5, [], {}, ["title"], None])
def test_metadata_fields_reject_non_strings(value):
    assert ai._sanitize_metadata_field(value, "title") == ""


@pytest.mark.parametrize("value", [True, False, float("inf"), float("-inf"), float("nan"), "NaN"])
def test_confidence_rejects_boolean_and_nonfinite_values(value):
    assert ai._coerce_confidence(value) is None


def test_tags_ignore_non_string_values():
    with patch.object(ai, "get_openai_client", return_value=object()), \
         patch.object(ai, "setup_task_logger", return_value=logging.getLogger("test")), \
         patch.object(ai, "_request_json_object", return_value={
             "tags": [True, 2, {"topic": "game"}, ["game"], "Game", "game", "News"],
         }):
        tags = ai.generate_acfun_tags(
            "A new game review", "Detailed gameplay review.", {"OPENAI_API_KEY": "test"},
        )
    assert tags == ["Game", "News", "", "", "", ""]


def test_profile_rejects_container_values_as_fabricated_text():
    assert ai._normalize_content_profile({
        "subdomain": {"topic": "fake"}, "entities": [None, True, 42, {}],
        "reason_summary": ["fake"],
    }) is None
