"""main：会话重置清理（优先级 / 钩子 / 指纹自愈 / 在途守卫）与召回注入。"""

import asyncio
import importlib
import json
import sys
import types

import astrbot_plugin_self_reply.main as main_mod
from astrbot_plugin_self_reply.main import (
    RESET_HOOK_PRIORITY,
    Main,
)

ORIGIN = "bot:GroupMessage:400000000"


# ---------------- 通用假对象 ----------------


class FakeEvent:
    def __init__(self, origin=ORIGIN, clean=False):
        self.unified_msg_origin = origin
        self._extras = {"_clean_ltm_session": True} if clean else {}

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)


class FakeProvider:
    def __init__(self, texts, on_call=None):
        self.texts = list(texts)
        self.prompts = []
        self.on_call = on_call

    async def text_chat(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call()
        text = self.texts.pop(0) if self.texts else ""
        return types.SimpleNamespace(completion_text=text, reasoning_content="")


class FakeConvManager:
    def __init__(self, cid="cid-1", history=None):
        self.cid = cid
        self.history = [] if history is None else history

    async def get_curr_conversation_id(self, origin):
        return self.cid

    async def get_conversation(self, origin, cid):
        return types.SimpleNamespace(
            history=json.dumps(self.history, ensure_ascii=False),
            persona_id="小盐-new",
        )


class FakePersonaManager:
    personas_v3 = []

    async def get_default_persona_v3(self, origin):
        return {"name": "小盐-new", "prompt": "你是小盐。"}


class FakeContext:
    def __init__(self, judge, gen, conv_manager=None):
        self._judge = judge
        self._gen = gen
        self.conversation_manager = conv_manager or FakeConvManager()
        self.persona_manager = FakePersonaManager()

    def get_provider_by_id(self, provider_id):
        if provider_id == "gen-provider":
            return self._gen
        if provider_id == "judge-provider":
            return self._judge
        return None

    def get_using_provider(self, umo=None):
        return self._judge

    def get_config(self, umo=None):
        return {"kb_names": []}


def _make_plugin(judge, gen, conv_manager=None):
    cfg = {
        "enable": True,
        "judge": {"provider_id": "judge-provider"},
        "generate": {"provider_id": "gen-provider"},
    }
    return Main(FakeContext(judge, gen, conv_manager), cfg)


def _seed_pending(plugin, text="小盐三X今天跟你说了什么", norm_id="1001"):
    state = plugin.runtime.touch(ORIGIN)
    state.session_chats = [f"[E0/8000000000/18:04:49](admin) #msg{norm_id}: {text}"]
    state.pending_messages = [
        {
            "norm_id": norm_id,
            "nick": "E0",
            "sender_id": "8000000000",
            "text": text,
            "has_image": False,
            "role": "(admin)",
            "ts": 0.0,
            "group_id": "400000000",
            "platform": "aiocqhttp",
            "platform_id": "aiocqhttp-1",
            "self_id": "6000000000",
        }
    ]
    return state


def _judge_reply(norm_id="1001"):
    return json.dumps({"decision": "reply", "target_ids": [f"msg{norm_id}"]})


def _record_sends(monkeypatch):
    sent = []

    async def fake_send(origin, chain):
        sent.append((origin, chain))
        return True

    monkeypatch.setattr(main_mod.StarTools, "send_message", fake_send)
    return sent


# ---------------- 重置钩子优先级 ----------------


def test_reset_hook_priority_is_maxsize(monkeypatch):
    """命令事件常常已被 stop，只有链首钩子能收到 /reset 通知。"""
    import astrbot.api.event.filter as filter_mod

    captured = {}
    monkeypatch.setattr(
        filter_mod,
        "after_message_sent",
        lambda **kwargs: (
            captured.update(kwargs),
            lambda func: func,
        )[1],
    )
    importlib.reload(main_mod)

    assert captured.get("priority") == sys.maxsize
    assert RESET_HOOK_PRIORITY == sys.maxsize
    assert callable(main_mod.Main.on_after_message_sent)


# ---------------- 重置清理 ----------------


def test_handle_session_reset_clears_state():
    plugin = Main(None, {"enable": True})
    state = plugin.runtime.touch(ORIGIN)
    state.session_chats.append("[You/18:00:00]: 旧上下文")

    assert plugin._handle_session_reset(FakeEvent(clean=True)) is True
    assert plugin.runtime.get(ORIGIN) is None


def test_handle_session_reset_ignores_plain_events():
    plugin = Main(None, {"enable": True})
    state = plugin.runtime.touch(ORIGIN)
    state.session_chats.append("[You/18:00:00]: 旧上下文")

    assert plugin._handle_session_reset(FakeEvent(clean=False)) is False
    assert plugin.runtime.get(ORIGIN) is state


def test_handle_session_reset_without_origin():
    plugin = Main(None, {"enable": True})
    assert plugin._handle_session_reset(FakeEvent(origin="", clean=True)) is False


def test_reset_hook_does_not_cancel_running_task():
    """在自身防抖任务里清理时，不应把当前任务取消掉。"""

    async def runner():
        plugin = Main(None, {"enable": True})
        state = plugin.runtime.touch(ORIGIN)

        async def fake_debounce():
            state.debounce_task = asyncio.current_task()
            plugin._handle_session_reset(FakeEvent(clean=True))
            return "finished"

        task = asyncio.create_task(fake_debounce())
        state.debounce_task = task
        return await task

    assert asyncio.run(runner()) == "finished"


# ---------------- 指纹自愈 ----------------


def _fp_plugin(manager):
    plugin = Main(None, {"enable": True})
    plugin.context = types.SimpleNamespace(conversation_manager=manager)
    return plugin


def test_fingerprint_records_first_seen_session():
    plugin = _fp_plugin(FakeConvManager(cid="cid-1", history=["a"]))
    state = plugin.runtime.touch(ORIGIN)

    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is False
    assert state.core_session_fp == ("cid-1", 1)
    assert plugin.runtime.get(ORIGIN) is state


def test_fingerprint_detects_new_conversation():
    plugin = _fp_plugin(FakeConvManager(cid="cid-1", history=["a"]))
    state = plugin.runtime.touch(ORIGIN)
    asyncio.run(plugin._detect_core_session_reset(ORIGIN, state))

    plugin.context.conversation_manager.cid = "cid-2"
    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is True
    assert plugin.runtime.get(ORIGIN) is None


def test_fingerprint_detects_history_cleared():
    plugin = _fp_plugin(FakeConvManager(cid="cid-1", history=["a", "b"]))
    state = plugin.runtime.touch(ORIGIN)
    asyncio.run(plugin._detect_core_session_reset(ORIGIN, state))

    plugin.context.conversation_manager.history = []
    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is True
    assert plugin.runtime.get(ORIGIN) is None


def test_fingerprint_ignores_history_growth():
    plugin = _fp_plugin(FakeConvManager(cid="cid-1", history=["a"]))
    state = plugin.runtime.touch(ORIGIN)
    asyncio.run(plugin._detect_core_session_reset(ORIGIN, state))

    plugin.context.conversation_manager.history = ["a", "b", "c"]
    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is False
    assert plugin.runtime.get(ORIGIN) is state
    assert state.core_session_fp == ("cid-1", 3)


def test_fingerprint_ignores_empty_to_empty():
    plugin = _fp_plugin(FakeConvManager(cid="cid-1", history=[]))
    state = plugin.runtime.touch(ORIGIN)
    asyncio.run(plugin._detect_core_session_reset(ORIGIN, state))

    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is False
    assert plugin.runtime.get(ORIGIN) is state


def test_fingerprint_survives_manager_errors():
    class BrokenManager:
        async def get_curr_conversation_id(self, origin):
            raise RuntimeError("db down")

    plugin = _fp_plugin(BrokenManager())
    state = plugin.runtime.touch(ORIGIN)

    assert asyncio.run(plugin._detect_core_session_reset(ORIGIN, state)) is False
    assert plugin.runtime.get(ORIGIN) is state


# ---------------- 召回注入 ----------------


def test_append_recall_if_missing_appends_block():
    out = Main._append_recall_if_missing("PROMPT", "PROMPT", "【记忆】叁X是姐X")
    assert out == "PROMPT\n\n【记忆】叁X是姐X"


def test_append_recall_if_missing_uses_placeholder():
    out = Main._append_recall_if_missing(
        "A\n{recalled_memories}\nB", "A\n{recalled_memories}\nB", "【记忆】叁X是姐X"
    )
    assert out == "A\n【记忆】叁X是姐X\nB"


def test_append_recall_if_missing_keeps_already_formatted_prompt():
    out = Main._append_recall_if_missing(
        "A\nMEM\nB", "A\n{recalled_memories}\nB", "【记忆】叁X是姐X"
    )
    assert out == "A\nMEM\nB"


def test_append_recall_if_missing_strips_leftover_placeholder():
    out = Main._append_recall_if_missing("A\n{recalled_memories}\n\n\nB", "T", "")
    assert "{recalled_memories}" not in out
    assert "\n\n\n" not in out


def test_build_recall_query_prefers_newest_and_skips_empty():
    pending = [
        {"text": "第一条"},
        {"text": "[Empty]"},
        {"text": "第二条"},
        {"text": "第一条"},
    ]
    assert Main._build_recall_query(pending) == "第一条 | 第二条"


def test_build_event_shim_uses_target_message():
    pending = [
        {
            "text": "[Empty]",
            "nick": "路人",
            "sender_id": "1",
            "group_id": "g1",
            "platform": "aiocqhttp",
            "platform_id": "p1",
            "self_id": "9",
        },
        {
            "text": "小盐三X是谁",
            "nick": "E0",
            "sender_id": "8000000000",
            "group_id": "g1",
            "platform": "aiocqhttp",
            "platform_id": "p1",
            "self_id": "9",
        },
    ]
    shim = Main._build_event_shim(ORIGIN, pending)
    assert shim.get_sender_id() == "8000000000"
    assert shim.get_sender_name() == "E0"
    assert shim.unified_msg_origin == ORIGIN
    assert Main._build_event_shim(ORIGIN, []) is None


def test_recall_block_injected_into_judge_and_generate(monkeypatch):
    judge = FakeProvider([_judge_reply()])
    gen = FakeProvider(["叁X是窝的姐X喵~"])
    plugin = _make_plugin(judge, gen)
    _seed_pending(plugin)
    sent = _record_sends(monkeypatch)

    async def fake_memory_recall(**kwargs):
        assert kwargs["query"] == "小盐三X今天跟你说了什么"
        assert kwargs["persona_id"] == "小盐-new"
        return "【相关长期记忆】user是窝的姐X"

    async def fake_kb(*args, **kwargs):
        return "【知识库检索结果】user，又称三X。"

    monkeypatch.setattr(plugin.memory, "recall", fake_memory_recall)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", fake_kb)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert "user是窝的姐X" in judge.prompts[0]
    assert "user，又称三X" in judge.prompts[0]
    assert "user是窝的姐X" in gen.prompts[0]
    assert "user，又称三X" in gen.prompts[0]
    assert len(sent) == 1


def test_recall_skipped_for_judge_when_disabled(monkeypatch):
    judge = FakeProvider([_judge_reply()])
    gen = FakeProvider(["回复"])
    plugin = _make_plugin(judge, gen)
    plugin._config = main_mod.parse_plugin_config(
        {
            "enable": True,
            "judge": {"provider_id": "judge-provider"},
            "generate": {"provider_id": "gen-provider"},
            "memory": {"inject_into_judge": False},
        }
    )
    _seed_pending(plugin)
    _record_sends(monkeypatch)

    async def fake_memory_recall(**kwargs):
        return "【相关长期记忆】只有生成阶段能看到"

    monkeypatch.setattr(plugin.memory, "recall", fake_memory_recall)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", lambda *a, **k: _empty())

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert "只有生成阶段能看到" not in judge.prompts[0]
    assert "只有生成阶段能看到" in gen.prompts[0]


async def _empty():
    return ""


def _plugin_with_recall(config_extra=None, judge_texts=None, gen_texts=None):
    """构造一个带自定义 memory 配置的插件（用于召回超时/并发用例）。"""
    judge = FakeProvider(judge_texts or [_judge_reply()])
    gen = FakeProvider(gen_texts or ["回复"])
    plugin = _make_plugin(judge, gen)
    raw = {
        "enable": True,
        "judge": {"provider_id": "judge-provider"},
        "generate": {"provider_id": "gen-provider"},
        "memory": config_extra or {},
    }
    plugin._config = main_mod.parse_plugin_config(raw)
    _seed_pending(plugin)
    return plugin, judge, gen


def test_kb_timeout_keeps_memory_block(monkeypatch):
    """知识库慢/超时不能把已经检索到的记忆一起丢掉。"""
    plugin, judge, gen = _plugin_with_recall({"timeout_sec": 0.2})
    _record_sends(monkeypatch)

    async def fast_memory(**kwargs):
        return "【相关长期记忆】user是窝的姐X"

    async def slow_kb(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "【知识库检索结果】不该出现"

    monkeypatch.setattr(plugin.memory, "recall", fast_memory)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", slow_kb)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert "user是窝的姐X" in judge.prompts[0]
    assert "user是窝的姐X" in gen.prompts[0]
    assert "不该出现" not in gen.prompts[0]


def test_memory_timeout_keeps_kb_block(monkeypatch):
    """记忆慢/超时也不能影响知识库结果。"""
    plugin, judge, gen = _plugin_with_recall({"timeout_sec": 0.2})
    _record_sends(monkeypatch)

    async def slow_memory(**kwargs):
        await asyncio.sleep(1.0)
        return "【相关长期记忆】不该出现"

    async def fast_kb(*args, **kwargs):
        return "【知识库检索结果】user，又称三X。"

    monkeypatch.setattr(plugin.memory, "recall", slow_memory)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", fast_kb)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert "user，又称三X" in gen.prompts[0]
    assert "不该出现" not in gen.prompts[0]


def test_recall_sources_run_concurrently(monkeypatch):
    """两个来源并发执行：各自耗时接近超时上限时，仍能同时拿到结果。"""
    plugin, judge, gen = _plugin_with_recall({"timeout_sec": 0.4})
    _record_sends(monkeypatch)

    async def memory_like(**kwargs):
        await asyncio.sleep(0.25)
        return "【相关长期记忆】记忆块"

    async def kb_like(*args, **kwargs):
        await asyncio.sleep(0.25)
        return "【知识库检索结果】知识块"

    monkeypatch.setattr(plugin.memory, "recall", memory_like)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", kb_like)

    asyncio.run(plugin._handle_pending(ORIGIN))

    # 串行执行的话第二个必然超时（0.25+0.25 > 0.4）
    assert "记忆块" in gen.prompts[0]
    assert "知识块" in gen.prompts[0]


def test_recall_block_carries_usage_hint(monkeypatch):
    """召回块首必须带"同一实体不同写法"的使用说明。"""
    plugin, judge, gen = _plugin_with_recall()
    _record_sends(monkeypatch)

    async def memory_like(**kwargs):
        return "【相关长期记忆】user是窝的姐X"

    monkeypatch.setattr(plugin.memory, "recall", memory_like)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", lambda *a, **k: _empty())

    asyncio.run(plugin._handle_pending(ORIGIN))

    from astrbot_plugin_self_reply.main import RECALL_USAGE_HINT

    assert RECALL_USAGE_HINT in gen.prompts[0]
    assert gen.prompts[0].index(RECALL_USAGE_HINT) < gen.prompts[0].index("user")


def _sent_text(chain_obj) -> str:
    parts = []
    for comp in getattr(chain_obj, "chain", None) or []:
        text = getattr(comp, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts)


def test_recall_failure_does_not_break_reply(monkeypatch):
    judge = FakeProvider([_judge_reply()])
    gen = FakeProvider(["照常回复"])
    plugin = _make_plugin(judge, gen)
    _seed_pending(plugin)
    sent = _record_sends(monkeypatch)

    async def broken_memory_recall(**kwargs):
        raise RuntimeError("LivingMemory 挂了")

    monkeypatch.setattr(plugin.memory, "recall", broken_memory_recall)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", lambda *a, **k: _empty())

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert len(sent) == 1
    assert "照常回复" in _sent_text(sent[0][1])
    assert "【相关长期记忆" not in gen.prompts[0]


# ---------------- 在途守卫 ----------------


def test_reply_dropped_when_session_reset_during_generation(monkeypatch):
    plugin_ref = {}

    def reset_during_generation():
        plugin_ref["plugin"].runtime.cleanup(ORIGIN)

    judge = FakeProvider([_judge_reply()])
    gen = FakeProvider(["重置前生成的回复"], on_call=reset_during_generation)
    plugin = _make_plugin(judge, gen)
    plugin_ref["plugin"] = plugin
    _seed_pending(plugin)
    sent = _record_sends(monkeypatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert sent == []
    assert plugin.runtime.get(ORIGIN) is None


def test_reply_sent_when_state_unchanged(monkeypatch):
    judge = FakeProvider([_judge_reply()])
    gen = FakeProvider(["正常回复"])
    plugin = _make_plugin(judge, gen)
    _seed_pending(plugin)
    sent = _record_sends(monkeypatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert len(sent) == 1
    assert plugin.runtime.get(ORIGIN) is not None
