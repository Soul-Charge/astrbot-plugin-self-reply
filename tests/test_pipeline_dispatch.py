"""主管道派发（pipeline dispatch）：合成事件注入 + 三个往返钩子。"""

import asyncio
import json
import sys
import time
import types

import astrbot_plugin_self_reply.main as main_mod
from astrbot_plugin_self_reply.main import Main
from astrbot_plugin_self_reply.pipeline_dispatch import (
    DEFAULT_CHAT_TOOLS,
    DEFAULT_FALLBACK_TEXT,
    MARKER_PREFIX,
    PipelineJob,
    kept_tool_names,
    new_message_id,
    normalize_chat_tool_mode,
    normalize_fallback_on,
    should_inject_fallback,
)
from astrbot_plugin_self_reply.runtime_state import OriginState

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
        "director_note": "插话提示词",
        "system_prompt": "你是小盐。",
        "query_text": "群聊最近的几句话",
        "self_id": "6000000000",
        "group_id": "400000000",
        "platform": "aiocqhttp",
    }
    payload.update(kwargs)
    return PipelineJob(**payload)


class FakePart:
    """假的 TextPart：生产代码用 type(part)(text=...) 重建，形状一致即可。"""

    def __init__(self, text):
        self.text = text


def _req(**kwargs):
    req = types.SimpleNamespace(
        prompt="原始 prompt",
        system_prompt="你是小盐。",
        image_urls=[],
        func_tool=object(),
        conversation=object(),
        # P0a：身份提醒的真实注入点是 extra_user_content_parts
        extra_user_content_parts=[
            FakePart(
                "<system_reminder>User ID: 6000000000, Nickname: 小盐-new\n"
                "Current datetime: 2026-09-12 00:00 (CST)</system_reminder>"
            )
        ],
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


def test_llm_request_hook_rewrites_prompt_and_keeps_capabilities():
    """P0a：写导演指令 + 保留工具集 + 默认挂核心会话 + 不重复注入人格。"""
    plugin = _make_plugin({"dispatch": {"mode": "pipeline"}})
    job = _job(
        director_note="插话专用提示词",
        query_text="群聊上下文",
        image_urls=["http://img/1.png"],
    )
    plugin.dispatcher.register(job)
    tool_set = object()
    req = _req(func_tool=tool_set)
    conversation = req.conversation
    system_prompt = req.system_prompt

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    assert req.prompt == "插话专用提示词"
    assert req.image_urls == ["http://img/1.png"]
    # 工具集不再被阉割；默认挂核心会话
    assert req.func_tool is tool_set
    assert req.conversation is conversation
    # 人格由核心原生注入，插件不再触碰 system_prompt
    assert req.system_prompt == system_prompt


def test_llm_request_hook_strips_bot_self_identity_reminder():
    """身份提醒在 extra_user_content_parts 里被改写，且幂等。"""
    plugin = _make_plugin()
    job = _job()
    plugin.dispatcher.register(job)
    req = _req()

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    parts = req.extra_user_content_parts
    assert len(parts) == 1
    assert "User ID: 6000000000, Nickname: 小盐-new" not in parts[0].text
    assert "You are the sender of this turn" in parts[0].text
    assert "Current datetime" in parts[0].text

    # 幂等：再跑一遍不应报错、也不应再改（marker 已消失）
    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )
    assert parts[0].text == req.extra_user_content_parts[0].text


def test_llm_request_hook_drops_conversation_only_when_disabled():
    plugin = _make_plugin(
        {"dispatch": {"mode": "pipeline", "attach_core_conversation": False}}
    )
    job = _job()
    plugin.dispatcher.register(job)
    tool_set = object()
    req = _req(func_tool=tool_set)

    asyncio.run(
        plugin.on_pipeline_llm_request(FakeEvent(message_id=job.message_id), req)
    )

    # 关掉挂载时才置 None；工具永远保留
    assert req.conversation is None
    assert req.func_tool is tool_set


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


