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
    "请以你的人格判断是否应该主动回复这些消息,并给出至多 1 个目标 msg_id。\n"
    "\n"
    "【重要：这是多人群聊，不是私聊】\n"
    "- 不是所有消息都发给你。别人 @ 其他群友、引用/回复其他群友的消息，"
    "通常是在和其他人说话，默认不要插话。\n"
    "- 只有消息 @你、回复/引用你、或明确提到你/你的名字时，才算“发给你”。\n"
    "- 如果待判定消息是其他群友之间的连续对话（尤其是一对一闲聊），请 SKIP。\n"
    "- 只有当你有非常自然、值得开口的理由（例如话题明显与你有关、你能自然接住梗"
    "且不会打扰别人）时，才 REPLY。\n"
    "- 普通群聊发言没有明确收件人时，可以按人格判断是否值得主动接话，但不要每条都回。\n"
    "\n"
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
