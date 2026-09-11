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

DEFAULT_GENERATE_PROMPT = (
    "你正在以「小盐」的软萌猫娘身份参与群聊，对群友的消息生成一句插话回应。\n\n"
    "群聊历史:\n"
    "{history_text}\n\n"
    "你决定针对以下消息进行插话回应:\n"
    "{targets_str}\n\n"
    "1. 先判断对方的真实情绪，再决定回应方式：\n"
    "   - 句尾括号（如（悲（哭（恼（乐）是对方的真实情绪注脚，以括号为准；\n"
    "   - 对方难过、心疼、诉苦、自嘲倒霉 → 软软安慰、摸摸、陪ta叹气；\n"
    "   - 对方明确在开心、整活、玩梗 → 才跟着调侃吐槽；\n"
    "   - 拿不准 → 温和中性回应，不要反讽、不要倒打一耙。\n"
    "2. 回应必须忠于对方实际表达的意思，不臆测、不捏造对方没有表现出的态度（例如对方在诉苦时，禁止说成对方在笑/在得意）。\n"
    "3. 一两句话，口语化，不用列表、不用格式化排版，符合群聊闲聊氛围。\n"
    "不要以为所有消息都是对你说的。只有在有明确证据时才把自己当话题。直接输出回复内容，不要额外解释。\n"
    "{quote_rule}"
    "{anti_repeat_instr}"
)


def parse_judge_output(raw: str, fallback_id: str) -> dict:
    """
    解析 judge 输出为 {decision, target_ids, reason}。
    fallback_id = pending 最近一条 msg_id（用于 REPLY 容错）
    """
    if not raw:
        return {"decision": "skip", "target_ids": [], "reason": "empty"}
    text = raw.strip()

    candidates = []
    m = JSON_RE.search(text)
    if m:
        candidates.append(m.group())

    if "{" in text:
        start_idx = text.find("{")
        sub = text[start_idx:].strip()
        if not sub.endswith("}"):
            candidates.append(sub + "}")
        candidates.append(sub)

    for cand in candidates:
        try:
            obj = json.loads(cand)
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
            continue

    # 正则提取 decision 与 target_ids 作为容错兜底
    lower_text = text.lower()
    if '"decision": "reply"' in lower_text or '"decision":"reply"' in lower_text:
        target_ids = re.findall(r'"(msg\d+|\d+)"', text)
        valid_ids = [tid for tid in target_ids if tid.startswith("msg") or tid.isdigit()]
        return {
            "decision": "reply",
            "target_ids": valid_ids if valid_ids else ([fallback_id] if fallback_id else []),
            "reason": "regex-extracted reply",
        }

    # 兼容 REPLY/SKIP
    tok = text.split()[0].upper() if text else ""
    if tok.startswith("REPLY"):
        return {
            "decision": "reply",
            "target_ids": [fallback_id] if fallback_id else [],
            "reason": "fallback REPLY parse",
        }
    return {"decision": "skip", "target_ids": [], "reason": "fallback SKIP parse"}
