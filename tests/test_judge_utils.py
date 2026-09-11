"""judge_utils：JSON 解析 / REPLY 容错 / 异常格式。"""

from astrbot_plugin_self_reply.judge_utils import parse_judge_output


def test_valid_reply_json():
    out = parse_judge_output(
        '{"decision":"reply","target_ids":["123"],"reason":"被提问"}', "999"
    )
    assert out == {"decision": "reply", "target_ids": ["123"], "reason": "被提问"}


def test_valid_skip_json():
    out = parse_judge_output('{"decision":"skip","reason":"闲聊"}', "999")
    assert out["decision"] == "skip"
    assert out["target_ids"] == []
    assert out["reason"] == "闲聊"


def test_json_embedded_in_prose():
    raw = '好的，我的判断是 {"decision":"reply","target_ids":["42"],"reason":"r"} 请查收'
    out = parse_judge_output(raw, "999")
    assert out["decision"] == "reply"
    assert out["target_ids"] == ["42"]


def test_decision_case_insensitive_and_prefix_tolerance():
    out = parse_judge_output('{"decision":"REPLY","target_ids":["7"],"reason":""}', "")
    assert out["decision"] == "reply"
    out2 = parse_judge_output('{"decision":"Replying","target_ids":["7"]}', "")
    assert out2["decision"] == "reply"


def test_unknown_decision_in_json_falls_to_token_path():
    raw = '{"decision":"hello","target_ids":["7"],"reason":"r"}'
    out = parse_judge_output(raw, "999")
    assert out["decision"] == "skip"


def test_bare_reply_token_uses_fallback_id():
    out = parse_judge_output("REPLY", "999")
    assert out["decision"] == "reply"
    assert out["target_ids"] == ["999"]
    assert out["reason"] == "fallback REPLY parse"

    out2 = parse_judge_output("reply 因为有人在问路", "888")
    assert out2["decision"] == "reply"
    assert out2["target_ids"] == ["888"]


def test_skip_token():
    out = parse_judge_output("SKIP 不需要回复", "999")
    assert out["decision"] == "skip"
    assert out["target_ids"] == []


def test_empty_input():
    out = parse_judge_output("", "999")
    assert out == {"decision": "skip", "target_ids": [], "reason": "empty"}
    out2 = parse_judge_output("   ", "999")
    assert out2["decision"] == "skip"


def test_target_ids_not_a_list_is_dropped():
    out = parse_judge_output('{"decision":"reply","target_ids":"123","reason":""}', "999")
    assert out["decision"] == "reply"
    assert out["target_ids"] == []


def test_target_ids_ints_become_strings():
    out = parse_judge_output('{"decision":"reply","target_ids":[123, 456]}', "999")
    assert out["target_ids"] == ["123", "456"]


def test_malformed_json_falls_to_token_path():
    out = parse_judge_output("{decision: reply}", "999")
    assert out["decision"] == "skip"

    out2 = parse_judge_output("REPLY {broken json", "777")
    assert out2["decision"] == "reply"
    assert out2["target_ids"] == ["777"]


def test_plain_prose_defaults_to_skip():
    out = parse_judge_output("这是一段没有任何格式的文字", "999")
    assert out["decision"] == "skip"


def test_truncated_json_missing_brace():
    raw = '{"decision":"reply","target_ids":["msg856257937"],"reason":"在催窝说话，就被拦了一下下喵~"'
    out = parse_judge_output(raw, "999")
    assert out["decision"] == "reply"
    assert out["target_ids"] == ["msg856257937"]
    assert "在催窝说话" in out["reason"]

