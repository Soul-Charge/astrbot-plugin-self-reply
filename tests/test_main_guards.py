"""main.py 入口守卫逻辑：唤醒消息排除 + 同内容指纹折叠。

用假事件直接驱动 on_group_message，不触网、不调用 LLM。
"""

import asyncio
import time

from astrbot.api.message_components import At, Plain
from astrbot.api.platform import MessageType

from astrbot_plugin_self_reply.main import _DUP_TEXT_WINDOW_SEC, Main

ORIGIN = "bot:GroupMessage:400000000"
BOT_QQ = "6000000000"
SENDER_QQ = "8000000000"


class FakeEvent:
    def __init__(self, text, is_wake=False, message_id="1001", at_bot=False):
        self.message_str = text
        self.unified_msg_origin = ORIGIN
        self.is_at_or_wake_command = is_wake
        self.message_obj = type(
            "MsgObj",
            (),
            {
                "sender": type("Sender", (), {"nickname": "E0"})(),
                "message_id": message_id,
                "self_id": BOT_QQ,
                "message": [At(qq=BOT_QQ)] if at_bot else [Plain(text=text)] if text else [],
            },
        )()

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def get_sender_id(self):
        return SENDER_QQ

    def get_group_id(self):
        return "400000000"

    def is_admin(self):
        return False

    def get_messages(self):
        return self.message_obj.message


def _make_plugin():
    return Main(None, {"enable": True})


def _drive(plugin, events):
    """在事件循环里依次喂事件，返回会话状态；退出前清掉防抖任务。"""

    async def runner():
        for ev in events:
            await plugin.on_group_message(ev)
        st = plugin.runtime.get(ORIGIN)
        task = st.debounce_task if st else None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return st

    return asyncio.run(runner())


def test_wake_message_not_pending_and_not_scheduled():
    plugin = _make_plugin()
    st = _drive(plugin, [FakeEvent("在吗", is_wake=True, at_bot=True)])
    assert len(st.session_chats) == 1  # 历史仍然记录
    assert st.pending_messages == []
    assert st.debounce_task is None


def test_plain_message_pends_and_schedules():
    plugin = _make_plugin()
    st = _drive(plugin, [FakeEvent("今天好无聊啊", message_id="2001")])
    assert len(st.pending_messages) == 1
    assert st.pending_messages[0]["text"] == "今天好无聊啊"
    assert st.debounce_task is not None


def test_twin_delivery_wake_second_withdraws_pending():
    # 复刻实际案例：先纯文本事件，再带 At 的同内容事件（8ms 双投递）
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("小盐你一天吃多少小鱼干", message_id="3001"),
            FakeEvent("小盐你一天吃多少小鱼干", message_id="3002", is_wake=True, at_bot=True),
        ],
    )
    assert len(st.session_chats) == 1  # 历史只记一次
    assert st.pending_messages == []  # 纯文本副本已撤回，不与主流水线抢答


def test_twin_delivery_wake_first_suppresses_plain_copy():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("小盐帮我看看", message_id="4001", is_wake=True, at_bot=True),
            FakeEvent("小盐帮我看看", message_id="4002"),
        ],
    )
    assert len(st.session_chats) == 1
    assert st.pending_messages == []


def test_identical_plain_spam_collapse():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("哈哈哈", message_id="5001"),
            FakeEvent("哈哈哈", message_id="5002"),
        ],
    )
    assert len(st.session_chats) == 1
    assert len(st.pending_messages) == 1


def test_different_texts_not_collapsed():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("第一条消息", message_id="6001"),
            FakeEvent("第二条消息", message_id="6002"),
        ],
    )
    assert len(st.session_chats) == 2
    assert len(st.pending_messages) == 2


def test_same_text_after_window_not_collapsed():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("重复一次", message_id="7001"),
            FakeEvent("重复一次", message_id="7002"),
        ],
    )
    # 手动把指纹时间戳推到窗口之外，再喂第三条同内容消息
    for fp in st.msg_fingerprints.values():
        fp["ts"] -= _DUP_TEXT_WINDOW_SEC + 1

    async def extra():
        await plugin.on_group_message(FakeEvent("重复一次", message_id="7003"))

    asyncio.run(extra())
    assert len(st.session_chats) == 2
    assert len(st.pending_messages) == 2


def test_self_message_ignored():
    plugin = _make_plugin()

    class SelfEvent(FakeEvent):
        def get_sender_id(self):
            return BOT_QQ

    st = _drive(plugin, [SelfEvent("我自己说的话", message_id="8001")])
    assert st is None or len(st.session_chats) == 0
