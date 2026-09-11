"""memory_bridge：LivingMemory 发现、门禁、作用域、召回、降级与写回。

全部使用假对象，不触网、不依赖真实 LivingMemory 插件。
"""

import asyncio
import sys
import types

from astrbot_plugin_self_reply.memory_bridge import (
    MAX_ITEM_CHARS,
    EventShim,
    MemoryBridge,
    truncate_text,
)

ORIGIN = "bot:GroupMessage:400000000"


class FakeConfigManager:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def get(self, key, default=None):
        cur = self.cfg
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


class FakeMemory:
    def __init__(self, content, score=0.9, doc_id=1, metadata=None):
        self.content = content
        self.final_score = score
        self.doc_id = doc_id
        self.metadata = metadata if metadata is not None else {
            "create_time": "2026-08-29 12:00",
            "importance": 0.8,
        }


class FakeEngine:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []

    async def search_memories(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.results


def _make_shim(**kwargs):
    params = dict(
        origin=ORIGIN,
        sender_id="8000000000",
        sender_name="E0",
        group_id="400000000",
        platform="aiocqhttp",
        platform_id="aiocqhttp-1",
        self_id="6000000000",
    )
    params.update(kwargs)
    return EventShim(**params)


def _install_fake_package(monkeypatch, *, module_name="fake_lm", core_attrs=None):
    """在 sys.modules 里造一个假插件包，用来验证复用上游实现的路径。"""
    pkg = types.ModuleType(module_name)
    pkg.__package__ = module_name
    core = types.ModuleType(f"{module_name}.core")
    pkg.core = core
    for name, value in (core_attrs or {}).items():
        setattr(core, name, value)
    monkeypatch.setitem(sys.modules, module_name, pkg)
    monkeypatch.setitem(sys.modules, f"{module_name}.core", core)
    return pkg, core


def _make_plugin(engine, config_manager, module_name="fake_lm"):
    cls = type("FakeLivingMemory", (), {})
    cls.__module__ = module_name
    inst = cls()
    inst.initializer = types.SimpleNamespace(memory_engine=engine)
    inst.config_manager = config_manager
    return inst


def _patch_metadata(monkeypatch, instance):
    md = types.SimpleNamespace(
        name="LivingMemory",
        module_path="data.plugins.astrbot_plugin_livingmemory.main",
        module=None,
        star_cls=instance,
    )
    monkeypatch.setattr(MemoryBridge, "_find_metadata", staticmethod(lambda: md))
    return md


def _recall(bridge, shim=None, **kwargs):
    params = dict(
        shim=shim or _make_shim(),
        query="小盐三X今天跟你说了什么",
        persona_id="小盐-new",
    )
    params.update(kwargs)
    return asyncio.run(bridge.recall(**params))


# ---------------- EventShim ----------------


def test_event_shim_exposes_required_getters():
    shim = _make_shim()
    assert shim.unified_msg_origin == ORIGIN
    assert shim.get_sender_id() == "8000000000"
    assert shim.get_sender_name() == "E0"
    assert shim.get_group_id() == "400000000"
    assert shim.get_platform_name() == "aiocqhttp"
    assert shim.get_platform_id() == "aiocqhttp-1"
    assert shim.get_self_id() == "6000000000"
    assert shim.get_message_type() is not None
    assert shim.get_extra("missing", "fallback") == "fallback"


# ---------------- 发现与降级 ----------------


def test_recall_returns_empty_when_plugin_missing(monkeypatch):
    monkeypatch.setattr(MemoryBridge, "_find_metadata", staticmethod(lambda: None))
    assert _recall(MemoryBridge(None)) == ""


def test_recall_returns_empty_when_engine_not_ready(monkeypatch):
    inst = _make_plugin(None, FakeConfigManager())
    _patch_metadata(monkeypatch, inst)
    assert _recall(MemoryBridge(None)) == ""


def test_recall_returns_empty_when_whitelist_denied(monkeypatch):
    engine = FakeEngine([FakeMemory("user是窝的姐X")])
    cfg = FakeConfigManager(
        {
            "access_control": {"whitelist_enabled": True, "allowed_ids": ""},
            "recall_engine": {"top_k": 3},
        }
    )
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))
    assert _recall(MemoryBridge(None)) == ""
    assert engine.calls == []


def test_recall_returns_empty_on_engine_error(monkeypatch):
    engine = FakeEngine(error=RuntimeError("boom"))
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))
    assert _recall(MemoryBridge(None)) == ""


def test_recall_returns_empty_when_top_k_disabled(monkeypatch):
    engine = FakeEngine([FakeMemory("不应该被检索")])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 0}})
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))
    assert _recall(MemoryBridge(None)) == ""
    assert engine.calls == []


# ---------------- 检索参数 ----------------


def test_recall_passes_query_persona_and_scope(monkeypatch):
    engine = FakeEngine([FakeMemory("user是窝的姐X，要叫妈X")])
    cfg = FakeConfigManager(
        {
            "recall_engine": {"top_k": 3},
            "filtering_settings": {
                "use_persona_filtering": True,
                "use_session_filtering": False,
            },
        }
    )
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))

    block = _recall(MemoryBridge(None), query="三X是谁", persona_id="小盐-new")

    assert engine.calls == [
        {
            "query": "三X是谁",
            "k": 3,
            "session_id": None,  # use_session_filtering=false → 跨会话共享
            "persona_id": "小盐-new",
        }
    ]
    assert "user是窝的姐X" in block


