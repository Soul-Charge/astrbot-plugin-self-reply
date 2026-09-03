from __future__ import annotations

import difflib
import time

from .runtime_state import OriginState


class ReplyTracker:
    @staticmethod
    def on_cooldown(st: OriginState, min_interval: float) -> bool:
        return time.time() - st.last_reply_ts < min_interval

    @staticmethod
    def filter_unreplied(st: OriginState, ids: list[str]) -> list[str]:
        out = [i for i in ids if i not in st.replied_registry]
        return out

    @staticmethod
    def mark_replied(st: OriginState, ids: list[str]) -> None:
        for i in ids:
            st.replied_registry[i] = time.time()
        # 容量 ~200，超出走 LRU
        while len(st.replied_registry) > 200:
            st.replied_registry.popitem(last=False)

    @staticmethod
    def find_duplicate(st: OriginState, text: str, threshold: float) -> tuple[bool, float]:
        best = 0.0
        for prev in st.recent_bot_replies:
            r = difflib.SequenceMatcher(None, prev, text).ratio()
            if r > best:
                best = r
            if r > threshold:
                return True, r
        return False, best

    @staticmethod
    def register_bot_reply(st: OriginState, text: str) -> None:
        st.recent_bot_replies.append(text)
