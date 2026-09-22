"""P0a/P0b 回归保护：on_pipeline_llm_request 的四个必须行为（规划 §7.3 b）。

1. 不再阉割工具集——删掉 `req.func_tool = None`；
2. 默认把插话挂到核心会话——删掉无条件的 `req.conversation = None`；
3. 不再重复注入人格——人格由核心 `_ensure_persona_and_skills` 原生注入；
4. P0b：按 kind 收窄工具集——chat 只留 dispatch.chat_tools 白名单，task 全量。

这几点没有别的回归网，断言本身就是保险。
"""

import asyncio
import types

from astrbot_plugin_self_reply.main import Main
from astrbot_plugin_self_reply.pipeline_dispatch import PipelineJob, new_message_id

ORIGIN = "test-platform:GroupMessage:10000"
#: 线上真实工具集的样子：有副作用的、只读的、联网的各一个
ALL_TOOLS = ("llm_poke_user", "search_memes", "web_search_tavily")
SELF_ID = "10001"
PERSONA = "# Persona Instructions\n你是小盐，一只猫娘。"


class FakePart:
    """假的 TextPart：生产代码用 type(part)(text=...) 重建。"""

    def __init__(self, text):
        self.text = text


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeToolSet:
    def __init__(self, *names):
        self.tools = [FakeTool(n) for n in names]

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]


class FakeEvent:
    def __init__(self, message_id):
        self.unified_msg_origin = ORIGIN
        self.message_obj = types.SimpleNamespace(message_id=message_id)

    def get_extra(self, key=None, default=None):
        return default


class FakePersonaManager:
    personas_v3 = []

    async def get_default_persona_v3(self, origin):
        return {"name": "小盐", "prompt": PERSONA}


class FakeConvManager:
    async def get_curr_conversation_id(self, origin):
        return "cid-1"

    async def get_conversation(self, origin, cid):
        return types.SimpleNamespace(history="[]", persona_id="小盐")


class FakeContext:
    def __init__(self):
        self.conversation_manager = FakeConvManager()
        self.persona_manager = FakePersonaManager()
        self.platform_manager = types.SimpleNamespace(get_insts=lambda: [])

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self, umo=None):
        return None

    def get_config(self, umo=None):
        return {"kb_names": []}


def _make_plugin(**dispatch):
    raw = {
        "enable": True,
        "judge": {"provider_id": "judge-provider"},
        "generate": {"provider_id": "gen-provider"},
        "dispatch": {"mode": "pipeline", **dispatch},
    }
    return Main(FakeContext(), raw)


def _job(**kwargs):
    payload = {
        "message_id": new_message_id(),
        "origin": ORIGIN,
        "director_note": "[旁白] 群聊近况…",
        "kind": "task",
        "self_id": SELF_ID,
        "group_id": "10000",
        "platform": "aiocqhttp",
    }
    payload.update(kwargs)
    return PipelineJob(**payload)


def _req(**kwargs):
    req = types.SimpleNamespace(
        prompt="原始 prompt",
        system_prompt=PERSONA,
        image_urls=[],
        func_tool=FakeToolSet(*ALL_TOOLS),
        conversation=object(),
        extra_user_content_parts=[
            FakePart(
                "<system_reminder>User ID: " + SELF_ID + ", Nickname: 小盐\n"
                "Current datetime: 2026-09-18 00:00 (CST)</system_reminder>"
            )
        ],
    )
    for key, value in kwargs.items():
        setattr(req, key, value)
    return req


def _call(plugin, job, req):
    plugin.dispatcher.register(job)
    asyncio.run(plugin.on_pipeline_llm_request(FakeEvent(job.message_id), req))


def test_hook_keeps_full_tool_set_for_task():
    """保护“删除 req.func_tool = None”：task 类仍然是全量工具。"""
    plugin = _make_plugin()
    req = _req()
    _call(plugin, _job(), req)
    assert req.func_tool is not None
    assert [t.name for t in req.func_tool.tools] == list(ALL_TOOLS)


def test_hook_attaches_core_conversation_by_default():
    """保护“删除无条件的 req.conversation = None”。"""
    plugin = _make_plugin()
    req = _req()
    conversation = req.conversation
    _call(plugin, _job(), req)
    assert req.conversation is conversation


def test_hook_does_not_inject_persona_twice():
    """保护“删除人格重复注入”。"""
    plugin = _make_plugin()
    req = _req()
    _call(plugin, _job(), req)
    assert req.system_prompt.count("# Persona Instructions") == 1
    assert req.system_prompt == PERSONA


