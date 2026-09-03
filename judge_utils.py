from __future__ import annotations

import json
import re

ALLOWED_DECISION = {"reply", "skip"}
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# 占位符与 main.py 中 judge_prompt 的 .format 参数一一对应；
# JSON 示例中的大括号需写成 {{ }} 转义，否则 .format 会报错。
DEFAULT_JUDGE_PROMPT = (
    "你当前的人格是:{persona_name}\n"
    "人格设定:{persona_mask}\n"
    "\n"
    "以下群聊最近 {pending_count} 条待判定消息:\n"
    "{pending_msgs}\n"
    "\n"
    "(历史上下文最近 {history_count} 条,已回复的已标为 [replied]):\n"
    "{history_lines}\n"
    "\n"
    "请以你的人格判断是否应该主动回复这些消息,并给出至多 1 个目标 msg_id:\n"
    '{{"decision":"reply","target_ids":["msgid"],"reason":"..."}}\n'
    '{{"decision":"skip","reason":"..."}}\n'
    "只输出 JSON,不要额外文字。"
)


def parse_judge_output(raw: str, fallback_id: str) -> dict:
    """
    解析 judge 输出为 {decision, target_ids, reason}。
    fallback_id = pending 最近一条 msg_id（用于 REPLY 容错）
    """
    if not raw:
        return {"decision": "skip", "target_ids": [], "reason": "empty"}
    text = raw.strip()
    m = JSON_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group())
            dec = str(obj.get("decision", "")).lower().strip()
            ids = obj.get("target_ids") or []
            if not isinstance(ids, list):
                ids = []
            ids = [str(i) for i in ids]
            if dec in ALLOWED_DECISION or dec.startswith("reply"):
                return {
                    "decision": "reply" if dec.startswith("reply") else "skip",
                    "target_ids": ids,
                    "reason": str(obj.get("reason", "")),
                }
        except Exception:
            pass
    # 兼容 REPLY/SKIP
    tok = text.split()[0].upper() if text else ""
    if tok.startswith("REPLY"):
        return {
            "decision": "reply",
            "target_ids": [fallback_id],
            "reason": "fallback REPLY parse",
        }
    return {"decision": "skip", "target_ids": [], "reason": "fallback SKIP parse"}