def test_tool_round_empty_result_does_not_block_later_bookkeeping():
    """遗留缺陷 3 回归：工具轮的空结果不能把整轮的记账粘掉。

    真实时序（2026-09-18 01:16 线上 task 轮）：模型先只回 tool_calls、
    content 为空 -> 主管道对这一轮跑 on_decorating_result（空链）->
    job.dropped=True；工具结果回灌后模型给出真答案 -> 再跑一次
    on_decorating_result。修复点：接受结果时复位 dropped，于是
    after_message_sent 照常写回（否则防重复登记、last_reply_ts、
    session_chats 会静默丢失）。
    """
    plugin = _make_plugin()
    state = plugin.runtime.touch(ORIGIN)
    job = _job(reply_targets=["1001"])
    plugin.dispatcher.register(job)

    # 第一轮：工具轮，只有 tool_calls，没有可发送内容
    assert _decorate(plugin, job, []) == []
    assert job.dropped is True

    # 第二轮：工具结果灌回后的真答案，必须能把取消标记复位
    chain = _decorate(plugin, job, [Plain("第28话「怀玉—之肆—」")])
    assert chain
    assert job.dropped is False
    assert job.final_text == "第28话「怀玉—之肆—」"

    asyncio.run(plugin.on_after_message_sent(FakeEvent(message_id=job.message_id)))

    assert state.session_chats[-1].endswith("第28话「怀玉—之肆—」")
    assert list(state.recent_bot_replies)[-1] == "第28话「怀玉—之肆—」"
    assert "1001" in state.replied_registry
    assert state.last_reply_ts > 0


def test_decorating_result_empty_round_log_is_not_a_failure(caplog):
    """空轮（工具轮）的日志不能再写成「取消发送」，免得看起来像故障。"""
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job()
    plugin.dispatcher.register(job)

    with caplog.at_level("INFO"):
        _decorate(plugin, job, [])

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "本轮无结果（工具轮/空回复）" in line
    assert "取消发送" not in line


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
    assert "user是窝的姐X" not in job.director_note
    # P0a：导演指令由 kind 决定模板，且默认带上近况块
    assert job.kind == "chat"
    assert "#msg1001" in job.director_note
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

# ---------------- P0b：按 kind 收窄工具集 ----------------


CURRENT_TOOLS = [
    "llm_poke_user",
    "search_memes",
    "bilibili_read",
    "web_search_tavily",
    "send_message_to_user",
]


def test_normalize_chat_tool_mode_tolerates_junk():
    assert normalize_chat_tool_mode("READONLY") == "readonly"
    assert normalize_chat_tool_mode(" none ") == "none"
    assert normalize_chat_tool_mode("all") == "all"
    # 写错一律回退最保守的 none，而不是悄悄按 readonly 跑
    assert normalize_chat_tool_mode("readonlyy") == "none"
    assert normalize_chat_tool_mode("") == "none"
    assert normalize_chat_tool_mode(None) == "none"


def test_kept_tool_names_task_always_keeps_everything():
    """派活要真本事：task 类不受任何策略影响。"""
    for mode in ("none", "readonly", "all", "typo"):
        assert (
            kept_tool_names(
                kind="task", mode=mode, allowlist=DEFAULT_CHAT_TOOLS, current=CURRENT_TOOLS
            )
            == CURRENT_TOOLS
        )


def test_kept_tool_names_chat_none_drops_everything():
    assert (
        kept_tool_names(
            kind="chat", mode="none", allowlist=DEFAULT_CHAT_TOOLS, current=CURRENT_TOOLS
        )
        == []
    )


def test_kept_tool_names_chat_readonly_keeps_allowlist_in_original_order():
    given = ["web_search_tavily", "search_memes", "llm_poke_user", "bilibili_read"]
    assert kept_tool_names(
        kind="chat",
        mode="readonly",
        allowlist=("bilibili_read", "search_memes"),
        current=given,
    ) == ["search_memes", "bilibili_read"]


def test_kept_tool_names_chat_readonly_ignores_names_not_present():
    assert kept_tool_names(
        kind="chat",
        mode="readonly",
        allowlist=("search_memes", "已经卸载的工具"),
        current=CURRENT_TOOLS,
    ) == ["search_memes"]


def test_kept_tool_names_chat_all_mode_is_a_noop():
    assert (
        kept_tool_names(
            kind="chat", mode="all", allowlist=(), current=CURRENT_TOOLS
        )
        == CURRENT_TOOLS
    )


def test_kept_tool_names_unknown_kind_is_treated_as_chat():
    """判定失败时 kind 兜底成 chat，工具收窄也必须跟着保守。"""
    assert (
        kept_tool_names(
            kind="", mode="readonly", allowlist=(), current=CURRENT_TOOLS
        )
        == []
    )


def test_kept_tool_names_does_not_mutate_current():
    given = list(CURRENT_TOOLS)
    kept = kept_tool_names(
        kind="task", mode="none", allowlist=(), current=given
    )
    kept.append("注入的假工具")
    assert given == CURRENT_TOOLS


