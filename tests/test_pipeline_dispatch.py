"""主管道派发（pipeline dispatch）：合成事件注入 + 三个往返钩子。"""

import asyncio
import json
import sys
import types

import astrbot_plugin_self_reply.main as main_mod
from astrbot_plugin_self_reply.main import Main
from astrbot_plugin_self_reply.pipeline_dispatch import (
    MARKER_PREFIX,
    PipelineJob,
    new_message_id,
)

from astrbot.api.message_components import Plain, Reply

ORIGIN = "bot:GroupMessage:400000000"


# ---------------- 通用假对象 ----------------


class FakeMessageObj:
    def __init__(self, message_id=""):
        self.message_id = message_id


class FakeEvent:
    def __init__(self, origin=ORIGIN, message_id="", chain=None, extras=None):
        self.unified_msg_origin = origin
        self.message_obj = FakeMessageObj(message_id)
        self._extras = dict(extras or {})
        self._result = (
            types.SimpleNamespace(chain=list(chain)) if chain is not None else None
        )

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self._result


class FakeProvider:
    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts = []

    async def text_chat(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        text = self.texts.pop(0) if self.texts else ""
        return types.SimpleNamespace(completion_text=text, reasoning_content="")


class FakePersonaManager:
    personas_v3 = []

    async def get_default_persona_v3(self, origin):
        return {"name": "小盐-new", "prompt": "你是小盐。"}


class FakeConvManager:
    async def get_curr_conversation_id(self, origin):
        return "cid-1"

    async def get_conversation(self, origin, cid):
        return types.SimpleNamespace(history="[]", persona_id="小盐-new")


class FakeContext:
    def __init__(self, judge=None, gen=None):
        self._judge = judge
        self._gen = gen
        self.conversation_manager = FakeConvManager()
        self.persona_manager = FakePersonaManager()
        self.platform_manager = types.SimpleNamespace(get_insts=lambda: [])

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


def _make_plugin(config_extra=None):
    raw = {
        "enable": True,
        "judge": {"provider_id": "judge-provider"},
        "generate": {"provider_id": "gen-provider"},
    }
    if config_extra:
        raw.update(config_extra)
    return Main(FakeContext(), raw)


def _job(message_id=None, **kwargs):
    payload = {
        "message_id": message_id or new_message_id(),
        "origin": ORIGIN,
        "prompt": "插话提示词",
        "system_prompt": "你是小盐。",
        "query_text": "群聊最近的几句话",
        "self_id": "6000000000",
        "group_id": "400000000",
        "platform": "aiocqhttp",
    }
    payload.update(kwargs)
    return PipelineJob(**payload)


def _req(**kwargs):
    req = types.SimpleNamespace(
        prompt="原始 prompt",
        system_prompt="<system_reminder>User ID: 6000000000, Nickname: 小盐-new\n"
        "Current datetime: 2026-09-12 00:00 (CST)</system_reminder>",
        image_urls=[],
        func_tool=object(),
        conversation=object(),
    )
    for key, value in kwargs.items():
        setattr(req, key, value)
    return req


# ---------------- 登记表：marker 识别 ----------------


def test_new_message_id_carries_marker():
    mid = new_message_id()
    assert mid.startswith(MARKER_PREFIX)
    assert len(mid) > len(MARKER_PREFIX)


def test_dispatcher_ignores_foreign_events():
    plugin = _make_plugin()
    plugin.dispatcher.register(_job())
    foreign = FakeEvent(message_id="1234567890")
    assert plugin.dispatcher.find(foreign) is None
    assert plugin.dispatcher.take(foreign) is None


def test_dispatcher_finds_and_takes_registered_job():
    plugin = _make_plugin()
    job = _job()
    plugin.dispatcher.register(job)
    event = FakeEvent(message_id=job.message_id)

    assert plugin.dispatcher.find(event) is job
    assert plugin.dispatcher.take(event) is job
    assert plugin.dispatcher.find(event) is None


# ---------------- 注入：合成事件 ----------------


def _install_fake_aiocqhttp(monkeypatch):
    """把 aiocqhttp 的事件类/适配器换成可观测的假实现。"""

    class FakeAiocqhttpAdapter:
        def __init__(self):
            self.metadata = types.SimpleNamespace(id="bot", name="aiocqhttp")
            self.bot = object()
            self.events = []

        def commit_event(self, event):
            self.events.append(event)

    class FakeAiocqhttpMessageEvent:
        def __init__(
            self, message_str, message_obj, platform_meta, session_id, bot
        ):
            self.message_str = message_str
            self.message_obj = message_obj
            self.platform_meta = platform_meta
            self.session_id = session_id
            self.bot = bot
            self._extras = {}
            self.is_wake = False
            self.is_at_or_wake_command = False

        def set_extra(self, key, value):
            self._extras[key] = value

    adapter = FakeAiocqhttpAdapter()

    base = "astrbot.core.platform.sources.aiocqhttp"
    mod_event = types.ModuleType(f"{base}.aiocqhttp_message_event")
    mod_event.AiocqhttpMessageEvent = FakeAiocqhttpMessageEvent
    mod_adapter = types.ModuleType(f"{base}.aiocqhttp_platform_adapter")
    mod_adapter.AiocqhttpAdapter = FakeAiocqhttpAdapter
    monkeypatch.setitem(sys.modules, f"{base}.aiocqhttp_message_event", mod_event)
    monkeypatch.setitem(sys.modules, f"{base}.aiocqhttp_platform_adapter", mod_adapter)
    return adapter


def test_dispatch_rejects_non_aiocqhttp(monkeypatch):
    adapter = _install_fake_aiocqhttp(monkeypatch)
    plugin = _make_plugin()
    plugin.context.platform_manager = types.SimpleNamespace(
        get_insts=lambda: [adapter]
    )
    job = _job(platform="telegram")

    assert asyncio.run(plugin.dispatcher.dispatch(job)) is False
    assert adapter.events == []
    assert plugin.dispatcher.find(FakeEvent(message_id=job.message_id)) is None


def test_dispatch_requires_group_and_self_id(monkeypatch):
    adapter = _install_fake_aiocqhttp(monkeypatch)
    plugin = _make_plugin()
    plugin.context.platform_manager = types.SimpleNamespace(
        get_insts=lambda: [adapter]
    )
    assert asyncio.run(plugin.dispatcher.dispatch(_job(group_id=""))) is False
    assert asyncio.run(plugin.dispatcher.dispatch(_job(self_id=""))) is False
    assert adapter.events == []


def test_dispatch_without_adapter_returns_false():
    plugin = _make_plugin()
    assert asyncio.run(plugin.dispatcher.dispatch(_job())) is False


def test_dispatch_commits_wake_event(monkeypatch):
    adapter = _install_fake_aiocqhttp(monkeypatch)
    plugin = _make_plugin({"dispatch": {"mode": "pipeline"}})
    plugin.context.platform_manager = types.SimpleNamespace(
        get_insts=lambda: [adapter]
    )
    job = _job(
        provider_id="gen-provider",
        query_text="最近群聊说了三X",
        nickname="小盐-new",
    )

    assert asyncio.run(plugin.dispatcher.dispatch(job)) is True
    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.message_str == "最近群聊说了三X"
    assert event.message_obj.group_id == "400000000"
    assert event.message_obj.session_id == "400000000"
    assert event.message_obj.self_id == "6000000000"
    assert event.message_obj.message_id == job.message_id
    assert event.message_obj.sender.user_id == "6000000000"
    # At 段 + 显式唤醒标记：不依赖 wake_prefix 配置也能进主管道
    assert str(event.message_obj.message[0].qq) == "6000000000"
    assert event.is_wake is True
    assert event.is_at_or_wake_command is True
    assert event._extras["selected_provider"] == "gen-provider"
    # 载荷已登记，供后续钩子按 message_id 找回
    assert plugin.dispatcher.get(job.message_id) is job


# ---------------- 钩子：on_llm_request ----------------


def test_llm_request_hook_rewrites_prompt_and_drops_conversation():
    plugin = _make_plugin({"dispatch": {"mode": "pipeline"}})
    job = _job(
        prompt="插话专用提示词",
        system_prompt="你是小盐。",
        query_text="群聊上下文",
        image_urls=["http://img/1.png"],
    )
    plugin.dispatcher.register(job)
    req = _req()

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    assert req.prompt == "插话专用提示词"
    assert req.system_prompt.startswith("你是小盐。")
    assert req.image_urls == ["http://img/1.png"]
    # 默认不挂核心会话、不允许工具
    assert req.conversation is None
    assert req.func_tool is None


def test_llm_request_hook_strips_bot_self_identity_reminder():
    plugin = _make_plugin()
    job = _job()
    plugin.dispatcher.register(job)
    req = _req()

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    assert "User ID: 6000000000, Nickname: 小盐-new" not in req.system_prompt
    assert "Current datetime" in req.system_prompt


def test_llm_request_hook_keeps_conversation_and_tools_when_configured():
    plugin = _make_plugin(
        {"dispatch": {"mode": "pipeline", "allow_tools": True, "keep_conversation": True}}
    )
    job = _job()
    plugin.dispatcher.register(job)
    tool_set = object()
    req = _req(func_tool=tool_set)
    conversation = req.conversation

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    assert req.func_tool is tool_set
    assert req.conversation is conversation


def test_llm_request_hook_ignores_direct_mode_events():
    plugin = _make_plugin()
    req = _req()
    before = (req.prompt, req.system_prompt)

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id="1234567890"), req)
    )

    assert (req.prompt, req.system_prompt) == before
    assert req.conversation is not None


