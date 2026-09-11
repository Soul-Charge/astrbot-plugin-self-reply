from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import deque
from dataclasses import replace as dataclass_replace
from datetime import datetime
from uuid import uuid4

from astrbot.api import logger, sp, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.star import Context
from astrbot.core.star.star_tools import StarTools

from .image_resolver import resolve
from .judge_utils import DEFAULT_GENERATE_PROMPT, parse_judge_output
from .kb_bridge import retrieve_kb_block
from .memory_bridge import EventShim, MemoryBridge, truncate_block
from .pipeline_dispatch import (
    PIPELINE_HOOK_PRIORITY,
    PipelineDispatcher,
    PipelineJob,
    new_message_id,
)
from .plugin_config import PluginConfig, parse_plugin_config
from .reply_tracker import ReplyTracker
from .runtime_state import OriginState, RuntimeState
from .tag_utils import (
    MENTION_CLOSE_RE,
    MENTION_RE,
    QUOTE_CLOSE_RE,
    QUOTE_RE,
    chain_has_refuse_tag,
    normalize_id,
    transform_result_chain,
)

MSG_ID_LINE_RE = re.compile(r"#msg(\w+)")

# 会话重置清理必须最先执行：AstrBot 的 hook 链一旦遇到 event.is_stopped()
# 就会中断后续 handler（见 core/pipeline/context_utils.py），而 /reset 这类
# 命令事件常常在进入 RespondStage 前就已被 stop，导致排在后面的钩子收不到通知。
RESET_HOOK_PRIORITY = sys.maxsize

# 召回内容注入占位符
RECALL_PLACEHOLDER = "{recalled_memories}"

# 召回块的使用提示（放在块首，避免被 max_chars 截断时丢失）
RECALL_USAGE_HINT = (
    "【参考信息使用说明】以下是从长期记忆与知识库检索到的相关内容，"
    "同一个人的不同写法（繁体/简体、全名/简称/别称/昵称）请视为同一个实体；"
    "记忆里已经写明的关系与事实请直接采用，不要回答“不认识”“不知道是谁”；"
    "若确实与本轮无关，忽略即可，也不要据此编造。"
)

# 识别“值得引用回复”的特别问句，避免所有自主回复都变成引用模式
_QUESTION_RE = re.compile(
    r"[?？]|吗|呢|嘛|什么|怎么|怎样|如何|为什么|为啥|哪|谁|多少|几|"
    r"是不是|能不能|可不可以|要不要|有没有|是否",
    re.IGNORECASE,
)

# 同一 (发送者, 文本) 在该窗口内的事件视为同一条消息（平台可能双投递：纯文本+At 各一条）
_DUP_TEXT_WINDOW_SEC = 10.0


def is_question_text(text: str) -> bool:
    t = (text or "").strip()
    if not t or t == "[Empty]":
        return False
    return bool(_QUESTION_RE.search(t))


def extract_msg_id_from_line(line: str) -> str | None:
    m = MSG_ID_LINE_RE.search(line)
    return m.group(1) if m else None


