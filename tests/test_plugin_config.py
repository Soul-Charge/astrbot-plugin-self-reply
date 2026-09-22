"""plugin_config：默认值与 parse 容错。"""

import dataclasses

import pytest

from astrbot_plugin_self_reply.judge_utils import (
    DEFAULT_GENERATE_PROMPT,
    DEFAULT_JUDGE_PROMPT,
)
from astrbot_plugin_self_reply.pipeline_dispatch import (
    DEFAULT_CHAT_TOOLS,
    DEFAULT_FALLBACK_ON,
    DEFAULT_FALLBACK_TEXT,
)
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
    assert cfg.generate.prompt_template == DEFAULT_GENERATE_PROMPT
    assert cfg.anti_repeat.enable is True
    assert cfg.anti_repeat.similarity_threshold == 0.85
    assert cfg.anti_repeat.max_retries == 1
    assert cfg.anti_repeat.compare_window == 10
    assert cfg.history.max_messages == 50
    assert cfg.memory.enable is True
    assert cfg.memory.top_k == 0
    assert cfg.memory.kb_enable is True
    assert cfg.memory.kb_top_k == 0
    assert cfg.memory.max_chars == 3000
    assert cfg.memory.timeout_sec == 5.0
    assert cfg.memory.inject_into_judge is True
    assert cfg.memory.record_bot_reply is False
    assert cfg.whitelist.allowed_origins == []
    assert cfg.global_settings.max_origins == 500
    assert cfg.global_settings.judge_timeout_sec == 45.0
    assert cfg.global_settings.generate_timeout_sec == 60.0


def test_prompt_template_formatable_with_judge_kwargs():
    rendered = DEFAULT_JUDGE_PROMPT.format(
        persona_name="Soul-Charge",
        persona_mask="mask",
        pending_count=3,
        pending_msgs="msgs",
        history_count=10,
        history_lines="lines",
    )
    assert "Soul-Charge" in rendered
    assert '{"decision":"reply"' in rendered
    assert parse_plugin_config({}).judge.prompt_template.format(
        persona_name="x",
        persona_mask="y",
        pending_count=1,
        pending_msgs="m",
        history_count=1,
        history_lines="h",
    )


def test_generate_prompt_template_formatable():
    rendered = DEFAULT_GENERATE_PROMPT.format(
        history_text="hist",
        targets_str="targets",
        quote_rule="quote",
        anti_repeat_instr="anti_repeat",
    )
    assert "小盐" in rendered
    assert "句尾括号" in rendered
    assert "以括号为准" in rendered


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


def test_memory_config_parsing_tolerance():
    cfg = parse_plugin_config(
        {
            "memory": {
                "enable": "false",
                "top_k": "2",
                "kb_enable": 0,
                "kb_top_k": "3.9",
                "max_chars": "800",
                "timeout_sec": "2.5",
                "inject_into_judge": "on",
                "record_bot_reply": "yes",
            }
        }
    )
    assert cfg.memory.enable is False
    assert cfg.memory.top_k == 2
    assert cfg.memory.kb_enable is False
    assert cfg.memory.kb_top_k == 3
    assert cfg.memory.max_chars == 800
    assert cfg.memory.timeout_sec == 2.5
    assert cfg.memory.inject_into_judge is True
    assert cfg.memory.record_bot_reply is True


def test_memory_config_invalid_values_fall_back():
    cfg = parse_plugin_config(
        {
            "memory": {
                "top_k": "abc",
                "timeout_sec": None,
                "enable": "not-a-bool",
            }
        }
    )
    assert cfg.memory.top_k == 0
    assert cfg.memory.timeout_sec == 5.0
    assert cfg.memory.enable is True


def test_dispatch_config_defaults_to_direct():
    cfg = parse_plugin_config({})
    assert cfg.dispatch.mode == "direct"
    # P0a：默认挂核心会话；工具不再由本插件开关
    assert cfg.dispatch.attach_core_conversation is True
    assert cfg.dispatch.recent_context_lines == 12
    assert cfg.dispatch.recent_context_max_chars == 1200
    assert "{recent_context}" in cfg.dispatch.chat_note_template
    assert "{recent_context}" in cfg.dispatch.task_note_template


