from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from datetime import datetime
from uuid import uuid4

from astrbot.api import logger, sp, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.star import Context
from astrbot.core.star.star_tools import StarTools

from .image_resolver import resolve
from .judge_utils import parse_judge_output
from .plugin_config import PluginConfig, parse_plugin_config
from .reply_tracker import ReplyTracker
from .runtime_state import OriginState, RuntimeState
from .tag_utils import (
    MENTION_CLOSE_RE,
    MENTION_RE,
    QUOTE_CLOSE_RE,
    QUOTE_RE,
    normalize_id,
    transform_result_chain,
)

MSG_ID_LINE_RE = re.compile(r"#msg(\w+)")

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


def clean_text(text: str) -> str:
    # 移除 quote/mention tag 再写历史
    text = QUOTE_RE.sub("", text)
    text = QUOTE_CLOSE_RE.sub("", text)
    text = MENTION_RE.sub(r"[At: \1]", text)
    text = MENTION_CLOSE_RE.sub("", text)
    return text.strip()


class Main(star.Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._config = parse_plugin_config(config or {})
        self.runtime = RuntimeState()
        self.runtime.max_origins = self._config.global_settings.max_origins
        self.tracker = ReplyTracker()

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

        async with state.lock:
            pending = list(state.pending_messages)
            state.pending_messages.clear()

            if self.tracker.on_cooldown(state, cfg.trigger.min_reply_interval_seconds):
                logger.info(
                    f"self-reply | cooldown hit origin={origin} pending={len(pending)}"
                )
                return

            # Build judge input
            history_slice = state.session_chats[-cfg.judge.history_messages :]
            judge_prompt = cfg.judge.prompt_template.format(
                persona_name=await self._resolve_persona_name(origin),
                persona_mask=await self._resolve_persona_mask(origin),
                pending_count=len(pending),
                pending_msgs=self._format_pending(pending, state, cfg),
                history_count=len(history_slice),
                history_lines="\n".join(history_slice),
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
            _, persona_prompt = await self._resolve_persona(origin)
            anti_repeat_instr = ""
            if cfg.anti_repeat.enable and recent_own:
                anti_repeat_instr = (
                    "Recent own replies:\n"
                    + "\n".join(recent_own)
                    + "\n请勿重复以上内容，但保持你自己的人格语气。\n"
                )

            gen_prompt = (
                f"You are in a chatroom. Chat history:\n{history_text}\n\n"
                f"You decided to reply to:\n{targets_str}\n\n"
                f"{quote_rule}"
                "Output only your reply, nothing else. Use the same language as the chatroom.\n"
                f"{anti_repeat_instr}"
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

            # Tags
            allowed_ids = {extract_msg_id_from_line(l) for l in history_slice}
            allowed_ids.discard(None)

            # 只有问句才允许引用；普通直接回复即使模型误加了 quote 标签也会被剥掉
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
            return r.completion_text or ""
        except asyncio.TimeoutError:
            logger.error("self-reply | generate timeout")
            return ""
        except Exception as e:
            logger.error(f"self-reply | generate fail: {e}")
            return ""

    # ---------------- 工具函数 ----------------

    async def _resolve_persona(self, origin: str) -> tuple[str, str]:
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
            return "none", "No persona mask."

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
        return persona_name, persona_prompt

    async def _resolve_persona_name(self, origin: str) -> str:
        name, _ = await self._resolve_persona(origin)
        return name

    async def _resolve_persona_mask(self, origin: str) -> str:
        _, mask = await self._resolve_persona(origin)
        return mask

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

    # ---------------- Hook 3: reload 清理 ----------------

    async def terminate(self):
        for origin in list(self.runtime.origins.keys()):
            self.runtime.cleanup(origin)
