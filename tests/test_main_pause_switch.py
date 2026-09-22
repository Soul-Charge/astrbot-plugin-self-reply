"""自主回复开关（/reply，任务书 v3）单测。

覆盖：paused 群消息零判定 + 不写 pending、_handle_pending 双保险、
关闭期间 session_chats 仍写入、白名单留空=插件关闭、/reply 状态迁移与
KV 写入、群聊里的 /reply 插件不响应、白名单留空时 on/off 不写 KV、
stat 输出、以及 /reply 的 ADMIN + 指令声明。
"""

import asyncio
import types

from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType

from astrbot_plugin_self_reply.main import (
    EMPTY_WHITELIST_NOTICE,
    KV_PAUSE_STATE_KEY,
    Main,
    format_duration,
)
from astrbot_plugin_self_reply.runtime_state import RuntimeState

BOT_QQ = "6000000000"
GROUP_ORIGIN = "bot:GroupMessage:400000000"
GROUP_ID = "400000000"
PRIVATE_ORIGIN = "bot:FriendMessage:8000000000"

PROVIDER_CALLS = []


class FakeProvider:
    async def text_chat(self, **kwargs):
        PROVIDER_CALLS.append(kwargs)
        return types.SimpleNamespace(completion_text="OK")


class FakeContext:
    def get_using_provider(self, umo=None):
        return FakeProvider()

    def get_config(self, umo=None):
        return {"kb_names": []}


class FakeEvent:
    def __init__(
        self,
        text="你好呀",
        message_id="1001",
        message_type=MessageType.GROUP_MESSAGE,
        is_wake=False,
        sender_id="8000000000",
        is_admin=False,
    ):
        self.message_str = text
        self.unified_msg_origin = (
            GROUP_ORIGIN if message_type == MessageType.GROUP_MESSAGE else PRIVATE_ORIGIN
        )
        self.is_at_or_wake_command = is_wake
        self._mt = message_type
        self._sender_id = sender_id
        self._is_admin = is_admin
        self._stopped = False
        self._sent = []
        self.message_obj = types.SimpleNamespace(
            sender=types.SimpleNamespace(nickname="E0"),
            message_id=message_id,
            self_id=BOT_QQ,
            group_id=GROUP_ID,
            # 历史行由消息组件拼出正文，故必须带一个 Plain，否则行内只有 header
            message=[Plain(text=text)] if text else [],
        )

    def get_message_type(self):
        return self._mt

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return GROUP_ID if self._mt == MessageType.GROUP_MESSAGE else ""

    def get_self_id(self):
        return BOT_QQ

    def is_admin(self):
        return self._is_admin

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key=None, default=None):
        return default

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    def plain_result(self, text):
        return ("plain", text)

    async def send(self, chain=None):
        self._sent.append(chain)


def make_plugin(whitelist=None, enable=True, context=None):
    raw = {"enable": enable}
    if whitelist is not None:
        raw["whitelist"] = {"allowed_origins": list(whitelist)}
    return Main(context if context is not None else FakeContext(), raw)


def drive(plugin, events):
    """喂事件，退出前清掉防抖任务，避免任务悬挂。"""

    async def runner():
        st = plugin.runtime.touch(GROUP_ORIGIN)
        for ev in events:
            await plugin.on_group_message(ev)
        task = st.debounce_task if st else None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return st

    return asyncio.run(runner())


def collect(agen):
    async def runner():
        return [item async for item in agen]

    return asyncio.run(runner())


def collect_and_capture(plugin, event):
    async def runner():
        out = []
        async for item in plugin.reply_command(event):
            out.append(item)
        return out

    return asyncio.run(runner())


# ---------------- 1/2/3/4：关闭态与白名单语义 ----------------


def test_paused_group_message_skips_pending_and_debounce():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    PROVIDER_CALLS.clear()

    async def runner():
        await plugin._set_paused(True)
        st = plugin.runtime.touch(GROUP_ORIGIN)
        await plugin.on_group_message(FakeEvent("今天好无聊啊", message_id="2001"))
        task = st.debounce_task
        return st, task

    st, task = asyncio.run(runner())
    assert st.pending_messages == []
    assert task is None
    assert PROVIDER_CALLS == []
    assert len(st.session_chats) == 1


