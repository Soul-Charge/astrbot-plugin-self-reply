from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field


@dataclass
class OriginState:
    session_chats: list[str] = field(default_factory=list)
    image_registry: dict[str, dict] = field(
        default_factory=dict
    )  # norm_id → {urls, captions}
    pending_messages: list[dict] = field(
        default_factory=list
    )  # {norm_id, nick, sender_id, text, has_image, ts, role}
    replied_registry: OrderedDict[str, float] = field(default_factory=OrderedDict)
    recent_bot_replies: deque = field(default_factory=lambda: deque(maxlen=20))
    msg_fingerprints: dict[str, dict] = field(
        default_factory=dict
    )  # "sender_id\0text" → {ts, wake}，用于折叠同内容重复事件
    debounce_task: asyncio.Task | None = None
    last_reply_ts: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RuntimeState:
    def __init__(self):
        self.origins: dict[str, OriginState] = {}
        self.lru: OrderedDict[str, None] = OrderedDict()
        self.max_origins = 500

    def _evict(self, origin: str) -> None:
        state = self.origins.pop(origin, None)
        if state and state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()

    def touch(self, origin: str) -> OriginState:
        self.lru.pop(origin, None)
        self.lru[origin] = None
        while len(self.lru) > self.max_origins:
            oldest, _ = self.lru.popitem(last=False)
            self._evict(oldest)
        if origin not in self.origins:
            self.origins[origin] = OriginState()
        return self.origins[origin]

    def get(self, origin: str) -> OriginState | None:
        return self.origins.get(origin)

    def cleanup(self, origin: str) -> None:
        self._evict(origin)
        self.lru.pop(origin, None)
