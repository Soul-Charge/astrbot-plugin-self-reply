"""reply_tracker：冷却 / 已回复过滤 / 相似度 / 容量。"""

import time

from astrbot_plugin_self_reply.reply_tracker import (
    ReplyTracker,
    should_defer_by_cooldown,
)
from astrbot_plugin_self_reply.runtime_state import OriginState


def test_cooldown_false_when_never_replied():
    st = OriginState()
    assert ReplyTracker.on_cooldown(st, 15.0) is False


def test_cooldown_true_within_interval():
    st = OriginState()
    st.last_reply_ts = time.time()
    assert ReplyTracker.on_cooldown(st, 15.0) is True


def test_cooldown_false_after_interval():
    st = OriginState()
    st.last_reply_ts = time.time() - 20
    assert ReplyTracker.on_cooldown(st, 15.0) is False


def test_should_defer_by_cooldown_never_defers_task():
    """P1b：派活消息落在冷却窗口内也要接住，只有闲聊批才延后。"""
    assert should_defer_by_cooldown("task", on_cooldown=True) is False
    assert should_defer_by_cooldown("task", on_cooldown=False) is False
    assert should_defer_by_cooldown("chat", on_cooldown=True) is True
    assert should_defer_by_cooldown("chat", on_cooldown=False) is False
    # 判定失败兜底成 chat（保守）：跟着延后，而不是硬发
    assert should_defer_by_cooldown("", on_cooldown=True) is True


def test_filter_unreplied_excludes_marked():
    st = OriginState()
    ReplyTracker.mark_replied(st, ["1", "2"])
    assert ReplyTracker.filter_unreplied(st, ["1", "3", "2"]) == ["3"]
    assert ReplyTracker.filter_unreplied(st, []) == []


def test_mark_replied_respects_capacity():
    st = OriginState()
    ids = [str(i) for i in range(205)]
    ReplyTracker.mark_replied(st, ids)
    assert len(st.replied_registry) == 200
    assert "0" not in st.replied_registry
    assert "4" not in st.replied_registry
    assert "204" in st.replied_registry


def test_find_duplicate_above_threshold():
    st = OriginState()
    ReplyTracker.register_bot_reply(st, "今天天气真好，我们一起出去玩吧")
    dup, ratio = ReplyTracker.find_duplicate(
        st, "今天天气真好，我们一起出去玩吧", 0.85
    )
    assert dup is True
    assert ratio > 0.85


def test_find_duplicate_below_threshold():
    st = OriginState()
    ReplyTracker.register_bot_reply(st, "今天天气真好，我们一起出去玩吧")
    dup, best = ReplyTracker.find_duplicate(st, "完全不同的一段话qwerty", 0.85)
    assert dup is False
    assert 0.0 <= best <= 0.85


def test_find_duplicate_empty_history():
    st = OriginState()
    assert ReplyTracker.find_duplicate(st, "anything", 0.85) == (False, 0.0)


def test_register_bot_reply_respects_deque_cap():
    st = OriginState()
    for i in range(25):
        ReplyTracker.register_bot_reply(st, f"reply-{i}")
    assert len(st.recent_bot_replies) == 20
    assert list(st.recent_bot_replies)[0] == "reply-5"
    assert list(st.recent_bot_replies)[-1] == "reply-24"
