"""空占位（戳一戳等无文本通知）不得污染待判定队列与判定输入。

线上事故复现：admin 说「小盐讲个鬼故事」（msg18598404），9 秒后群友戳了
bot 一下。戳一戳在 aiocqhttp 适配器里 message_str 为空、只有一个 Poke 组件，
旧实现把它记成 "[Empty]" 入队并重置防抖，最终 judge 只针对那条空消息判 SKIP，
连带把同一批里的鬼故事请求一起丢掉。
"""

import asyncio

from astrbot.api.message_components import Image, Plain, Poke
from astrbot.api.platform import MessageType

from astrbot_plugin_self_reply.main import (
    Main,
    has_meaningful_content,
    is_noise_entry,
)
from astrbot_plugin_self_reply.runtime_state import OriginState

ORIGIN = "bot:GroupMessage:400000000"
BOT_QQ = "6000000000"
SENDER_QQ = "8000000000"


class FakeEvent:
    """够用即可的假事件；无文本事件用 text="" 表达（同适配器行为）。"""

    def __init__(self, text, message_id="1001", components=None, sender_qq=SENDER_QQ):
        self.message_str = text
        self.unified_msg_origin = ORIGIN
        self.is_at_or_wake_command = False
        self._sender_qq = sender_qq
        if components is None:
            components = [Plain(text=text)] if text else []
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


def _poke_event(message_id="18599000", sender_qq="800000000"):
    """戳一戳：message_str 为空 + 单个 Poke 组件。"""
    return FakeEvent(
        "",
        message_id=message_id,
        components=[Poke(id=BOT_QQ)],
        sender_qq=sender_qq,
    )


def _make_plugin():
    return Main(None, {"enable": True, "whitelist": {"allowed_origins": [ORIGIN]}})


def _drive(plugin, events):
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


# ---------------- has_meaningful_content ----------------


def test_plain_text_is_meaningful():
    assert has_meaningful_content("小盐讲个鬼故事", [], [Plain(text="小盐讲个鬼故事")])


def test_poke_only_is_not_meaningful():
    assert not has_meaningful_content("", [], [Poke(id=BOT_QQ)])


def test_empty_message_list_is_not_meaningful():
    assert not has_meaningful_content("", [], [])


def test_whitespace_text_is_not_meaningful():
    assert not has_meaningful_content("   ", [], [])


def test_image_only_is_meaningful():
    assert has_meaningful_content("", ["http://x/1.png"], [Image(file="1.png")])


# ---------------- is_noise_entry ----------------


def test_noise_entry_for_empty_text():
    assert is_noise_entry({"text": "[Empty]", "has_image": False})


def test_noise_entry_for_blank_text():
    assert is_noise_entry({"text": "", "has_image": False})


def test_real_text_is_not_noise():
    assert not is_noise_entry({"text": "小盐讲个鬼故事", "has_image": False})


def test_image_only_entry_is_not_noise():
    """纯图片消息 text 也是 "[Empty]"，但有图，不能当成噪声丢掉。"""
    assert not is_noise_entry({"text": "[Empty]", "has_image": True})


# ---------------- on_group_message：入队行为 ----------------


def test_poke_does_not_enter_pending_and_keeps_history():
    plugin = _make_plugin()
    st = _drive(plugin, [_poke_event()])
    assert st.pending_messages == []
    assert len(st.session_chats) == 1
    assert st.session_chats[0].endswith(" [Poke]")


def test_poke_after_request_keeps_request_pending():
    """核心回归：戳一戳不得把已入队的真实请求顶掉或改写。"""
    plugin = _make_plugin()
    st = _drive(
        plugin,
        [
            FakeEvent("小盐讲个鬼故事", message_id="18598404"),
            _poke_event(),
        ],
    )
    assert [p["text"] for p in st.pending_messages] == ["小盐讲个鬼故事"]
    assert st.pending_messages[0]["norm_id"] == "18598404"
    assert len(st.session_chats) == 2  # 一条正文 + 一条 [Poke] 痕迹


def test_poke_does_not_reschedule_debounce():
    """戳一戳不该重置防抖窗口，否则会推迟真实消息的判定。"""
    plugin = _make_plugin()
    st = _drive(plugin, [FakeEvent("小盐讲个鬼故事", message_id="18598405"), _poke_event()])
    assert st.debounce_task is not None


# ---------------- _format_pending：判定输入 ----------------


class _FakeState:
    def __init__(self, replied=()):
        self.replied_registry = {rid: 0.0 for rid in replied}


def _entry(text, norm_id="1", has_image=False, nick="E0", sender_id=SENDER_QQ):
    return {
        "norm_id": norm_id,
        "nick": nick,
        "sender_id": sender_id,
        "text": text,
        "has_image": has_image,
        "role": "(admin)",
    }


def test_format_pending_drops_empty_placeholder():
    out = Main._format_pending(
        None,
        [_entry("小盐讲个鬼故事", "18598404"), _entry("[Empty]", "18599000")],
        _FakeState(),
        None,
    )
    assert out.count("\n") == 0
    assert "小盐讲个鬼故事" in out
    assert "[Empty]" not in out
    assert "#msg18599000" not in out
    assert "#msg18598404" in out


def test_format_pending_numbering_matches_count_without_gaps():
    out = Main._format_pending(
        None,
        [
            _entry("[Empty]", "1"),
            _entry("第一句", "2"),
            _entry("[Empty]", "3"),
            _entry("第二句", "4"),
        ],
        _FakeState(),
        None,
    )
    lines = out.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("[1] ")
    assert lines[1].startswith("[2] ")


def test_format_pending_keeps_image_only_entry():
    out = Main._format_pending(
        None, [_entry("[Empty]", "9", has_image=True)], _FakeState(), None
    )
    assert "#msg9" in out


def test_format_pending_marks_replied():
    out = Main._format_pending(
        None, [_entry("回过了", "7")], _FakeState(replied=["7"]), None
    )
    assert "[replied]" in out


# ---------------- _handle_pending：不叫 judge ----------------


class _FakeProvider:
    def __init__(self):
        self.calls = 0

    async def text_chat(self, **_kwargs):
        self.calls += 1
        raise AssertionError("纯空占位批次不应触发 judge")


def test_handle_pending_skips_judge_when_batch_is_all_noise():
    """旧行为：戳一戳单独成批也会叫一次 judge 并必然 SKIP，顺带清空队列。"""
    plugin = _make_plugin()
    st = _drive(plugin, [_poke_event()])
    assert st.pending_messages == []  # 噪声根本没入队，队列为空

    provider = _FakeProvider()
    plugin._resolve_provider = lambda _pid=None: provider
    st.pending_messages.append(_entry("[Empty]", "18599000"))
    asyncio.run(plugin._handle_pending(ORIGIN))

    assert provider.calls == 0
    assert st.pending_messages == []
