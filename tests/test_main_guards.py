"""main.py 入口守卫逻辑：唤醒消息排除 + 同内容指纹折叠。

用假事件直接驱动 on_group_message，不触网、不调用 LLM。
"""

import asyncio
import time

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.platform import MessageType

from astrbot_plugin_self_reply.main import (
    _DUP_TEXT_WINDOW_SEC,
    _in_other_side_conversation,
    _is_reaction_only,
    Main,
    is_directed_to_other,
)
from astrbot_plugin_self_reply.runtime_state import OriginState

ORIGIN = "bot:GroupMessage:400000000"
BOT_QQ = "6000000000"
SENDER_QQ = "8000000000"
OTHER_QQ = "9000000000"


class FakeEvent:
    def __init__(
        self,
        text,
        is_wake=False,
        message_id="1001",
        at_bot=False,
        at_other=False,
        reply_to_other=False,
        sender_qq=SENDER_QQ,
    ):
        self.message_str = text
        self.unified_msg_origin = ORIGIN
        self.is_at_or_wake_command = is_wake
        self._sender_qq = sender_qq
        components = []
        if at_bot:
            components.append(At(qq=BOT_QQ))
        if at_other:
            components.append(At(qq=OTHER_QQ, name="椧珩"))
        if reply_to_other:
            components.append(
                Reply(
                    id="222",
                    sender_id=OTHER_QQ,
                    sender_nickname="椧珩",
                    message_str="今天真的猪了",
                )
            )
        if text:
            components.append(Plain(text=text))
        self.message_obj = type(
            "MsgObj",
            (),
            {
                "sender": type("Sender", (), {"nickname": "E0"})(),
                "message_id": message_id,
                "self_id": BOT_QQ,
                "message": components,
            },
        )()

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def get_sender_id(self):
        return self._sender_qq

    def get_group_id(self):
        return "400000000"

    def is_admin(self):
        return False

    def get_messages(self):
        return self.message_obj.message


def _make_plugin():
    return Main(None, {"enable": True, "whitelist": {"allowed_origins": [ORIGIN]}})


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


def test_is_directed_to_other_detects_reply_to_other():
    ev = FakeEvent("今天真的猪了", message_id="9001", reply_to_other=True)
    assert is_directed_to_other(ev, BOT_QQ) is True


def test_is_directed_to_other_detects_at_other():
    ev = FakeEvent("你觉得呢", message_id="9002", at_other=True)
    assert is_directed_to_other(ev, BOT_QQ) is True


def test_is_directed_to_other_false_for_plain_or_at_bot():
    plain_ev = FakeEvent("普通群聊", message_id="9003")
    at_bot_ev = FakeEvent("在吗", message_id="9004", at_bot=True)
    assert is_directed_to_other(plain_ev, BOT_QQ) is False
    assert is_directed_to_other(at_bot_ev, BOT_QQ) is False


def test_reply_to_other_still_recorded_but_not_pending():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("今天真的猪了", message_id="9101", reply_to_other=True),
            FakeEvent("普通群聊", message_id="9102"),
        ],
    )
    assert len(st.session_chats) == 2  # 仍写入历史供上下文使用
    assert len(st.pending_messages) == 1
    assert st.pending_messages[0]["norm_id"] == "9102"


def test_at_other_still_recorded_but_not_pending():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("你觉得呢", message_id="9201", at_other=True),
            FakeEvent("普通群聊", message_id="9202"),
        ],
    )
    assert len(st.session_chats) == 2
    assert len(st.pending_messages) == 1
    assert st.pending_messages[0]["norm_id"] == "9202"


def test_short_reaction_in_two_person_side_conversation_not_pending():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("在吗", message_id="9301", sender_qq=SENDER_QQ),
            FakeEvent("在的", message_id="9302", sender_qq=OTHER_QQ),
            FakeEvent("🐖", message_id="9303", sender_qq=SENDER_QQ),
        ],
    )
    assert len(st.session_chats) == 3
    assert len(st.pending_messages) == 2
    assert all(p["norm_id"] != "9303" for p in st.pending_messages)


def test_full_message_in_two_person_side_conversation_still_pending():
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("在吗", message_id="9401", sender_qq=SENDER_QQ),
            FakeEvent("在的", message_id="9402", sender_qq=OTHER_QQ),
            FakeEvent("小盐今天天气怎么样", message_id="9403", sender_qq=SENDER_QQ),
        ],
    )
    assert len(st.session_chats) == 3
    assert len(st.pending_messages) == 3


def test_side_conversation_detection_requires_no_recent_bot():
    st = OriginState(
        session_chats=[
            "[E0/8000000000/07:56:00](admin) #msg1: 在吗",
            "[You/07:56:05]: 喵？",
            "[Ö/9000000000/07:56:10](member) #msg2: 在的",
        ]
    )
    assert _in_other_side_conversation(st, "8000000000") is False
    st2 = OriginState(
        session_chats=[
            "[E0/8000000000/07:56:00](admin) #msg1: 在吗",
            "[Ö/9000000000/07:56:10](member) #msg2: 在的",
        ]
    )
    # 只有两条还不足以判定为“旁听中的两人对话”
    assert _in_other_side_conversation(st2, "8000000000") is False


def test_reaction_only_detection():
    assert _is_reaction_only("", False) is True
    assert _is_reaction_only("[Empty]", True) is True
    assert _is_reaction_only("🐖", False) is True
    assert _is_reaction_only("😂", False) is True
    assert _is_reaction_only("233", False) is True
    assert _is_reaction_only("哈", False) is True
    # 两个中文字可能是称呼/短句，不当作纯反应
    assert _is_reaction_only("哈哈", False) is False
    assert _is_reaction_only("小盐", False) is False
    assert _is_reaction_only("今天好无聊", False) is False