# ---------------- 钩子：on_decorating_result ----------------


def _decorate(plugin, job, chain):
    event = FakeEvent(message_id=job.message_id, chain=chain)
    asyncio.run(plugin.on_pipeline_decorating_result(event))
    return event.get_result().chain


def test_decorating_result_parses_tags_and_quotes():
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(allowed_msg_ids={"1001"}, reply_targets=["1001"])
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain('<quote id="1001"/>三X是姐X喵')])

    assert isinstance(chain[0], Reply)
    assert chain[0].id == "1001"
    assert isinstance(chain[1], Plain)
    assert "quote" not in chain[1].text
    assert job.final_text == "三X是姐X喵"


def test_decorating_result_drops_hallucinated_quote():
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(allowed_msg_ids={"1001"})
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain('<quote id="9999"/>三X是姐X喵')])

    assert not any(isinstance(c, Reply) for c in chain)
    assert job.final_text == "三X是姐X喵"


def test_decorating_result_forces_quote_for_question():
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(allowed_msg_ids={"1001"}, reply_targets=["1001"], should_quote=True)
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain("三X是姐X喵")])

    assert isinstance(chain[0], Reply)
    assert chain[0].id == "1001"


def test_decorating_result_respects_refuse():
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job()
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain("<refuse/>")])

    assert chain == []
    assert job.dropped is True