def test_recall_honours_session_scope_when_enabled(monkeypatch):
    engine = FakeEngine([FakeMemory("会话内记忆")])
    cfg = FakeConfigManager(
        {
            "recall_engine": {"top_k": 5},
            "filtering_settings": {"use_session_filtering": True},
        }
    )
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))

    _recall(MemoryBridge(None))
    assert engine.calls[0]["session_id"] == ORIGIN


def test_recall_skips_persona_filter_when_disabled(monkeypatch):
    engine = FakeEngine([FakeMemory("跨人格记忆")])
    cfg = FakeConfigManager(
        {
            "recall_engine": {"top_k": 5},
            "filtering_settings": {
                "use_persona_filtering": False,
                "use_session_filtering": False,
            },
        }
    )
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))

    _recall(MemoryBridge(None))
    assert engine.calls[0]["persona_id"] is None


def test_recall_uses_livingmemory_formatter_when_available(monkeypatch):
    engine = FakeEngine([FakeMemory("姐X教窝叫妈X")])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(engine, cfg)

    utils = types.ModuleType("fake_lm.core.utils")

    def fake_formatter(memories):
        return "LM-FORMATTED:" + memories[0]["content"]

    utils.format_memories_for_injection = fake_formatter
    _install_fake_package(monkeypatch, core_attrs={"utils": utils})
    _patch_metadata(monkeypatch, instance)

    assert _recall(MemoryBridge(None)) == "LM-FORMATTED:姐X教窝叫妈X"


def test_recall_reuses_livingmemory_whitelist_check(monkeypatch):
    engine = FakeEngine([FakeMemory("门禁由上游决定")])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(engine, cfg)

    scope_mod = types.ModuleType("fake_lm.core.memory_scope")
    scope_mod.is_event_memory_allowed = lambda config, shim: False
    scope_mod.resolve_memory_scope = lambda config, shim: "livingmemory:global"
    _install_fake_package(monkeypatch, core_attrs={"memory_scope": scope_mod})
    _patch_metadata(monkeypatch, instance)

    assert _recall(MemoryBridge(None)) == ""
    assert engine.calls == []


def test_recall_blocked_by_disabled_session(monkeypatch):
    engine = FakeEngine([FakeMemory("会话已关闭")])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(engine, cfg)

    async def _disabled(origin):
        return False

    capture = types.ModuleType("fake_lm.core.passive_group_capture")
    capture.is_session_enabled = _disabled
    _install_fake_package(monkeypatch, core_attrs={"passive_group_capture": capture})
    _patch_metadata(monkeypatch, instance)

    assert _recall(MemoryBridge(None)) == ""
    assert engine.calls == []


# ---------------- 截断 ----------------


def test_truncate_text_appends_suffix():
    assert truncate_text("abcdef", 10) == "abcdef"
    out = truncate_text("x" * 100, 20)
    assert out.endswith("（已截断）")
    assert len(out) == 20


def test_recall_truncates_long_memory_content(monkeypatch):
    engine = FakeEngine([FakeMemory("长" * 900)])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))

    block = _recall(MemoryBridge(None), max_chars=5000)
    assert "长" * MAX_ITEM_CHARS not in block  # 单条已被截断
    assert "长" * (MAX_ITEM_CHARS - 10) in block
    assert "（已截断）" in block


def test_recall_respects_total_char_limit(monkeypatch):
    engine = FakeEngine([FakeMemory("内容" * 200) for _ in range(5)])
    cfg = FakeConfigManager({"recall_engine": {"top_k": 5}})
    _patch_metadata(monkeypatch, _make_plugin(engine, cfg))

    block = _recall(MemoryBridge(None), max_chars=300)
    assert len(block) == 300


# ---------------- 写回 ----------------


def test_record_bot_reply_calls_livingmemory_reflection(monkeypatch):
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(FakeEngine(), cfg)
    seen = {}

    async def fake_reflection(event, resp):
        seen["origin"] = event.unified_msg_origin
        seen["text"] = resp.completion_text
        seen["role"] = resp.role

    instance.event_handler = types.SimpleNamespace(
        handle_memory_reflection=fake_reflection
    )
    _patch_metadata(monkeypatch, instance)

    ok = asyncio.run(
        MemoryBridge(None).record_bot_reply(shim=_make_shim(), text="窝也这么觉得喵")
    )

    assert ok is True
    assert seen == {
        "origin": ORIGIN,
        "text": "窝也这么觉得喵",
        "role": "assistant",
    }


def test_record_bot_reply_without_handler(monkeypatch):
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(FakeEngine(), cfg)
    instance.event_handler = None
    _patch_metadata(monkeypatch, instance)

    ok = asyncio.run(
        MemoryBridge(None).record_bot_reply(shim=_make_shim(), text="随便说说")
    )
    assert ok is False


def test_record_bot_reply_empty_text(monkeypatch):
    cfg = FakeConfigManager({"recall_engine": {"top_k": 3}})
    instance = _make_plugin(FakeEngine(), cfg)
    _patch_metadata(monkeypatch, instance)
    assert asyncio.run(MemoryBridge(None).record_bot_reply(shim=_make_shim(), text="  ")) is False
