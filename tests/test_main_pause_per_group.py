"""按群自主回复开关（/reply off|on <群号>）与 stat 群名显示的单测。"""

import asyncio
import types

from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType

from astrbot_plugin_self_reply.main import KV_PAUSE_STATE_KEY, Main

BOT_QQ = "6000000000"
GROUP_A = "400000000"
GROUP_B = "200000000"
ORIGIN_A = "bot:GroupMessage:400000000"
ORIGIN_B = "bot:GroupMessage:200000000"
PRIVATE_ORIGIN = "bot:FriendMessage:8000000000"
GROUP_NAMES = {GROUP_A: "测试群", GROUP_B: "测试群B"}

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
        text="你好",
        message_id="1001",
        message_type=MessageType.GROUP_MESSAGE,
        group_id=GROUP_A,
        is_admin=False,
        sender_id="8000000000",
    ):
        self.message_str = text
        self._mt = message_type
        self._gid = group_id
        self._sender_id = sender_id
        self._is_admin = is_admin
        self._stopped = False
        self._sent = []
        self.unified_msg_origin = (
            "bot:GroupMessage:" + group_id
            if message_type == MessageType.GROUP_MESSAGE
            else PRIVATE_ORIGIN
        )
        self.is_at_or_wake_command = False
        self.message_obj = types.SimpleNamespace(
            sender=types.SimpleNamespace(nickname="E0"),
            message_id=message_id,
            self_id=BOT_QQ,
            group_id=group_id,
            group=types.SimpleNamespace(group_name=GROUP_NAMES.get(group_id, "N/A")),
            message=[Plain(text=text)] if text else [],
        )

    def get_message_type(self):
        return self._mt

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._gid if self._mt == MessageType.GROUP_MESSAGE else ""

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


def make_plugin(whitelist=(GROUP_A, GROUP_B), context=None):
    raw = {"enable": True, "whitelist": {"allowed_origins": list(whitelist)}}
    return Main(context if context is not None else FakeContext(), raw)


def run_command(plugin, text):
    ev = FakeEvent(text=text, message_type=MessageType.FRIEND_MESSAGE, is_admin=True)

    async def runner():
        return [item async for item in plugin.reply_command(ev)]

    return asyncio.run(runner())


def feed_group(plugin, events):
    """喂群消息，返回各 origin 的状态快照并清掉防抖任务。"""

    async def runner():
        states = {}
        for origin in (ORIGIN_A, ORIGIN_B):
            states[origin] = plugin.runtime.touch(origin)
        for ev in events:
            await plugin.on_group_message(ev)
        for st in states.values():
            if st.debounce_task and not st.debounce_task.done():
                st.debounce_task.cancel()
                try:
                    await st.debounce_task
                except asyncio.CancelledError:
                    pass
        return states

    return asyncio.run(runner())


# ---------------- /reply off|on <群号> ----------------


def test_per_group_off_records_and_persists():
    plugin = make_plugin()
    writes = []

    async def fake_put(key, value):
        writes.append((key, dict(value)))

    plugin.put_kv_data = fake_put
    out = run_command(plugin, "/reply off " + GROUP_A)
    assert GROUP_A in plugin._paused_origins
    assert writes[-1][0] == KV_PAUSE_STATE_KEY
    assert writes[-1][1]["paused_origins"] == [GROUP_A]
    # 群名未缓存时退回纯群号
    assert "已关闭群 " + GROUP_A in str(out[0][1])
    assert "（" not in str(out[0][1])
    # 缓存了群名之后，提示里应带群名
    plugin._group_names[GROUP_B] = "测试群B"
    out2 = run_command(plugin, "/reply on " + GROUP_B)
    assert "测试群B" in str(out2[0][1])


def test_per_group_on_removes_and_persists():
    plugin = make_plugin()
    plugin.put_kv_data = _noop_put
    run_command(plugin, "/reply off " + GROUP_A)
    assert GROUP_A in plugin._paused_origins
    out = run_command(plugin, "/reply on " + GROUP_A)
    assert GROUP_A not in plugin._paused_origins
    assert "已开启群" in str(out[0][1])


def test_per_group_on_warns_when_global_still_paused():
    plugin = make_plugin()
    plugin.put_kv_data = _noop_put
    plugin._paused = True
    out = run_command(plugin, "/reply on " + GROUP_A)
    assert "全局仍处于关闭状态" in str(out[0][1])


def test_per_group_off_rejects_group_outside_whitelist():
    plugin = make_plugin()
    writes = []

    async def fake_put(key, value):
        writes.append((key, value))

    plugin.put_kv_data = fake_put
    out = run_command(plugin, "/reply off 999999")
    assert "不在白名单中" in str(out[0][1])
    assert plugin._paused_origins == set()
    assert writes == []