def test_dispatch_config_parsing():
    cfg = parse_plugin_config(
        {
            "dispatch": {
                "mode": "PIPELINE",
                "attach_core_conversation": "no",
                "chat_note_template": "CUSTOM {recent_context}",
                "task_note_template": "TASK {target_text}",
                "recent_context_lines": "5",
                "recent_context_max_chars": 300,
            }
        }
    )
    assert cfg.dispatch.mode == "pipeline"
    assert cfg.dispatch.attach_core_conversation is False
    assert cfg.dispatch.chat_note_template == "CUSTOM {recent_context}"
    assert cfg.dispatch.task_note_template == "TASK {target_text}"
    assert cfg.dispatch.recent_context_lines == 5
    assert cfg.dispatch.recent_context_max_chars == 300


def test_dispatch_config_legacy_keys_are_ignored_and_warned(caplog):
    # 老键只警告、不做值映射（老默认 keep_conversation=false 若映射会把新默认顶反）
    cfg = parse_plugin_config(
        {"dispatch": {"mode": "pipeline", "allow_tools": True, "keep_conversation": False}}
    )
    assert cfg.dispatch.attach_core_conversation is True


def test_dispatch_config_invalid_mode_falls_back_to_direct():
    cfg = parse_plugin_config({"dispatch": {"mode": "telepathy"}})
    assert cfg.dispatch.mode == "direct"

def test_dispatch_config_defaults_narrow_chat_tools():
    """P0b：chat 类插话默认只拿到只读白名单（task 类不受影响）。"""
    cfg = parse_plugin_config({})
    assert cfg.dispatch.chat_tool_mode == "readonly"
    assert DEFAULT_CHAT_TOOLS == ("search_memes",)
    assert cfg.dispatch.chat_tools == DEFAULT_CHAT_TOOLS


def test_dispatch_config_chat_tool_keys_parsing():
    cfg = parse_plugin_config(
        {
            "dispatch": {
                "chat_tool_mode": "NONE",
                "chat_tools": ["search_memes", " bilibili_read "],
            }
        }
    )
    assert cfg.dispatch.chat_tool_mode == "none"
    assert cfg.dispatch.chat_tools == ("search_memes", "bilibili_read")


def test_dispatch_config_empty_chat_tools_stays_empty():
    """白名单显式留空 = 只读模式下一个都不留，不偷偷回退成默认值。"""
    cfg = parse_plugin_config(
        {"dispatch": {"chat_tool_mode": "readonly", "chat_tools": []}}
    )
    assert cfg.dispatch.chat_tools == ()


def test_dispatch_config_invalid_chat_tool_mode_warns_and_falls_back(caplog):
    with caplog.at_level("WARNING"):
        cfg = parse_plugin_config({"dispatch": {"chat_tool_mode": "readonlyy"}})
    assert cfg.dispatch.chat_tool_mode == "none"
    assert any("chat_tool_mode" in r.getMessage() for r in caplog.records)


def test_dispatch_config_missing_chat_tool_mode_is_not_a_warning(caplog):
    """老配置里没有这个键是正常情况（走默认 readonly），不该刷警告。"""
    with caplog.at_level("WARNING"):
        cfg = parse_plugin_config({"dispatch": {"mode": "pipeline"}})
    assert cfg.dispatch.chat_tool_mode == "readonly"
    assert not any("chat_tool_mode" in r.getMessage() for r in caplog.records)


def test_dispatch_config_fallback_defaults():
    """P1b：默认只有派活轮兜底，文案走内置默认。"""
    cfg = parse_plugin_config({})
    assert cfg.dispatch.fallback_on == "task" == DEFAULT_FALLBACK_ON
    assert cfg.dispatch.fallback_text == DEFAULT_FALLBACK_TEXT


def test_dispatch_config_fallback_keys_parsing():
    cfg = parse_plugin_config(
        {"dispatch": {"fallback_on": " ALWAYS ", "fallback_text": " 再问一次喵 "}}
    )
    assert cfg.dispatch.fallback_on == "always"
    assert cfg.dispatch.fallback_text == "再问一次喵"


def test_dispatch_config_invalid_fallback_on_warns_and_falls_back(caplog):
    with caplog.at_level("WARNING"):
        cfg = parse_plugin_config({"dispatch": {"fallback_on": "alwayss"}})
    assert cfg.dispatch.fallback_on == "task"
    assert any("fallback_on" in r.getMessage() for r in caplog.records)


def test_dispatch_config_empty_fallback_text_uses_default():
    cfg = parse_plugin_config({"dispatch": {"fallback_text": "   "}})
    assert cfg.dispatch.fallback_text == DEFAULT_FALLBACK_TEXT