def test_decorating_result_drops_when_state_was_reset():
    plugin = _make_plugin()
    job = _job()  # 没有 runtime.touch → 会话状态已被 /reset 清理
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain("三X是姐X喵")])

    assert chain == []
    assert job.dropped is True


def test_decorating_result_drops_duplicate_reply():
    plugin = _make_plugin()
    state = plugin.runtime.touch(ORIGIN)
    state.recent_bot_replies.append("三X就是user啦")
    job = _job()
    plugin.dispatcher.register(job)

    chain = _decorate(plugin, job, [Plain("三X就是user啦")])

    assert chain == []
    assert job.dropped is True


def test_decorating_result_ignores_other_plugins_events():
    plugin = _make_plugin()
    event = FakeEvent(message_id="1234567890", chain=[Plain("<refuse/>")])

    asyncio.run(plugin.on_pipeline_decorating_result(event))

    chain = event.get_result().chain
    assert len(chain) == 1
    assert chain[0].text == "<refuse/>"


# ---------------- 发送后写回 ----------------


def test_after_message_sent_records_pipeline_reply():
    plugin = _make_plugin()
    state = plugin.runtime.touch(ORIGIN)
    job = _job(reply_targets=["1001"])
    job.final_text = "三X是姐X喵"
    plugin.dispatcher.register(job)

    asyncio.run(plugin.on_after_message_sent(FakeEvent(message_id=job.message_id)))

    assert state.session_chats[-1].startswith("[You/")
    assert "三X是姐X喵" in state.session_chats[-1]
    assert list(state.recent_bot_replies)[-1] == "三X是姐X喵"
    assert "1001" in state.replied_registry
    assert state.last_reply_ts > 0
    # 载荷已消费
    assert plugin.dispatcher.get(job.message_id) is None