def test_per_group_off_accepts_full_umo_entry():
    """白名单里写完整 umo 时，用群号也应能命中。"""
    plugin = make_plugin(whitelist=(ORIGIN_A,))
    plugin.put_kv_data = _noop_put
    run_command(plugin, "/reply off " + GROUP_A)
    assert plugin._paused_origins == {ORIGIN_A}


# ---------------- 按群拦截行为 ----------------


def test_per_group_off_blocks_only_that_group():
    plugin = make_plugin()
    PROVIDER_CALLS.clear()
    plugin.put_kv_data = _noop_put
    run_command(plugin, "/reply off " + GROUP_A)
    states = feed_group(
        plugin,
        [
            FakeEvent("群里说话", message_id="1", group_id=GROUP_A),
            FakeEvent("另一个群", message_id="2", group_id=GROUP_B),
        ],
    )
    # A 群：被拦住，不入队、不调度
    assert states[ORIGIN_A].pending_messages == []
    assert states[ORIGIN_A].debounce_task is None
    # 但历史仍然记录（关闭期间上下文继续积累）
    assert len(states[ORIGIN_A].session_chats) == 1
    # B 群：不受影响，正常入队
    assert len(states[ORIGIN_B].pending_messages) == 1
    assert PROVIDER_CALLS == []


def test_per_group_paused_guard_blocks_handle_pending():
    plugin = make_plugin()
    PROVIDER_CALLS.clear()
    plugin.put_kv_data = _noop_put
    run_command(plugin, "/reply off " + GROUP_A)

    async def runner():
        st = plugin.runtime.touch(ORIGIN_A)
        st.pending_messages.append(
            {"norm_id": "1", "nick": "E0", "sender_id": "1", "text": "hi",
             "has_image": False, "role": "(member)", "ts": 0.0}
        )
        await plugin._handle_pending(ORIGIN_A)
        return st

    st = asyncio.run(runner())
    assert PROVIDER_CALLS == []
    assert len(st.pending_messages) == 1


def test_global_off_still_blocks_all_groups():
    plugin = make_plugin()
    plugin.put_kv_data = _noop_put
    run_command(plugin, "/reply off")
    assert plugin._paused is True
    assert plugin._paused_origins == set()
    states = feed_group(
        plugin,
        [
            FakeEvent("a", message_id="1", group_id=GROUP_A),
            FakeEvent("b", message_id="2", group_id=GROUP_B),
        ],
    )
    assert states[ORIGIN_A].pending_messages == []
    assert states[ORIGIN_B].pending_messages == []


# ---------------- 群名缓存与 stat ----------------


def test_group_name_cached_from_group_message():
    plugin = make_plugin()
    feed_group(plugin, [FakeEvent("hi", message_id="1", group_id=GROUP_A)])
    assert plugin._group_names.get(GROUP_A) == "测试群"


def test_group_name_not_cached_for_na():
    plugin = make_plugin()
    ev = FakeEvent("hi", message_id="1", group_id=GROUP_A)
    ev.message_obj.group.group_name = "N/A"
    feed_group(plugin, [ev])
    assert GROUP_A not in plugin._group_names


def test_stat_shows_group_name_and_per_group_mark():
    plugin = make_plugin()
    plugin.put_kv_data = _noop_put
    # 先跑一条群消息把群名带进来
    feed_group(plugin, [FakeEvent("hi", message_id="1", group_id=GROUP_A)])
    run_command(plugin, "/reply off " + GROUP_A)
    out = run_command(plugin, "/reply stat")
    text = str(out[0][1])
    assert "测试群（" + GROUP_A + "）" in text
    assert "按群关闭" in text
    assert "[按群已关]" in text
    # 未缓存的群退回群号显示
    assert GROUP_B in text


def test_display_name_falls_back_to_group_id():
    plugin = make_plugin()
    assert plugin._display_name(GROUP_A) == GROUP_A
    plugin._group_names[GROUP_A] = "测试群"
    assert plugin._display_name(GROUP_A) == "测试群（" + GROUP_A + "）"


def test_resolve_whitelist_entry_variants():
    plugin = make_plugin(whitelist=(ORIGIN_A, GROUP_B))
    # 完整 umo 条目：按群号命中，返回条目原样
    assert plugin._resolve_whitelist_entry(GROUP_A) == ORIGIN_A
    # 纯群号条目：原样返回
    assert plugin._resolve_whitelist_entry(GROUP_B) == GROUP_B
    # 带 @ 前缀也能解析
    assert plugin._resolve_whitelist_entry("@" + GROUP_B) == GROUP_B
    assert plugin._resolve_whitelist_entry("888888") is None


async def _noop_put(key, value):
    return None