def safe_event_str(event, method_name: str) -> str:
    """安全调用事件上的无参取值方法（兼容精简/自定义事件对象）。"""
    getter = getattr(event, method_name, None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def clean_text(text: str) -> str:
    # 移除 quote/mention tag 再写历史
    text = QUOTE_RE.sub("", text)
    text = QUOTE_CLOSE_RE.sub("", text)
    text = MENTION_RE.sub(r"[At: \1]", text)
    text = MENTION_CLOSE_RE.sub("", text)
    return text.strip()


def build_generate_prompt(
    prompt_tmpl: str,
    *,
    history_text: str,
    targets_str: str,
    quote_rule: str,
    anti_repeat_instr: str,
    recall_text: str,
) -> str:
    """渲染插话生成提示词。

    抽成纯函数是为了让两种派发模式共享同一套拼装逻辑：
    ``pipeline`` 模式传空召回（记忆/知识库由主管道的钩子注入），
    注入失败回退直连时再用召回块渲染一次。
    """
    if "{history_text}" in prompt_tmpl and "{targets_str}" in prompt_tmpl:
        try:
            prompt = prompt_tmpl.format(
                history_text=history_text,
                targets_str=targets_str,
                quote_rule=quote_rule,
                anti_repeat_instr=anti_repeat_instr,
                recalled_memories=recall_text,
            )
        except Exception as e:
            logger.warning(
                f"self-reply | format prompt_template 失败: {e}，回退标准拼接"
            )
            prompt = _fallback_generate_prompt(
                prompt_tmpl, history_text, targets_str, quote_rule, anti_repeat_instr
            )
    else:
        prompt = _fallback_generate_prompt(
            prompt_tmpl, history_text, targets_str, quote_rule, anti_repeat_instr
        )
    return Main._append_recall_if_missing(prompt, prompt_tmpl, recall_text)


def _fallback_generate_prompt(
    prompt_tmpl: str,
    history_text: str,
    targets_str: str,
    quote_rule: str,
    anti_repeat_instr: str,
) -> str:
    return (
        f"{prompt_tmpl}\n\n"
        f"Chat history:\n{history_text}\n\n"
        f"The following are the recent message(s) you are responding to or commenting on:\n"
        f"{targets_str}\n\n"
        "Do NOT assume those messages were addressed to you unless they @, quote, "
        "or clearly mention you. If they are between other members, you may still "
        "naturally join the group conversation when appropriate, but do not claim "
        "they were talking to you or treat yourself as the topic unless the evidence "
        "shows that.\n\n"
        f"{quote_rule}"
        "Output only your reply, nothing else. Use the same language as the chatroom.\n"
        f"{anti_repeat_instr}"
    )


def is_directed_to_other(event: AstrMessageEvent, self_id: str) -> bool:
    """判断消息是否明确写给/引用了除 bot 以外的某个群友。

    这类消息通常是在和其他人对话，不应作为“发给 bot”的自主回复候选。
    注意：bot 自己发起的消息已在调用前被过滤。
    """
    self_id_s = str(self_id or "")
    if not self_id_s:
        return False
    for comp in event.get_messages():
        if isinstance(comp, Reply):
            sid = str(getattr(comp, "sender_id", "") or "")
            # sender_id 可能因平台未取到引用消息而为 0/空，此时无法判断，不拦截
            if sid and sid not in ("0", self_id_s):
                return True
        elif isinstance(comp, At):
            qq = str(getattr(comp, "qq", "") or "")
            if qq and qq.lower() != "all" and qq != self_id_s:
                return True
    return False


# 历史行形如 [昵称/QQ/时间](角色) #msg123: ...；bot 自己的行是 [You/时间]: ...
_HIST_SENDER_RE = re.compile(
    r"^\[([^/\]]+)/([^/\]]+)/([^\]]+)\](?:\([^)]*\))?\s*#msg"
)
_BOT_HIST_PREFIX = "[You/"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _recent_sender_ids(state, max_count: int = 5) -> list[str]:
    """从会话历史中提取最近若干条消息的发送者 ID。

    解析失败或未开启 sender_id 展示时返回空列表，调用方应视为“无法判断”。
    bot 自己的消息用 "__bot__" 表示。
    """
    ids: list[str] = []
    for line in reversed(state.session_chats):
        m = _HIST_SENDER_RE.match(line)
        if m:
            ids.append(m.group(2))
        elif line.startswith(_BOT_HIST_PREFIX):
            ids.append("__bot__")
        else:
            return []
        if len(ids) >= max_count:
            break
    return list(reversed(ids))


def _is_reaction_only(text: str, has_image: bool) -> bool:
    """判断是否属于“短反应”类消息（表情、单字、纯图等）。

    中文“小盐”这类两个字的人名/称呼不算短反应，避免误伤点名。
    """
    t = (text or "").strip()
    if not t or t == "[Empty]":
        return True
    if _CJK_RE.search(t):
        # 中文单字/语气词算反应；两个以上中文字更可能是完整称呼或短句
        return len(t) <= 1
    # 非中文内容：emoji、颜文字、短英文数字等
    return len(t) <= 4


def _in_other_side_conversation(state, sender_id: str) -> bool:
    """若最近几条是另外两个群友之间的一对一往来，且 bot 未参与，返回 True。

    只用于抑制“短反应类”消息，避免 bot 把别人聊天中的表情/单字当成发给自己的。
    """
    ids = _recent_sender_ids(state, max_count=5)
    if len(ids) < 3:
        return False
    # bot 刚说过话时不视为纯旁听
    if "__bot__" in ids[-3:]:
        return False
    human_ids = [i for i in ids if i != "__bot__"]
    if len(human_ids) < 3:
        return False
    if len(set(human_ids)) != 2:
        return False
    return str(sender_id) in set(human_ids)


class Main(star.Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._config = parse_plugin_config(config or {})
        self.runtime = RuntimeState()
        self.runtime.max_origins = self._config.global_settings.max_origins
        self.tracker = ReplyTracker()
        self.memory = MemoryBridge(context)
        self.dispatcher = PipelineDispatcher(context)

    def _cfg(self) -> PluginConfig:
        return self._config

    # ---------------- Hook 1: 群消息记录与防抖调度 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9990)
    async def on_group_message(self, event: AstrMessageEvent):
        cfg = self._cfg()
        if not cfg.enable:
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        sender_id = event.get_sender_id()
        self_id = getattr(event.message_obj, "self_id", "")
        if sender_id and self_id and str(sender_id) == str(self_id):
            return  # bot 自身

        # whitelist
        if cfg.whitelist.allowed_origins:
            if event.unified_msg_origin not in cfg.whitelist.allowed_origins and (
                event.get_group_id()
                and event.get_group_id() not in cfg.whitelist.allowed_origins
            ):
                return

        # 记录
        text = (event.message_str or "").strip() or "[Empty]"
        nick = event.message_obj.sender.nickname
        role = "(admin)" if event.is_admin() else "(member)"
        now = datetime.now().strftime("%H:%M:%S")
        msg_id_raw = event.message_obj.message_id
        norm_id = normalize_id(str(msg_id_raw))

        image_urls = []
        for comp in event.get_messages():
            if isinstance(comp, Image):
                image_urls.append(str(comp.url or comp.file or ""))

        if cfg.generate.include_sender_id and cfg.generate.include_role_tag:
            header = f"[{nick}/{sender_id}/{now}]{role} #msg{norm_id}:"
        elif cfg.generate.include_sender_id:
            header = f"[{nick}/{sender_id}/{now}] #msg{norm_id}:"
        elif cfg.generate.include_role_tag:
            header = f"[{nick}/{now}]{role} #msg{norm_id}:"
        else:
            header = f"[{nick}/{now}] #msg{norm_id}:"

        parts = [header]
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                qid = normalize_id(str(getattr(comp, "id", "")))
                qnick = comp.sender_nickname or "Unknown"
                qtext = (comp.message_str or "").strip() or "..."
                if qid:
                    parts.append(f" [Quote #msg{qid} {qnick}: {qtext}]")
                else:
                    parts.append(f" [Quote {qnick}: {qtext}]")
            elif isinstance(comp, Plain):
                parts.append(f" {comp.text}")
            elif isinstance(comp, Image):
                parts.append(" [Image]")
            elif isinstance(comp, At):
                parts.append(f" [At: {comp.name}]")
        line = "".join(parts)

        state = self.runtime.touch(event.unified_msg_origin)

        # 唤醒消息（@bot/引用bot/唤醒前缀）由主流水线响应，自主回复不再判定；
        # 同一消息可能被平台双投递（纯文本 + At 各一条），按 (发送者, 文本)
        # 指纹在窗口内折叠：历史只记一次；若任一副本为唤醒消息，
        # 撤回已入队的待判定条目，避免同一句话被两条链路各答一次
        is_wake = bool(event.is_at_or_wake_command)
        fp_key = f"{sender_id}\x00{text}"
        now_ts = time.time()
        prev_fp = state.msg_fingerprints.get(fp_key)
        if prev_fp is not None and now_ts - prev_fp["ts"] <= _DUP_TEXT_WINDOW_SEC:
            prev_fp["ts"] = now_ts
            prev_fp["wake"] = prev_fp["wake"] or is_wake
            if prev_fp["wake"]:
                state.pending_messages[:] = [
                    p
                    for p in state.pending_messages
                    if not (
                        str(p.get("sender_id")) == str(sender_id)
                        and p.get("text") == text
                    )
                ]
            return

        state.msg_fingerprints[fp_key] = {"ts": now_ts, "wake": is_wake}
        if len(state.msg_fingerprints) > 64:
            cutoff = now_ts - _DUP_TEXT_WINDOW_SEC
            state.msg_fingerprints = {
                k: v
                for k, v in state.msg_fingerprints.items()
                if v["ts"] > cutoff
            }

        state.session_chats.append(line)
        if len(state.session_chats) > cfg.history.max_messages:
            removed = state.session_chats.pop(0)
            rid = extract_msg_id_from_line(removed)
            if rid:
                state.image_registry.pop(rid, None)

        if norm_id and image_urls:
            state.image_registry[norm_id] = {"urls": image_urls, "captions": {}}

        # 唤醒消息交由主流水线响应
        if is_wake:
            return

        # 明确引用/At 其他群友的消息，通常是在和其他人对话；
        # 仍保留在上面的历史上下文中，但不进入自主回复的待判定队列。
        if is_directed_to_other(event, self_id):
            return

        # 别人正在一对一闲聊时，纯表情/单字/图片这类“反应”通常不是发给 bot 的。
        # 这里只拦短反应，不拦完整发言；历史仍保留，方便后续上下文理解。
        if _is_reaction_only(text, bool(image_urls)) and _in_other_side_conversation(
            state, str(sender_id)
        ):
            return

        # pending
        state.pending_messages.append(
            {
                "norm_id": norm_id,
                "nick": nick,
                "sender_id": sender_id,
                "text": text,
                "has_image": bool(image_urls),
                "role": role,
                "ts": time.time(),
                # 供记忆召回构造轻量事件 shim（防抖触发时真实事件已结束）
                "group_id": safe_event_str(event, "get_group_id"),
                "platform": safe_event_str(event, "get_platform_name"),
                "platform_id": safe_event_str(event, "get_platform_id"),
                "self_id": str(self_id or ""),
            }
        )
        if len(state.pending_messages) > cfg.trigger.max_pending_before_judge:
            state.pending_messages.pop(0)

        # debounce 调度
        quick = self._is_quick(event, cfg)
        self._schedule_debounce(event.unified_msg_origin, quick, cfg)

    # ---------------- Hook 2: 防抖与判定/生成/发送 ----------------

    def _is_quick(self, event: AstrMessageEvent, cfg: PluginConfig) -> bool:
        if event.is_at_or_wake_command:
            return True
        text = (event.message_str or "").strip()
        if any(k in text for k in cfg.trigger.quick_trigger_keywords):
            return True
        if "?" in text or "？" in text:
            return True
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                return True
        return False

    def _schedule_debounce(self, origin: str, quick: bool, cfg: PluginConfig) -> None:
        state = self.runtime.get(origin)
        if not state:
            return
        delay = (
            cfg.trigger.debounce_quick_seconds
            if quick
            else cfg.trigger.debounce_normal_seconds
        )
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
        state.debounce_task = asyncio.create_task(self._debounce_fire(origin, delay))

    async def _debounce_fire(self, origin: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._handle_pending(origin)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"self-reply | debounce fire error: {e}")

    async def _handle_pending(self, origin: str) -> None:
        cfg = self._cfg()
        state = self.runtime.get(origin)
        if not state:
            return
        if not state.pending_messages:
            return

        pipeline_mode = cfg.dispatch.mode == "pipeline"

        # 自愈兜底：核心会话被 /reset 或 /new 重置时，钩子链可能因
        # event.is_stopped() 提前中断，这里再按会话指纹检测一次。
        if await self._detect_core_session_reset(origin, state):
            return

        async with state.lock:
            pending = list(state.pending_messages)
            state.pending_messages.clear()

            if self.tracker.on_cooldown(state, cfg.trigger.min_reply_interval_seconds):
                logger.info(
                    f"self-reply | cooldown hit origin={origin} pending={len(pending)}"
                )
                return

            # 记忆 / 知识库召回（判定与生成共用同一次结果）
            persona_id, persona_name, persona_prompt = await self._resolve_persona(
                origin
            )
            shim = self._build_event_shim(origin, pending)
            recall_query = self._build_recall_query(pending)
            recall_block = await self._build_recall_block(
                origin, shim, recall_query, persona_id, cfg
            )
            judge_recall = recall_block if cfg.memory.inject_into_judge else ""
            # 管道模式下记忆/知识库由主流程钩子与核心 KB 注入，生成侧不再重复注入
            gen_recall = "" if pipeline_mode else recall_block

            # Build judge input
            history_slice = state.session_chats[-cfg.judge.history_messages :]
            judge_prompt = cfg.judge.prompt_template.format(
                persona_name=persona_name,
                persona_mask=persona_prompt,
                pending_count=len(pending),
                pending_msgs=self._format_pending(pending, state, cfg),
                history_count=len(history_slice),
                history_lines="\n".join(history_slice),
                recalled_memories=judge_recall,
            )
            judge_prompt = self._append_recall_if_missing(
                judge_prompt, cfg.judge.prompt_template, judge_recall
            )

            provider = self._resolve_provider(cfg.judge.provider_id or None)
            if not provider:
                logger.error("self-reply | judge provider not found")
                return

            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=judge_prompt, persist=False, session_id=uuid4().hex
                    ),
                    timeout=cfg.global_settings.judge_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.error("self-reply | judge timeout")
                return
            except Exception as e:
                logger.error(f"self-reply | judge fail: {e}")
                return

            fallback_id = pending[-1]["norm_id"] if pending else ""
            result = parse_judge_output(
                (resp.completion_text or "").strip(),
                fallback_id=fallback_id,
            )
            logger.info(f"self-reply | judge origin={origin} decision={result}")

            if result["decision"] != "reply":
                return
            target_ids = [normalize_id(t) for t in result.get("target_ids") or []]
            ok_targets = self.tracker.filter_unreplied(state, target_ids)
            if not ok_targets:
                logger.info("self-reply | all targets already replied; skip")
                return

            # Gather images
            image_urls: list[str] = []
            if cfg.generate.attach_recent_images > 0:
                # 从逆序收集至多 N 条有图的记录
                ctr = 0
                for line in reversed(state.session_chats):
                    if ctr >= cfg.generate.attach_recent_images:
                        break
                    rid = extract_msg_id_from_line(line)
                    if rid and rid in state.image_registry:
                        for u in state.image_registry[rid]["urls"]:
                            resolved = resolve(u)
                            if resolved:
                                image_urls.append(resolved)
                                ctr += 1
                                if ctr >= cfg.generate.attach_recent_images:
                                    break

            # Generate
            recent_own = list(state.recent_bot_replies)
            pending_by_id = {p["norm_id"]: p for p in pending}
            target_lines = []
            for t in ok_targets:
                p = pending_by_id.get(t)
                if p:
                    target_lines.append(f"#msg{t} {p['nick']}: [{p['text']}]")
            targets_str = "\n".join(target_lines) or "the recent message"

            # 只有目标消息是“特别问句”时才使用引用回复；
            # 普通群聊发言应直接说话，不再每条都强制引用。
            should_quote = any(
                is_question_text(pending_by_id.get(t, {}).get("text", ""))
                for t in ok_targets
            )
            quote_rule = (
                f'Rule: start with <quote id="{ok_targets[0]}"/> then your natural reply. '
                if should_quote
                else (
                    "Reply naturally in the chatroom as a normal group message. "
                    "Do not use a quote tag in this reply.\n"
                )
            )

            history_text = "\n".join(history_slice)
            # 生成阶段同样需要人格锚定，否则模型只会收到“chatroom”裸提示
            anti_repeat_instr = ""
            if cfg.anti_repeat.enable and recent_own:
                anti_repeat_instr = (
                    "Recent own replies:\n"
                    + "\n".join(recent_own)
                    + "\n请勿重复以上内容，但保持你自己的人格语气。\n"
                )

            prompt_tmpl = (
                getattr(cfg.generate, "prompt_template", None)
                or DEFAULT_GENERATE_PROMPT
            )
            gen_prompt = build_generate_prompt(
                prompt_tmpl,
                history_text=history_text,
                targets_str=targets_str,
                quote_rule=quote_rule,
                anti_repeat_instr=anti_repeat_instr,
                recall_text=gen_recall,
            )

            allowed_ids = {extract_msg_id_from_line(line) for line in history_slice}
            allowed_ids.discard(None)

            # 管道模式：把生成交回 AstrBot 主管道（注入一条合成唤醒事件），
            # 由主流程完成 LLM 调用与发送，记忆召回/反思等钩子自然触发。
            if pipeline_mode:
                job = PipelineJob(
                    message_id=new_message_id(),
                    origin=origin,
                    prompt=gen_prompt,
                    system_prompt=persona_prompt,
                    query_text=recall_query,
                    image_urls=list(image_urls),
                    allowed_msg_ids=set(allowed_ids),
                    reply_targets=list(ok_targets),
                    should_quote=should_quote,
                    quote_policy=cfg.generate.quote_policy,
                    provider_id=cfg.generate.provider_id or "",
                    self_id=str(pending[-1].get("self_id") or ""),
                    group_id=str(pending[-1].get("group_id") or ""),
                    platform=str(pending[-1].get("platform") or ""),
                    nickname=persona_name,
                )
                if await self.dispatcher.dispatch(job):
                    logger.info(
                        f"self-reply | 插话已交回主管道 origin={origin} "
                        f"targets={ok_targets} should_quote={should_quote}"
                    )
                    return
                logger.warning(
                    "self-reply | pipeline dispatch 失败，回退插件直连生成"
                )
                self.dispatcher.discard(job)
                # 直连兜底需要召回块：用同一套拼装逻辑重新渲染一次
                gen_prompt = build_generate_prompt(
                    prompt_tmpl,
                    history_text=history_text,
                    targets_str=targets_str,
                    quote_rule=quote_rule,
                    anti_repeat_instr=anti_repeat_instr,
                    recall_text=recall_block,
                )

            gen_provider_id = cfg.generate.provider_id or None
            gen_provider = self._resolve_provider(gen_provider_id)
            if not gen_provider:
                logger.error("self-reply | gen provider not found")
                return

            response_text = await self._generate(
                gen_provider,
                gen_prompt,
                image_urls,
                cfg,
                persona_prompt=persona_prompt,
            )

            # Anti-repeat
            if cfg.anti_repeat.enable and cfg.anti_repeat.compare_window > 0:
                window = list(state.recent_bot_replies)[
                    -cfg.anti_repeat.compare_window :
                ]
                st_subset = OriginState(
                    session_chats=[],
                    image_registry={},
                    pending_messages=[],
                    replied_registry=state.replied_registry,
                    recent_bot_replies=deque(window, maxlen=20),
                    debounce_task=None,
                    last_reply_ts=0,
                    lock=asyncio.Lock(),
                )
                dup, ratio = self.tracker.find_duplicate(
                    st_subset, response_text, cfg.anti_repeat.similarity_threshold
                )
                if dup:
                    logger.info(
                        f"self-reply | dup hit ratio={ratio:.2f} retry={cfg.anti_repeat.max_retries}"
                    )
                    if cfg.anti_repeat.max_retries > 0:
                        for _ in range(cfg.anti_repeat.max_retries):
                            response_text = await self._generate(
                                gen_provider,
                                gen_prompt,
                                image_urls,
                                cfg,
                                persona_prompt=persona_prompt,
                            )
                            dup, ratio = self.tracker.find_duplicate(
                                st_subset,
                                response_text,
                                cfg.anti_repeat.similarity_threshold,
                            )
                            if not dup:
                                break
                        if dup:
                            logger.info("self-reply | dup persist; drop")
                            return
                    else:
                        return

            if response_text.strip() == "<refuse/>" or not response_text.strip():
                logger.info("self-reply | refuse or empty")
                return

            # Tags（allowed_ids 已在上方计算，供两种派发模式共用）
            quote_allowed = should_quote and cfg.generate.quote_policy != "none"
            if not quote_allowed:
                response_text = QUOTE_RE.sub("", response_text)
                response_text = QUOTE_CLOSE_RE.sub("", response_text)
                if not response_text.strip():
                    logger.info("self-reply | empty after removing quote; skip")
                    return
            chain = [Plain(response_text)]
            if quote_allowed and cfg.generate.quote_policy == "judge" and ok_targets:
                if not any(isinstance(c, Reply) for c in chain):
                    if not QUOTE_RE.search(response_text):
                        chain = [Reply(id=ok_targets[0])] + chain
            transformed = transform_result_chain(
                chain, parse_mention=True, allowed_msg_ids=allowed_ids
            )
            if transformed is None:
                # 无任何控制标签（含强制锚定后的链）时按原链发送
                transformed = chain
            if not transformed:
                return

            # Send
            # 在途守卫：生成期间若发生 /reset（状态已被清理或重建），
            # 说明本轮回复基于已作废的上下文，直接丢弃。
            if self.runtime.get(origin) is not state:
                logger.info(
                    f"self-reply | 会话在生成期间被重置，丢弃本轮回复 origin={origin}"
                )
                return

            chain_obj = MessageChain(chain=transformed)
            await StarTools.send_message(origin, chain_obj)

            self.tracker.mark_replied(state, ok_targets)
            self.tracker.register_bot_reply(state, response_text)
            state.last_reply_ts = time.time()
            state.session_chats.append(
                f"[You/{datetime.now().strftime('%H:%M:%S')}]: {clean_text(response_text)}"
            )
            if len(state.session_chats) > cfg.history.max_messages:
                removed = state.session_chats.pop(0)
                rid = extract_msg_id_from_line(removed)
                if rid:
                    state.image_registry.pop(rid, None)
            logger.info(f"self-reply | sent origin={origin} targets={ok_targets}")

            # 可选：把自主回复写回长期记忆插件，让纯插话群聊也能触发反思/总结
            if cfg.memory.record_bot_reply and shim is not None:
                await self.memory.record_bot_reply(
                    shim=shim,
                    text=clean_text(response_text),
                    timeout=cfg.memory.timeout_sec,
                )

    async def _generate(
        self,
        provider,
        prompt: str,
        image_urls: list[str],
        cfg: PluginConfig,
        persona_prompt: str = "",
    ) -> str:
        try:
            r = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    persist=False,
                    session_id=uuid4().hex,
                    image_urls=image_urls or None,
                    system_prompt=persona_prompt,
                ),
                timeout=cfg.global_settings.generate_timeout_sec,
            )
            text = r.completion_text or ""
            if not text.strip():
                # 某些模型/兼容网关会在 content 为空时把实际回复放在
                # reasoning_content 字段里；这里做兜底，避免把有效回复当空内容丢弃。
                text = r.reasoning_content or ""
            return text
        except asyncio.TimeoutError:
            logger.error("self-reply | generate timeout")
            return ""
        except Exception as e:
            logger.error(f"self-reply | generate fail: {e}")
            return ""

    # ---------------- 工具函数 ----------------

    async def _resolve_persona(self, origin: str) -> tuple[str, str, str]:
        """解析当前会话人格，返回 (persona_id, persona_name, persona_prompt)。"""
        persona_id = ""
        try:
            session_service_config = await sp.get_async(
                scope="umo",
                scope_id=origin,
                key="session_service_config",
                default={},
            )
            if isinstance(session_service_config, dict):
                persona_id = str(session_service_config.get("persona_id") or "").strip()
        except Exception as e:
            logger.debug(f"self-reply | 获取 session persona 失败: {e}")

        if not persona_id:
            try:
                curr_cid = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        origin
                    )
                )
                if curr_cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        origin, curr_cid
                    )
                    if conv and conv.persona_id:
                        persona_id = str(conv.persona_id).strip()
            except Exception as e:
                logger.debug(f"self-reply | 获取 conversation persona 失败: {e}")

        if not persona_id:
            try:
                cfg = self.context.get_config(umo=origin)
                persona_id = str(
                    cfg.get("provider_settings", {}).get("default_personality") or ""
                ).strip()
            except Exception:
                persona_id = ""

        if persona_id == "[%None]":
            return "", "none", "No persona mask."

        persona = None
        if persona_id:
            try:
                persona = next(
                    (
                        p
                        for p in self.context.persona_manager.personas_v3
                        if p.get("name") == persona_id
                    ),
                    None,
                )
            except Exception:
                persona = None

        if not persona:
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(
                    origin
                )
            except Exception:
                persona = {"name": "default", "prompt": ""}

        persona_name = str(persona.get("name") or "default")
        persona_prompt = str(persona.get("prompt") or "").strip()
        if not persona_prompt:
            persona_prompt = "You are a helpful and friendly assistant."
        return persona_id, persona_name, persona_prompt

    async def _resolve_persona_name(self, origin: str) -> str:
        _, name, _ = await self._resolve_persona(origin)
        return name

    async def _resolve_persona_mask(self, origin: str) -> str:
        _, _, mask = await self._resolve_persona(origin)
        return mask

    # ---------------- 记忆 / 知识库召回 ----------------

    @staticmethod
    def _build_event_shim(origin: str, pending: list[dict]) -> EventShim | None:
        """用待判定消息构造轻量事件对象，供 LivingMemory 的门禁/作用域解析使用。"""
        if not pending:
            return None
        target = None
        for p in reversed(pending):
            text = str(p.get("text") or "").strip()
            if text and text != "[Empty]":
                target = p
                break
        if target is None:
            target = pending[-1]
        return EventShim(
            origin=origin,
            sender_id=str(target.get("sender_id") or ""),
            sender_name=str(target.get("nick") or ""),
            group_id=str(target.get("group_id") or ""),
            platform=str(target.get("platform") or ""),
            platform_id=str(target.get("platform_id") or ""),
            self_id=str(target.get("self_id") or ""),
        )

    @staticmethod
    def _build_recall_query(pending: list[dict]) -> str:
        """把待判定消息拼成检索 query（去重，最近的消息优先，限长）。"""
        picked: list[str] = []
        for p in reversed(pending):
            text = str(p.get("text") or "").strip()
            if not text or text == "[Empty]" or text in picked:
                continue
            picked.append(text)
        if not picked:
            return ""
        return " | ".join(picked)[:200]

    async def _build_recall_block(
        self,
        origin: str,
        shim: EventShim | None,
        query: str,
        persona_id: str,
        cfg: PluginConfig,
    ) -> str:
        """召回长期记忆 + 知识库。

        两个来源**并发执行、各自独立超时**：任何一个慢/失败都不影响另一个，
        避免"知识库超时把已经检索到的记忆一起丢掉"。
        """
        if not cfg.memory.enable or shim is None or not query:
            return ""
        try:
            return await self._collect_recall(origin, shim, query, persona_id, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"self-reply | memory | 召回失败: {e}")
            return ""

    async def _collect_recall(
        self,
        origin: str,
        shim: EventShim,
        query: str,
        persona_id: str,
        cfg: PluginConfig,
    ) -> str:
        jobs = [
            self._recall_memory_block(shim, query, persona_id, cfg),
        ]
        if cfg.memory.kb_enable:
            jobs.append(self._recall_kb_block(origin, query, cfg))

        results = await asyncio.gather(*jobs, return_exceptions=True)

        blocks: list[str] = []
        for item in results:
            if isinstance(item, BaseException):
                logger.warning(f"self-reply | memory | 召回子任务异常: {item}")
                continue
            if item:
                blocks.append(str(item))

        if not blocks:
            return ""
        # 使用提示放在最前面，保证被截断时也不会丢；截断优先落在条目边界
        return truncate_block(
            RECALL_USAGE_HINT + "\n\n" + "\n\n".join(blocks), cfg.memory.max_chars
        )

    async def _recall_memory_block(
        self, shim: EventShim, query: str, persona_id: str, cfg: PluginConfig
    ) -> str:
        timeout = cfg.memory.timeout_sec
        try:
            return await asyncio.wait_for(
                self.memory.recall(
                    shim=shim,
                    query=query,
                    persona_id=persona_id or None,
                    top_k=cfg.memory.top_k,
                    max_chars=cfg.memory.max_chars,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(f"self-reply | memory | 记忆召回超时(>{timeout}s)")
            return ""
        except Exception as e:
            logger.warning(f"self-reply | memory | 记忆召回失败: {e}")
            return ""

    async def _recall_kb_block(self, origin: str, query: str, cfg: PluginConfig) -> str:
        timeout = cfg.memory.timeout_sec
        try:
            return await asyncio.wait_for(
                retrieve_kb_block(
                    self.context,
                    origin=origin,
                    query=query,
                    top_k=cfg.memory.kb_top_k,
                    max_chars=cfg.memory.max_chars,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(f"self-reply | memory | 知识库检索超时(>{timeout}s)")
            return ""
        except Exception as e:
            logger.warning(f"self-reply | memory | 知识库检索失败: {e}")
            return ""

    @staticmethod
    def _append_recall_if_missing(
        prompt: str, template: str, recall_block: str
    ) -> str:
        """模板没写 {recalled_memories} 时，把召回块追加到提示词末尾。

        放在末尾与 LivingMemory 自身的注入策略一致（不破坏前缀缓存），
        同时兼容"自定义模板未走 format"的分支。
        """
        if not recall_block:
            if RECALL_PLACEHOLDER in prompt:
                prompt = re.sub(
                    r"\n{3,}", "\n\n", prompt.replace(RECALL_PLACEHOLDER, "")
                )
                return prompt.strip()
            return prompt
        if RECALL_PLACEHOLDER in (template or ""):
            if RECALL_PLACEHOLDER in prompt:
                return prompt.replace(RECALL_PLACEHOLDER, recall_block)
            return prompt
        return f"{prompt}\n\n{recall_block}"

    # ---------------- 会话重置自愈 ----------------

    async def _core_session_fp(self, origin: str) -> tuple[str | None, int] | None:
        """核心会话指纹 (conversation_id, 历史条数)；读取失败返回 None。"""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        try:
            cid = await manager.get_curr_conversation_id(origin)
            if not cid:
                return (None, 0)
            conv = await manager.get_conversation(origin, cid)
            history = getattr(conv, "history", "") if conv else ""
            try:
                data = json.loads(history) if history else []
            except Exception:
                data = []
            if not isinstance(data, list):
                data = []
            return (str(cid), len(data))
        except Exception as e:
            logger.debug(f"self-reply | 读取核心会话指纹失败: {e}")
            return None

    async def _detect_core_session_reset(
        self, origin: str, state: OriginState
    ) -> bool:
        """检测 /reset 或 /new 造成的核心会话变化，并同步清空插话缓存。

        触发条件（保守，避免误清）：
          - 会话 id 变化（/new 新建会话）；
          - 核心会话历史由非空变为空（/reset 清空当前会话）。
        """
        fp = await self._core_session_fp(origin)
        if fp is None:
            return False

        prev = state.core_session_fp
        state.core_session_fp = fp
        if prev is None:
            return False

        prev_cid, prev_len = prev
        cid, length = fp
        if prev_cid and cid and cid != prev_cid:
            reason = "核心会话已切换（/new）"
        elif prev_len > 0 and length == 0:
            reason = "核心会话历史已清空（/reset）"
        else:
            return False

        logger.info(
            f"self-reply | 检测到{reason}，同步清空插话历史缓存: {origin}"
        )
        self._drop_state(origin, state)
        return True

    def _drop_state(self, origin: str, state: OriginState | None = None) -> None:
        """清理某个会话的运行状态。

        若当前正处于该会话自己的防抖任务里，先摘掉 debounce_task，
        避免把正在执行的任务取消掉。
        """
        current = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if state is not None and current is not None and state.debounce_task is current:
            state.debounce_task = None
        self.runtime.cleanup(origin)

    def _resolve_provider(self, provider_id: str | None):
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider()

    def _format_pending(
        self, pending: list[dict], state: OriginState, cfg: PluginConfig
    ) -> str:
        out = []
        for p in pending:
            replied_mark = (
                "[replied]" if p["norm_id"] in state.replied_registry else ""
            )
            out.append(
                f"[{p['nick']}/{p['sender_id']}{p['role']} #msg{p['norm_id']}{replied_mark}]: {p['text']}"
            )
        return "\n".join(out)

    # ---------------- Hook 3: 主管道（pipeline）模式 ----------------

    @filter.on_llm_request(priority=PIPELINE_HOOK_PRIORITY)
    async def on_pipeline_llm_request(self, event: AstrMessageEvent, req) -> None:
        """把本插件登记好的插话载荷写进主管道构造的 ProviderRequest。

        优先级必须高于长期记忆插件的召回钩子：本钩子先写入 prompt，
        随后 LivingMemory 的召回注入叠加在同一个 prompt 上（不会被覆盖）。
        """
        job = self.dispatcher.find(event)
        if job is None:
            return
        cfg = self._cfg()

        req.prompt = job.prompt
        if job.system_prompt:
            current = req.system_prompt or ""
            req.system_prompt = (
                f"{job.system_prompt}\n\n{current}" if current else job.system_prompt
            )
        # 合成事件的发送者是 bot 自己，去掉“某个用户正在说话”的提醒，避免误导
        self._strip_self_identity_reminder(req, job)
        if job.image_urls:
            req.image_urls = list(job.image_urls)
        if not cfg.dispatch.allow_tools and req.func_tool is not None:
            req.func_tool = None
        if not cfg.dispatch.keep_conversation:
            # 不挂核心会话：避免插话提示词被写进核心对话历史
            req.conversation = None

        logger.info(
            f"self-reply | pipeline 注入完成 origin={job.origin} "
            f"msg_id={job.message_id} keep_conv={cfg.dispatch.keep_conversation} "
            f"tools={'on' if cfg.dispatch.allow_tools else 'off'}"
        )

    @staticmethod
    def _strip_self_identity_reminder(req, job: PipelineJob) -> None:
        if not job.self_id or not req.system_prompt:
            return
        pattern = re.compile(
            rf"User ID:\s*{re.escape(str(job.self_id))}\s*,\s*Nickname:\s*[^\n<]*"
        )
        req.system_prompt = pattern.sub(
            "You are the sender of this turn (this is your own message).",
            req.system_prompt,
        )

    @filter.on_decorating_result()
    async def on_pipeline_decorating_result(self, event: AstrMessageEvent) -> None:
        """装饰主管道生成的结果：标签解析、强制引用兜底、拒答与防重复。

        装饰发生在 RespondStage 发送之前，所以在这里清空结果链即可实现
        “这一轮不发言”。
        """
        job = self.dispatcher.find(event)
        if job is None:
            return
        cfg = self._cfg()
        result = event.get_result()
        chain = list(getattr(result, "chain", None) or [])
        if not chain:
            job.dropped = True
            logger.info("self-reply | pipeline 结果为空，取消发送")
            return

        # 生成期间发生 /reset、状态被清理 → 本轮上下文已作废
        if self.runtime.get(job.origin) is None:
            result.chain = []
            job.dropped = True
            logger.info(f"self-reply | pipeline 会话已被重置，取消发送 {job.origin}")
            return

        if chain_has_refuse_tag(chain):
            result.chain = []
            job.dropped = True
            logger.info("self-reply | pipeline 命中 <refuse/>，取消发送")
            return

        transformed = transform_result_chain(
            chain, parse_mention=True, allowed_msg_ids=job.allowed_msg_ids
        )
        if transformed:
            chain = transformed
        if (
            job.should_quote
            and job.quote_policy == "judge"
            and job.reply_targets
            and not any(isinstance(c, Reply) for c in chain)
        ):
            chain = [Reply(id=job.reply_targets[0]), *chain]

        text = self._chain_text(chain)
        if not text.strip():
            result.chain = []
            job.dropped = True
            logger.info("self-reply | pipeline 结果无有效文本，取消发送")
            return

        state = self.runtime.get(job.origin)
        if (
            cfg.anti_repeat.enable
            and cfg.anti_repeat.compare_window > 0
            and state is not None
        ):
            dup, ratio = self.tracker.find_duplicate(
                self._recent_replies_view(state, cfg.anti_repeat.compare_window),
                text,
                cfg.anti_repeat.similarity_threshold,
            )
            if dup:
                result.chain = []
                job.dropped = True
                logger.info(
                    f"self-reply | pipeline 回复与近期重复 ratio={ratio:.2f}，取消发送"
                )
                return

        job.final_text = text
        result.chain = chain

    @staticmethod
    def _chain_text(chain: list) -> str:
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text or "")
        return "".join(parts)

    @staticmethod
    def _recent_replies_view(state: OriginState, window: int) -> OriginState:
        recent = list(state.recent_bot_replies)[-window:] if window > 0 else []
        return dataclass_replace(
            state, recent_bot_replies=deque(recent, maxlen=20)
        )

    def _record_pipeline_reply(self, job: PipelineJob) -> None:
        """发送成功后把主管道生成的回复写回插件自己的历史与去重记录。"""
        state = self.runtime.get(job.origin)
        if state is None:
            return
        text = job.final_text or ""
        if not text:
            return
        cfg = self._cfg()
        self.tracker.mark_replied(state, job.reply_targets)
        self.tracker.register_bot_reply(state, text)
        state.last_reply_ts = time.time()
        state.session_chats.append(
            f"[You/{datetime.now().strftime('%H:%M:%S')}]: {clean_text(text)}"
        )
        while len(state.session_chats) > cfg.history.max_messages:
            removed = state.session_chats.pop(0)
            rid = extract_msg_id_from_line(removed)
            if rid:
                state.image_registry.pop(rid, None)
        logger.info(
            f"self-reply | pipeline 回复已发送 origin={job.origin} "
            f"targets={job.reply_targets}"
        )

    # ---------------- Hook 4: reload 清理 ----------------

    async def terminate(self):
        for origin in list(self.runtime.origins.keys()):
            self.runtime.cleanup(origin)

    # ---------------- Hook 5: 监听会话重置 (/reset 或 /new) ----------------

    @filter.after_message_sent(priority=RESET_HOOK_PRIORITY)
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """当会话被重置时，同步清空该会话在插话插件中的群聊滑动窗口历史。

        使用最高优先级：AstrBot 的 hook 链在遇到 ``event.is_stopped()`` 时会
        中断后续 handler，命令事件（/reset、/new）在多数链路上到达这里前
        就已经是 stopped 状态，只有排在链首才能收到通知。
        """
        job = self.dispatcher.take(event)
        if job is not None and not job.dropped and job.final_text:
            self._record_pipeline_reply(job)
        self._handle_session_reset(event)

    def _handle_session_reset(self, event: AstrMessageEvent) -> bool:
        """会话重置清理（可单测的纯同步实现），返回是否执行了清理。"""
        try:
            flagged = bool(event.get_extra("_clean_ltm_session", False))
        except Exception:
            flagged = False
        if not flagged:
            return False
        origin = getattr(event, "unified_msg_origin", "")
        if not origin:
            return False
        logger.info(f"self-reply | 会话已重置，同步清空插话历史缓存: {origin}")
        self._drop_state(origin, self.runtime.get(origin))
        return True