def test_handle_pending_guard_blocks_in_flight_batch():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    PROVIDER_CALLS.clear()

    async def runner():
        await plugin._set_paused(True)
        st = plugin.runtime.touch(GROUP_ORIGIN)
        st.pending_messages.append(
            {"norm_id": "1", "nick": "E0", "sender_id": "1", "text": "hi",
             "has_image": False, "role": "(member)", "ts": 0.0}
        )
        await plugin._handle_pending(GROUP_ORIGIN)
        return st

    st = asyncio.run(runner())
    assert PROVIDER_CALLS == []
    assert len(st.pending_messages) == 1


def test_session_chats_still_written_while_paused():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])

    async def runner():
        await plugin._set_paused(True)
        await plugin.on_group_message(FakeEvent("关闭期间的一句话", message_id="3001"))
        await plugin.on_group_message(FakeEvent("第二句", message_id="3002"))
        return plugin.runtime.get(GROUP_ORIGIN)

    st = asyncio.run(runner())
    assert len(st.session_chats) == 2
    assert any("关闭期间的一句话" in line for line in st.session_chats)
    assert st.pending_messages == []


def test_empty_whitelist_equals_plugin_off():
    plugin = make_plugin(whitelist=[])
    PROVIDER_CALLS.clear()
    st = drive(plugin, [FakeEvent("白名单为空时也不该入队", message_id="4001")])
    assert st.pending_messages == []
    assert st.debounce_task is None
    assert PROVIDER_CALLS == []
    # 白名单为空 == 插件关闭：连历史都不必积累（与 enable=False 行为一致）
    assert st.session_chats == []


def test_non_empty_whitelist_still_works():
    plugin = make_plugin(whitelist=[GROUP_ID])
    st = drive(plugin, [FakeEvent("白名单命中，正常入队", message_id="4002")])
    assert len(st.pending_messages) == 1
    assert st.pending_messages[0]["text"] == "白名单命中，正常入队"


def test_unlisted_group_is_ignored():
    plugin = make_plugin(whitelist=["999999"])
    st = drive(plugin, [FakeEvent("不在白名单")])
    assert st.pending_messages == []
    assert st.debounce_task is None


# ---------------- 5：/reply on / off 状态迁移 + KV ----------------


