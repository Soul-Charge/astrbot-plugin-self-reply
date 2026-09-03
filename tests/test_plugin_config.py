"""plugin_config：默认值与 parse 容错。"""

import dataclasses

import pytest

from astrbot_plugin_self_reply.judge_utils import DEFAULT_JUDGE_PROMPT
from astrbot_plugin_self_reply.plugin_config import PluginConfig, parse_plugin_config


def test_defaults_on_empty_config():
    cfg = parse_plugin_config({})
    assert cfg.enable is False
    assert cfg.enabled is False
    assert cfg.trigger.debounce_normal_seconds == 5.0
    assert cfg.trigger.debounce_quick_seconds == 1.0
    assert cfg.trigger.min_reply_interval_seconds == 15.0
    assert cfg.trigger.quick_trigger_keywords == []
    assert cfg.trigger.max_pending_before_judge == 20
    assert cfg.judge.provider_id == "deepseek/deepseek-v4-flash"
    assert cfg.judge.history_messages == 30
    assert cfg.judge.prompt_template == DEFAULT_JUDGE_PROMPT
    assert cfg.generate.provider_id == "deepseek-vision"
    assert cfg.generate.attach_recent_images == 3
    assert cfg.generate.quote_policy == "judge"
    assert cfg.generate.include_role_tag is True
    assert cfg.generate.include_sender_id is True
    assert cfg.anti_repeat.enable is True
    assert cfg.anti_repeat.similarity_threshold == 0.85
    assert cfg.anti_repeat.max_retries == 1
    assert cfg.anti_repeat.compare_window == 10
    assert cfg.history.max_messages == 50
    assert cfg.whitelist.allowed_origins == []
    assert cfg.global_settings.max_origins == 500
    assert cfg.global_settings.judge_timeout_sec == 45.0
    assert cfg.global_settings.generate_timeout_sec == 60.0


def test_prompt_template_formatable_with_judge_kwargs():
    rendered = DEFAULT_JUDGE_PROMPT.format(
        persona_name="阿汐",
        persona_mask="mask",
        pending_count=3,
        pending_msgs="msgs",
        history_count=10,
        history_lines="lines",
    )
    assert "阿汐" in rendered
    assert '{"decision":"reply"' in rendered
    assert parse_plugin_config({}).judge.prompt_template.format(
        persona_name="x",
        persona_mask="y",
        pending_count=1,
        pending_msgs="m",
        history_count=1,
        history_lines="h",
    )


def test_tolerant_type_conversion():
    cfg = parse_plugin_config(
        {
            "enable": "true",
            "trigger": {
                "debounce_normal_seconds": "3.5",
                "min_reply_interval_seconds": 30,
                "quick_trigger_keywords": "加我",
                "max_pending_before_judge": "35",
            },
            "judge": {"history_messages": 30.7},
            "generate": {"quote_policy": "MODEL", "attach_recent_images": "0"},
            "anti_repeat": {"enable": 0, "similarity_threshold": "0.9"},
        }
    )
    assert cfg.enable is True
    assert cfg.trigger.debounce_normal_seconds == 3.5
    assert cfg.trigger.min_reply_interval_seconds == 30.0
    assert cfg.trigger.quick_trigger_keywords == ["加我"]
    assert cfg.trigger.max_pending_before_judge == 35
    assert cfg.judge.history_messages == 30
    assert cfg.generate.quote_policy == "model"
    assert cfg.generate.attach_recent_images == 0
    assert cfg.anti_repeat.enable is False
    assert cfg.anti_repeat.similarity_threshold == 0.9


def test_invalid_values_fall_back_to_defaults():
    cfg = parse_plugin_config(
        {
            "enable": "not-a-bool",
            "trigger": {"debounce_normal_seconds": "abc"},
            "judge": {"history_messages": None},
            "generate": {"quote_policy": "always"},
            "anti_repeat": {"compare_window": "xyz"},
            "history": "not-a-dict",
            "global_settings": {"judge_timeout_sec": "oops"},
        }
    )
    assert cfg.enable is False
    assert cfg.trigger.debounce_normal_seconds == 5.0
    assert cfg.judge.history_messages == 30
    assert cfg.generate.quote_policy == "judge"
    assert cfg.anti_repeat.compare_window == 10
    assert cfg.history.max_messages == 50
    assert cfg.global_settings.judge_timeout_sec == 45.0


def test_quote_policy_allowed_values():
    for policy in ("judge", "model", "none"):
        assert parse_plugin_config({"generate": {"quote_policy": policy}}).generate.quote_policy == policy
    assert parse_plugin_config({"generate": {"quote_policy": None}}).generate.quote_policy == "judge"
    assert parse_plugin_config({"generate": {"quote_policy": ""}}).generate.quote_policy == "judge"


def test_list_fields_accept_iterables():
    cfg = parse_plugin_config(
        {
            "trigger": {"quick_trigger_keywords": [" a ", "", "b"]},
            "whitelist": {"allowed_origins": ("x", "y")},
        }
    )
    assert cfg.trigger.quick_trigger_keywords == ["a", "b"]
    assert cfg.whitelist.allowed_origins == ["x", "y"]


def test_config_is_frozen():
    cfg = parse_plugin_config({"enable": True})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.enable = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.trigger.debounce_normal_seconds = 99.0