# ---------------- P1b：空回复兜底（分支与范围） ----------------


def test_normalize_fallback_on_tolerates_junk():
    assert normalize_fallback_on("TASK") == "task"
    assert normalize_fallback_on(" always ") == "always"
    assert normalize_fallback_on("off") == "off"
    # 写错一律回退默认档 task，而不是悄悄按 always 跑
    assert normalize_fallback_on("alwayss") == "task"
    assert normalize_fallback_on("") == "task"
    assert normalize_fallback_on(None) == "task"


def test_should_inject_fallback_default_only_for_task():
    assert should_inject_fallback(kind="task", mode="task") is True
    assert should_inject_fallback(kind="chat", mode="task") is False
    assert should_inject_fallback(kind="", mode="task") is False
    assert should_inject_fallback(kind="chat", mode="always") is True
    assert should_inject_fallback(kind="task", mode="off") is False
    # 非法值按默认档处理
    assert should_inject_fallback(kind="task", mode="typo") is True
    assert should_inject_fallback(kind="chat", mode="typo") is False


def _decorate_result(plugin, job, chain):
    event = FakeEvent(message_id=job.message_id, chain=chain)
    asyncio.run(plugin.on_pipeline_decorating_result(event))
    return event.get_result().chain


def test_fallback_injected_for_task_when_result_has_no_text():
    """task 轮有结果但没文本（只剩引用标签）-> 注入兜底文案并照常记账。"""
    plugin = _make_plugin()
    state = plugin.runtime.touch(ORIGIN)
    job = _job(kind="task", reply_targets=["1001"], allowed_msg_ids={"1001"})
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>')])

    assert job.dropped is False
    assert job.final_text == DEFAULT_FALLBACK_TEXT
    assert [c.text for c in chain if isinstance(c, Plain)] == [DEFAULT_FALLBACK_TEXT]

    asyncio.run(plugin.on_after_message_sent(FakeEvent(message_id=job.message_id)))

    assert DEFAULT_FALLBACK_TEXT in state.session_chats[-1]
    assert "1001" in state.replied_registry
    assert state.last_reply_ts > 0


def test_fallback_keeps_existing_non_text_components():
    """引用/图片这类非文本组件要保留，只把空文本换成兜底文案。"""
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="task", reply_targets=["1001"], allowed_msg_ids={"1001"})
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>'), Plain("   ")])

    assert isinstance(chain[0], Reply)
    assert [c.text for c in chain if isinstance(c, Plain)] == [DEFAULT_FALLBACK_TEXT]


def test_fallback_not_injected_for_chat_by_default():
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="chat")
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>')])

    assert chain == []
    assert job.dropped is True


def test_empty_chain_never_gets_fallback_even_for_task():
    """工具轮 / 被别的钩子清掉的空链：绝不注入兜底（规划 §4.4 修正）。

    主管道在结果链为空时根本不会跑装饰钩子（result_decorate/stage.py:131），
    线上能看到的空链只可能是前面的钩子把结果清掉了（2026-09-18 01:28:21
    实测是 meme_manager 清掉工具轮的工具状态消息），后面还会再来一轮真答案。
    """
    plugin = _make_plugin()
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="task")
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [])

    assert chain == []
    assert job.dropped is True
    assert job.final_text == ""


def test_fallback_off_is_silent_even_for_task():
    plugin = _make_plugin({"dispatch": {"fallback_on": "off"}})
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="task")
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>')])

    assert chain == []
    assert job.dropped is True


def test_fallback_always_applies_to_chat_too():
    plugin = _make_plugin({"dispatch": {"fallback_on": "always"}})
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="chat")
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>')])

    assert job.dropped is False
    assert [c.text for c in chain if isinstance(c, Plain)] == [DEFAULT_FALLBACK_TEXT]


def test_fallback_text_is_configurable():
    plugin = _make_plugin({"dispatch": {"fallback_text": "算不出来喵"}})
    plugin.runtime.touch(ORIGIN)
    job = _job(kind="task")
    plugin.dispatcher.register(job)

    chain = _decorate_result(plugin, job, [Plain('<quote id="1001"/>')])

    assert [c.text for c in chain if isinstance(c, Plain)] == ["算不出来喵"]


# ---------------- P1b：冷却顺序（判定后拦 + 不丢消息） ----------------


def _judge_reply_with_kind(norm_id="1001", kind="chat"):
    payload = {"decision": "reply", "target_ids": [f"msg{norm_id}"]}
    if kind is not None:
        payload["kind"] = kind
    return json.dumps(payload)


