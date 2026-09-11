"""kb_bridge：知识库检索的官方入口复用、本地兜底、降级与截断。"""

import asyncio
import sys
import types

from astrbot_plugin_self_reply.kb_bridge import KB_HEADER, retrieve_kb_block

ORIGIN = "bot:GroupMessage:400000000"
CONFIG = {
    "kb_names": ["人际关系基础设定", "音游知识库"],
    "kb_final_top_k": 5,
    "kb_fusion_top_k": 20,
}


class FakeKbManager:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class FakeContext:
    def __init__(self, kb_manager=None, config=None):
        if kb_manager is not None:
            self.kb_manager = kb_manager
        self._config = CONFIG if config is None else config

    def get_config(self, umo=None):
        return self._config


def _install_core_helper(monkeypatch, func, *, module_attr=True):
    """把 astrbot.core.tools.knowledge_base_tools 换成桩模块。"""
    mod = types.ModuleType("astrbot.core.tools.knowledge_base_tools")
    if module_attr:
        mod.retrieve_knowledge_base = func
    monkeypatch.setitem(sys.modules, "astrbot.core.tools.knowledge_base_tools", mod)
    return mod


def _run(context, **kwargs):
    params = dict(origin=ORIGIN, query="三X是谁")
    params.update(kwargs)
    return asyncio.run(retrieve_kb_block(context, **params))


def test_prefers_core_helper(monkeypatch):
    calls = []

    async def fake_helper(*, query, umo, context):
        calls.append((query, umo))
        return "【知识 1】\n来源: 人际关系基础设定\n内容: user，又称三X。"

    _install_core_helper(monkeypatch, fake_helper)
    kb_manager = FakeKbManager(result={"context_text": "本地结果"})

    block = _run(FakeContext(kb_manager=kb_manager))

    assert calls == [("三X是谁", ORIGIN)]
    assert block.startswith(KB_HEADER)
    assert "user" in block
    assert kb_manager.calls == []  # 官方入口可用时不再走本地实现


def test_falls_back_to_local_when_helper_missing(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    kb_manager = FakeKbManager(result={"context_text": "【知识 1】人际关系基础设定"})

    block = _run(FakeContext(kb_manager=kb_manager))

    assert "人际关系基础设定" in block
    assert kb_manager.calls[0] == {
        "query": "三X是谁",
        "kb_names": ["人际关系基础设定", "音游知识库"],
        "top_k_fusion": 20,
        "top_m_final": 5,
    }


def test_falls_back_to_local_when_helper_raises(monkeypatch):
    async def broken_helper(**kwargs):
        raise RuntimeError("kb down")

    _install_core_helper(monkeypatch, broken_helper)
    kb_manager = FakeKbManager(result={"context_text": "本地兜底结果"})

    block = _run(FakeContext(kb_manager=kb_manager))
    assert "本地兜底结果" in block
    assert len(kb_manager.calls) == 1


def test_local_top_k_override(monkeypatch):
    async def fake_helper(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("top_k 覆盖时不应再走官方入口")

    _install_core_helper(monkeypatch, fake_helper)
    kb_manager = FakeKbManager(result={"context_text": "结果"})

    _run(FakeContext(kb_manager=kb_manager), top_k=2)

    assert kb_manager.calls[0]["top_m_final"] == 2
    assert kb_manager.calls[0]["top_k_fusion"] == 20


def test_returns_empty_when_no_kb_names(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    kb_manager = FakeKbManager(result={"context_text": "不该出现"})

    assert _run(FakeContext(kb_manager=kb_manager, config={"kb_names": []})) == ""
    assert kb_manager.calls == []


def test_returns_empty_without_kb_manager(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    assert _run(FakeContext(kb_manager=None)) == ""


def test_returns_empty_when_retrieval_fails(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    kb_manager = FakeKbManager(error=RuntimeError("sqlite locked"))
    assert _run(FakeContext(kb_manager=kb_manager)) == ""


def test_returns_empty_when_no_result(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    assert _run(FakeContext(kb_manager=FakeKbManager(result=None))) == ""
    assert _run(FakeContext(kb_manager=FakeKbManager(result={}))) == ""


def test_returns_empty_for_empty_query(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    kb_manager = FakeKbManager(result={"context_text": "不该出现"})
    assert _run(FakeContext(kb_manager=kb_manager), query="   ") == ""
    assert kb_manager.calls == []


def test_truncates_long_context(monkeypatch):
    _install_core_helper(monkeypatch, None, module_attr=False)
    kb_manager = FakeKbManager(result={"context_text": "知识" * 500})

    block = _run(FakeContext(kb_manager=kb_manager), max_chars=200)
    assert len(block) == 200
    assert block.endswith("（已截断）")
