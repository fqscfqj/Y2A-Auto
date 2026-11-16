"""Reasoning effort is independently configurable and safe for legacy configs."""
import json
import os
from copy import deepcopy
from unittest.mock import patch

import pytest
from lxml import html

from modules import config_manager as cm
from modules.utils import normalize_reasoning_effort


@pytest.mark.parametrize("value,expected", [
    ("HIGH", "high"), (" medium ", "medium"), ("xhigh", "xhigh"),
    ("minimal", "minimal"), ("low", "low"), ("auto", "auto"),
    ("none", "auto"), ("enabled", "auto"), (None, "auto"),
    (True, "auto"), ({"effort": "high"}, "auto"),
])
def test_effort_normalization(value, expected):
    assert normalize_reasoning_effort(value) == expected


def test_four_paths_default_to_model_default():
    assert len(cm.AI_REASONING_EFFORT_KEYS) == 4
    for key in cm.AI_REASONING_EFFORT_KEYS:
        assert cm.DEFAULT_CONFIG[key] == "auto"


def test_loading_legacy_or_malformed_config_normalizes_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "get_app_subdir", lambda name: str(tmp_path / name))
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir()
    raw = deepcopy(cm.DEFAULT_CONFIG)
    raw.pop("AI_SEGMENTATION_REASONING_EFFORT")
    raw["OPENAI_REASONING_EFFORT"] = " HIGH "
    raw["SUBTITLE_OPENAI_REASONING_EFFORT"] = {"effort": "high"}
    raw["SUBTITLE_QC_REASONING_EFFORT"] = "none"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    result = cm.load_config()
    assert result["OPENAI_REASONING_EFFORT"] == "high"
    for key in cm.AI_REASONING_EFFORT_KEYS[1:]:
        assert result[key] == "auto"
    assert json.loads(config_path.read_text(encoding="utf-8")) == result


def test_update_config_keeps_independent_efforts(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "get_app_subdir", lambda name: str(tmp_path / name))
    values = dict(zip(cm.AI_REASONING_EFFORT_KEYS, ["high", "low", "xhigh", "minimal"]))
    saved = cm.update_config(values)
    for key, value in values.items():
        assert saved[key] == value
    assert cm.update_config({"OPENAI_REASONING_EFFORT": "invalid"})["OPENAI_REASONING_EFFORT"] == "auto"
    assert cm.load_config()["SUBTITLE_OPENAI_REASONING_EFFORT"] == "low"


def test_settings_renders_unique_labeled_effort_controls():
    import app as web_app
    config = deepcopy(cm.DEFAULT_CONFIG)
    config["SUBTITLE_QC_REASONING_EFFORT"] = "high"
    client = web_app.app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
    with patch.object(web_app, "load_config", return_value=config):
        response = client.get("/settings")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    document = html.fromstring(page)
    for key in cm.AI_REASONING_EFFORT_KEYS:
        controls = document.xpath('//select[@name="%s"]' % key)
        assert len(controls) == 1
        control = controls[0]
        assert document.xpath('//label[@for="%s"]' % control.get("id"))
        assert [x.get("value") for x in control.xpath("./option")] == [
            "auto", "minimal", "low", "medium", "high", "xhigh",
        ]
        assert [x.get("value") for x in control.xpath("./option[@selected]")] == [config[key]]


def test_settings_invalid_effort_reports_visible_revert(tmp_path, monkeypatch):
    import app as web_app
    monkeypatch.setattr(cm, "get_app_subdir", lambda name: os.fspath(tmp_path / name))
    monkeypatch.setattr(web_app, "configure_app", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "_sync_notification_service", lambda *a, **k: None)
    monkeypatch.setattr(web_app.youtube_monitor, "reload_api_client", lambda *a, **k: (False, "missing_api_key"))
    monkeypatch.setattr(web_app.youtube_monitor, "start_all_schedules", lambda *a, **k: None)
    monkeypatch.setattr(web_app.youtube_monitor, "stop_all_schedules", lambda *a, **k: None)
    with patch("modules.task_manager.get_global_task_processor", return_value=None):
        result = web_app._perform_settings_save({"OPENAI_REASONING_EFFORT": "invalid"}, {})
    assert result["success"]
    assert result["updated_config"]["OPENAI_REASONING_EFFORT"] == "auto"
    assert any(
        m["category"] == "warning" and "OPENAI_REASONING_EFFORT" in m["text"]
        for m in result["messages"]
    )