def _plugin_on_cooldown(mode="pipeline", judge_texts=None):
    """构造一个「刚回过话」的会话：on_cooldown 必然命中。"""
    plugin, judge, gen = _plugin_for_pending(
        mode, judge_texts or [_judge_reply_with_kind()]
    )
    plugin.runtime.get(ORIGIN).last_reply_ts = time.time()
    return plugin, judge, gen


def test_pending_task_bypasses_cooldown(monkeypatch):
    """派活消息落在冷却窗口内 -> 仍然接住（规划 §4.2 方案 A）。"""
    plugin, judge, gen = _plugin_on_cooldown(
        judge_texts=[_judge_reply_with_kind(kind="task")]
    )
    _stub_recall(monkeypatch, plugin)
    _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    state = plugin.runtime.get(ORIGIN)
    assert len(jobs) == 1
    assert jobs[0].kind == "task"
    assert jobs[0].reply_targets == ["1001"]
    assert state.pending_messages == []
    assert state.deferred_judge is None


def test_pending_chat_inside_cooldown_is_deferred_not_dropped(monkeypatch):
    """闲聊批落在冷却窗口内 -> 写回 pending 并留下判定，不再凭空吞掉。"""
    plugin, judge, gen = _plugin_on_cooldown()
    state = plugin.runtime.get(ORIGIN)
    _stub_recall(monkeypatch, plugin)
    sent = _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):  # pragma: no cover - 冷却中不该派发
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert jobs == []
    assert sent == []
    assert [p["norm_id"] for p in state.pending_messages] == ["1001"]
    assert "judged_at" in state.pending_messages[0]
    assert state.deferred_judge == {"kind": "chat", "targets": ["1001"]}


def test_pending_deferred_batch_reuses_verdict_without_second_judge(monkeypatch):
    """冷却结束、又没有新消息 -> 复用上次判定，不再叫一次 judge（§4.3）。"""
    plugin, judge, gen = _plugin_on_cooldown()
    state = plugin.runtime.get(ORIGIN)
    _stub_recall(monkeypatch, plugin)
    _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))  # 冷却中 -> 延后
    assert jobs == []
    assert len(judge.prompts) == 1

    state.last_reply_ts = 0.0  # 冷却结束
    asyncio.run(plugin._handle_pending(ORIGIN))

    assert len(judge.prompts) == 1  # 同一批不会被判第二次
    assert len(jobs) == 1
    assert jobs[0].reply_targets == ["1001"]
    assert state.pending_messages == []
    assert state.deferred_judge is None


def test_pending_deferred_batch_is_rejudged_when_new_messages_arrive(monkeypatch):
    """延后期间来了新消息 -> 整批重新判定（新消息不会被旧判定盖住）。"""
    plugin, judge, gen = _plugin_for_pending(
        "pipeline", [_judge_reply_with_kind(), _judge_reply_with_kind("1002")]
    )
    state = plugin.runtime.get(ORIGIN)
    state.last_reply_ts = time.time()
    _stub_recall(monkeypatch, plugin)
    _record_sends(monkeypatch)
    jobs = []

    async def fake_dispatch(job):
        jobs.append(job)
        return True

    monkeypatch.setattr(plugin.dispatcher, "dispatch", fake_dispatch)

    asyncio.run(plugin._handle_pending(ORIGIN))  # 冷却中 -> 延后
    assert len(judge.prompts) == 1

    state.last_reply_ts = 0.0  # 冷却结束，且来了一条新消息
    new_entry = dict(state.pending_messages[0])
    new_entry.pop("judged_at", None)
    new_entry["norm_id"] = "1002"
    new_entry["text"] = "又来一条"
    state.pending_messages.append(new_entry)

    asyncio.run(plugin._handle_pending(ORIGIN))

    assert len(judge.prompts) == 2
    assert "#msg1001" in judge.prompts[1]
    assert "#msg1002" in judge.prompts[1]
    assert len(jobs) == 1
    assert jobs[0].reply_targets == ["1002"]


def test_requeue_pending_keeps_order_and_respects_cap():
    """写回是「插到队首保持原时序」，且仍受 pending 上限约束。"""
    state = OriginState()
    state.pending_messages = [{"norm_id": "old"}]
    batch = [{"norm_id": "a"}, {"norm_id": "b"}, {"norm_id": "c"}]

    Main._requeue_pending(state, batch, 3)

    assert [p["norm_id"] for p in state.pending_messages] == ["b", "c", "old"]