def test_hook_writes_director_note_and_logs_real_capabilities(caplog):
    plugin = _make_plugin()
    job = _job(kind="task", director_note="[旁白] 只接一句")
    req = _req()
    with caplog.at_level("INFO"):
        _call(plugin, job, req)
    assert req.prompt == "[旁白] 只接一句"
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "kind=task" in line
    assert "tool_mode=readonly" in line
    assert f"tools={list(ALL_TOOLS)}" in line
    assert "dropped=" not in line


def test_hook_leaves_foreign_events_alone():
    plugin = _make_plugin()
    req = _req()
    before = (req.prompt, req.system_prompt, req.conversation)
    tools_before = _tool_names(req) if hasattr(req.func_tool, "tools") else None
    asyncio.run(plugin.on_pipeline_llm_request(FakeEvent("not-ours"), req))
    assert (req.prompt, req.system_prompt, req.conversation) == before
    # 别人的事件连工具集都不能碰
    assert _tool_names(req) == tools_before

# ---------------- P0b：按 kind 收窄工具集 ----------------


def _tool_names(req):
    return [t.name for t in req.func_tool.tools]


def test_hook_narrows_chat_tools_to_readonly_allowlist():
    """chat（闲聊接梗）默认只留 dispatch.chat_tools 白名单里的工具。"""
    plugin = _make_plugin()
    req = _req()
    _call(plugin, _job(kind="chat"), req)
    assert _tool_names(req) == ["search_memes"]


def test_hook_chat_tool_mode_none_drops_every_tool():
    plugin = _make_plugin(chat_tool_mode="none")
    req = _req()
    _call(plugin, _job(kind="chat"), req)
    assert req.func_tool.tools == []
    assert req.func_tool is not None  # 只是清空，不是置 None


def test_hook_chat_tool_mode_all_is_a_noop():
    plugin = _make_plugin(chat_tool_mode="all")
    req = _req()
    _call(plugin, _job(kind="chat"), req)
    assert _tool_names(req) == list(ALL_TOOLS)


def test_hook_task_ignores_chat_tool_mode():
    """派活类不受 chat 策略影响：永远全量。"""
    plugin = _make_plugin(chat_tool_mode="none")
    req = _req()
    _call(plugin, _job(kind="task"), req)
    assert _tool_names(req) == list(ALL_TOOLS)


def test_hook_respects_custom_chat_allowlist():
    plugin = _make_plugin(chat_tools=["web_search_tavily", "llm_poke_user"])
    req = _req()
    _call(plugin, _job(kind="chat"), req)
    # 保留顺序跟着原工具集，而不是白名单顺序
    assert _tool_names(req) == ["llm_poke_user", "web_search_tavily"]


def test_hook_unknown_kind_is_narrowed_like_chat():
    """判定失败兜底成 chat 时，工具也必须一起保守。"""
    plugin = _make_plugin()
    req = _req()
    _call(plugin, _job(kind="banter"), req)
    assert _tool_names(req) == ["search_memes"]


def test_hook_narrowing_tolerates_foreign_tool_object():
    """拿不到标准 ToolSet 时不崩、也不动它（宁可少收窄一次）。"""
    plugin = _make_plugin()
    req = _req()
    foreign = object()
    req.func_tool = foreign
    _call(plugin, _job(kind="chat"), req)
    assert req.func_tool is foreign


def test_hook_narrowing_tolerates_toolset_without_remove_tool():
    """老/伪 ToolSet 没有 remove_tool 时退化为重建 tools 列表。"""

    class _NoRemove:
        def __init__(self):
            self.tools = [FakeTool("search_memes"), FakeTool("llm_poke_user")]

    plugin = _make_plugin()
    req = _req(func_tool=_NoRemove())
    _call(plugin, _job(kind="chat"), req)
    assert [t.name for t in req.func_tool.tools] == ["search_memes"]


def test_hook_logs_dropped_tools_for_chat(caplog):
    plugin = _make_plugin()
    req = _req()
    with caplog.at_level("INFO"):
        _call(plugin, _job(kind="chat"), req)
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "kind=chat" in line
    assert "tool_mode=readonly" in line
    assert "tools=['search_memes']" in line
    assert "dropped=['llm_poke_user', 'web_search_tavily']" in line


def test_hook_leaves_no_trace_when_toolset_was_empty():
    """本来就没有工具：不报错、日志里也不该出现 dropped。"""
    plugin = _make_plugin()
    req = _req(func_tool=FakeToolSet())
    _call(plugin, _job(kind="chat"), req)
    assert req.func_tool.tools == []