def test_after_message_sent_skips_dropped_replies():
    plugin = _make_plugin()
    state = plugin.runtime.touch(ORIGIN)
    job = _job()
    job.final_text = "不该被记录"
    job.dropped = True
    plugin.dispatcher.register(job)

    asyncio.run(plugin.on_after_message_sent(FakeEvent(message_id=job.message_id)))

    assert state.session_chats == []
    assert list(state.recent_bot_replies) == []


# ---------------- _handle_pending：模式切换 ----------------


def _judge_reply(norm_id="1001"):
    return json.dumps({"decision": "reply", "target_ids": [f"msg{norm_id}"]})


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


def _plugin_for_pending(mode, judge_texts=None, gen_texts=None):
    judge = FakeProvider(judge_texts or [_judge_reply()])
    gen = FakeProvider(gen_texts or ["直连回复"])
    raw = {
        "enable": True,
        "judge": {"provider_id": "judge-provider"},
        "generate": {"provider_id": "gen-provider"},
        "dispatch": {"mode": mode},
    }
    plugin = Main(FakeContext(judge, gen), raw)
    _seed_pending(plugin)
    return plugin, judge, gen


async def _memory_block(**kwargs):
    return "【相关长期记忆】user是窝的姐X"


async def _empty_kb(*args, **kwargs):
    return ""


def _stub_recall(monkeypatch, plugin):
    monkeypatch.setattr(plugin.memory, "recall", _memory_block)
    monkeypatch.setattr(main_mod, "retrieve_kb_block", _empty_kb)


def _record_sends(monkeypatch):
    sent = []

    async def fake_send(origin, chain):
        sent.append((origin, chain))
        return True

    monkeypatch.setattr(main_mod.StarTools, "send_message", fake_send)
    return sent


def test_pending_pipeline_mode_dispatches_instead_of_generating(monkeypatch):
    plugin, judge, gen = _plugin_for_pending("pipeline")
    _stub_recall(monkeypatch, plugin)
    sent = _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert len(jobs) == 1
    job = jobs[0]
    assert job.origin == ORIGIN
    assert job.group_id == "400000000"
    assert job.self_id == "6000000000"
    assert job.provider_id == "gen-provider"
    assert job.reply_targets == ["1001"]
    assert job.should_quote is True
    assert "1001" in job.allowed_msg_ids
    # 召回块仍给判定用，但生成侧交给主管道（不重复注入）
    assert "user是窝的姐X" in judge.prompts[0]
    assert "user是窝的姐X" not in job.prompt
    # 没有走插件自己的生成/发送
    assert gen.prompts == []
    assert sent == []


def test_pending_pipeline_mode_falls_back_to_direct(monkeypatch):
    plugin, judge, gen = _plugin_for_pending("pipeline")
    _stub_recall(monkeypatch, plugin)
    sent = _record_sends(monkeypatch)

    async def failing_dispatch(job):
        return False

    monkeypatch.setattr(plugin.dispatcher, "dispatch", failing_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    # 回退直连：插件自己生成并发送，且召回块仍在生成提示词里
    assert len(gen.prompts) == 1
    assert "user是窝的姐X" in gen.prompts[0]
    assert len(sent) == 1


def test_pending_direct_mode_does_not_dispatch(monkeypatch):
    plugin, judge, gen = _plugin_for_pending("direct")
    _stub_recall(monkeypatch, plugin)
    sent = _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):  # pragma: no cover - 不应被调用
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert jobs == []
    assert len(gen.prompts) == 1
    assert len(sent) == 1