def test_reply_off_then_on_transitions_and_persists():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    writes = []

    async def fake_put(key, value):
        writes.append((key, dict(value)))

    plugin.put_kv_data = fake_put

    out_off = collect_and_capture(plugin, FakeEvent("/reply off", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    assert plugin._paused is True
    assert plugin._paused_at is not None
    assert writes[-1][0] == KV_PAUSE_STATE_KEY
    assert writes[-1][1]["paused"] is True
    assert str(out_off[0][1]).startswith("自主回复已关闭")

    out_on = collect_and_capture(plugin, FakeEvent("/reply on", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    assert plugin._paused is False
    assert plugin._paused_at is None
    assert writes[-1][1] == {"paused": False, "paused_at": None, "paused_origins": []}
    assert str(out_on[0][1]).startswith("自主回复已开启")


def test_reply_extra_parameters_target_that_group():
    """语义变更：带群号不再是「被忽略」，而是只关该群（全局开关不动）。"""
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    plugin.put_kv_data = _noop_put
    out = collect_and_capture(
        plugin,
        FakeEvent("/reply off " + GROUP_ORIGIN.rsplit(":", 1)[-1], message_type=MessageType.FRIEND_MESSAGE, is_admin=True),
    )
    assert plugin._paused is False  # 全局不受影响
    assert plugin._paused_origins == {GROUP_ORIGIN}
    assert str(out[0][1]).startswith("已关闭群")


def test_reply_without_args_shows_usage_and_changes_nothing():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    plugin.put_kv_data = _noop_put
    out = collect_and_capture(plugin, FakeEvent("/reply", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    assert plugin._paused is False
    assert "用法" in str(out[0][1])


def test_load_pause_state_from_kv():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])

    async def fake_get(key, default=None):
        return {"paused": True, "paused_at": 1234.5}

    plugin.get_kv_data = fake_get
    asyncio.run(plugin.initialize())
    assert plugin._paused is True
    assert plugin._paused_at == 1234.5


def test_load_pause_state_survives_kv_failure():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])

    async def boom(key, default=None):
        raise RuntimeError("db down")

    plugin.get_kv_data = boom
    asyncio.run(plugin.initialize())
    assert plugin._paused is False


# ---------------- 6：群聊里 /reply 插件不响应 ----------------


def test_reply_in_group_is_not_answered():
    plugin = make_plugin(whitelist=[GROUP_ORIGIN])
    ev = FakeEvent("/reply off", message_type=MessageType.GROUP_MESSAGE, is_admin=True)
    out = collect_and_capture(plugin, ev)
    assert out == []
    assert ev._sent == []
    assert ev.is_stopped() is False
    assert plugin._paused is False


# ---------------- 7/8：白名单留空时命令只提示 ----------------


def test_empty_whitelist_on_off_do_not_write_kv():
    plugin = make_plugin(whitelist=[])
    writes = []

    async def fake_put(key, value):
        writes.append((key, value))

    plugin.put_kv_data = fake_put

    out_on = collect_and_capture(plugin, FakeEvent("/reply on", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    assert plugin._paused is False
    assert out_on[0][1] == EMPTY_WHITELIST_NOTICE

    out_off = collect_and_capture(plugin, FakeEvent("/reply off", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    assert plugin._paused is False
    assert out_off[0][1] == EMPTY_WHITELIST_NOTICE
    assert writes == []


def test_empty_whitelist_stat_is_readonly_notice():
    plugin = make_plugin(whitelist=[])
    out = collect_and_capture(plugin, FakeEvent("/reply stat", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    text = str(out[0][1])
    assert text.startswith(EMPTY_WHITELIST_NOTICE)
    assert "当前开关" in text


def test_stat_with_whitelist_lists_groups_and_state():
    plugin = make_plugin(whitelist=[GROUP_ID, "999999"])
    plugin._paused = True
    plugin._paused_at = 1000.0
    st = plugin.runtime.touch(GROUP_ORIGIN)
    st.session_chats = ["[E0/1/18:04:49](member) #msg1: 历史一行"]

    out = collect_and_capture(plugin, FakeEvent("/reply stat", message_type=MessageType.FRIEND_MESSAGE, is_admin=True))
    text = str(out[0][1])
    assert "关闭" in text
    assert GROUP_ID in text
    assert "999999" in text
    assert "历史 1 条" in text


# ---------------- 9：ADMIN 与指令声明 ----------------


def test_reply_command_declares_admin_and_command_filters():
    """真实注册表里 /reply 必须同时挂 ADMIN 权限与 reply 指令过滤器。"""
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.permission import PermissionTypeFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = [
        h
        for h in star_handlers_registry.get_handlers_by_module_name(Main.__module__)
        if h.handler_name == "reply_command"
    ]
    assert handlers, "reply_command 未出现在 handler 注册表中"
    filters = handlers[0].event_filters
    assert any(isinstance(f, PermissionTypeFilter) for f in filters), "缺少 ADMIN 权限过滤"
    cmds = [f for f in filters if isinstance(f, CommandFilter)]
    assert cmds and cmds[0].command_name == "reply", "缺少 reply 指令过滤"


# ---------------- 10：内部辅助 ----------------


def test_format_duration_renders_units():
    assert format_duration(0) == "0秒"
    assert format_duration(59) == "59秒"
    assert format_duration(60) == "1分"
    assert format_duration(3661) == "1小时1分1秒"
    assert format_duration(90061) == "1天1小时1分1秒"


def test_runtime_state_has_no_paused_field():
    """开关是全局的（v3）：OriginState 不再带 paused 字段。"""
    assert not hasattr(RuntimeState().touch("x"), "paused")


async def _noop_put(key, value):
    return None
