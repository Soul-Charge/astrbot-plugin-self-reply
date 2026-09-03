# Self-Reply 插件施工手册

> **本文档是唯一施工依据**。快速模型按本手册逐文件施工，不得改动接口、不得扩张功能、不得引入未列出的依赖。

## 0. 施工前置检查

施工单位接收任务前，先确认以下。任何一条不满足，停止并报告：

- [ ] AstroBot data 目录: `/mnt/d/AstrBot/local-docker/astrbot/data/`
- [ ] 已存在的插件目录: `astrbot_plugin_self_reply/` (已建 Blueprint 文件)
- [ ] `astrbot_plugin_astrbot_enhance_mode` **已禁用** (AstrBot WebUI)
- [ ] AstrBot 内置 `active_reply.enable` 关闭
- [ ] Provider `deepseek-vision` 已配置于 AstrBot
- [ ] Python ≥ 3.10，AstrBot v4.24.5
- [ ] 测试可运行: `pytest` 可用

施工完成后在 ✅ 清单打钩并报告。

## 1. 目录布局（固定）

```
astrbot_plugin_self_reply/
├── BLUEPRINT.md          # 原蓝图(参考用)
├── PLAN.md               # 本文档
├── metadata.yaml
├── requirements.txt
├── plugin_config.py
├── runtime_state.py
├── tag_utils.py
├── image_resolver.py
├── reply_tracker.py
├── judge_utils.py
├── main.py
├── README.md
├── _conf_schema.json
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_plugin_config.py
    ├── test_tag_utils.py
    ├── test_image_resolver.py
    ├── test_reply_tracker.py
    └── test_judge_utils.py
```

## 2. 依赖

```
requirements.txt:
```
```
# 空,仅用 AstrBot 内置库
```

`metadata.yaml`:
```yaml
name: astrbot_plugin_self_reply
desc: 自主回复插件 - 防抖触发 + 结构化判定 + 引用锚定 + 防重复
version: v0.1.0
author: Soul-Charge
repo: ""
```

> 开发由来：本插件由 Soul-Charge 发起、AI 辅助从零开发。早期文档中的“阿汐”是参考 `astrbot_plugin_astrbot_enhance_mode`（作者 Axi404/阿汐）时误带入的署名，非实际作者。

## 3. 配置文件 ( `_conf_schema.json` + `plugin_config.py` )

### 组划分（7 组）

| 组名 | 字段 | 类型 | 默认 |
|---|---|---|---|
| `enable` | bool | false | 总开关 |
| `trigger` | `debounce_normal_seconds` | float | 5.0 |
|  | `debounce_quick_seconds` | float | 1.0 |
|  | `min_reply_interval_seconds` | float | 15.0 |
|  | `quick_trigger_keywords` | list[str] | [] |
|  | `max_pending_before_judge` | int | 20 |
| `judge` | `provider_id` | str | `deepseek/deepseek-v4-flash` |
|  | `history_messages` | int | 30 |
|  | `prompt_template` | str | (完整默认见 judge_utils) |
| `generate` | `provider_id` | str | `deepseek-vision` |
|  | `attach_recent_images` | int | 3 |
|  | `quote_policy` | str | `judge` (judge/model/none) |
|  | `include_role_tag` | bool | true |
|  | `include_sender_id` | bool | true |
| `anti_repeat` | `enable` | bool | true |
|  | `similarity_threshold` | float | 0.85 |
|  | `max_retries` | int | 1 |
|  | `compare_window` | int | 10 |
| `history` | `max_messages` | int | 50 |
| `whitelist` | `allowed_origins` | list[str] | [] |
| `global_settings` | `max_origins` | int | 500 |
|  | `judge_timeout_sec` | float | 45.0 |
|  | `generate_timeout_sec` | float | 60.0 |

### `plugin_config.py` 实现要点

- 用 `@dataclass(frozen=True)`
- `parse_plugin_config(raw: dict) -> PluginConfig`
- 容错转换：`_to_bool/_to_float/_to_int/_to_list`
- `quote_policy` 校验：非 `judge/model/none` → 回 `judge`
- 提供 `enabled` 属性

## 4. 运行时状态 ( `runtime_state.py` )

```python
import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field

@dataclass
class OriginState:
    session_chats: list[str] = field(default_factory=list)
    image_registry: dict[str, dict] = field(default_factory=dict)  # norm_id → {urls, captions}
    pending_messages: list[dict] = field(default_factory=list)  # {norm_id, nick, sender_id, text, has_image, ts, role}
    replied_registry: OrderedDict[str, float] = field(default_factory=OrderedDict)  # norm_id → ts
    recent_bot_replies: deque = field(default_factory=lambda: deque(maxlen=20))
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
```

## 5. 标签工具 ( `tag_utils.py` )

从 `astrbot_plugin_astrbot_enhance_mode/tag_utils.py` 完整迁移，并增加：

```python
VALID_ID_RE = re.compile(r"^[0-9]+$")

def normalize_id(raw: str | None) -> str:
    if not raw: return ""
    s = str(raw).strip()
    if s.startswith("#"): s = s[1:]
    if s.lower().startswith("msg"): s = s[3:]
    return s.strip()

def transform_result_chain(
    chain: list,
    parse_mention: bool,
    allowed_msg_ids: set[str] | None = None,
) -> list | None:
    # 源逻辑: 查找第一个 quote,嫁接 Reply
    # 增加: 若 allowed_msg_ids 非空且 quote id 不在其中,剥掉该 Reply
    # 其余逻辑同 enhance_mode
```

## 6. 图片 resover ( `image_resolver.py` )

```python
import base64, os
from urllib.parse import urlparse

def resolve(raw: str | None) -> str | None:
    """url → http 直传/file → base64/其他 → None"""
    if not raw: return None
    u = raw.strip()
    if u.startswith("http://") or u.startswith("https://"): return u
    if u.startswith("file://"):
        path = u[7:]
    elif os.path.isabs(u) or u.startswith("/"):
        path = u
    else:
        return None
    try:
        return _to_b64(path)
    except Exception:
        return None

def _to_b64(path: str) -> str | None:
    if not os.path.exists(path): return None
    with open(path, "rb") as f:
        data = f.read()
    mime, _ = __import__("mimetypes").guess_type(path)
    if not mime: mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
```

## 7. 回复记账 ( `reply_tracker.py` )

```python
import difflib, time
from runtime_state import OriginState

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
        # 容量 ~200,超出走 LRU
        while len(st.replied_registry) > 200:
            st.replied_registry.popitem(last=False)

    @staticmethod
    def find_duplicate(st: OriginState, text: str, threshold: float) -> tuple[bool, float]:
        best = 0.0
        for prev in st.recent_bot_replies:
            r = difflib.SequenceMatcher(None, prev, text).ratio()
            if r > best: best = r
            if r > threshold: return True, r
        return False, best

    @staticmethod
    def register_bot_reply(st: OriginState, text: str) -> None:
        st.recent_bot_replies.append(text)
```

## 8. 判定工具 ( `judge_utils.py` )

```python
import json, re

ALLOWED_DECISION = {"reply", "skip"}
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

def parse_judge_output(raw: str, fallback_id: str) -> dict:
    """
    解析 judge 输出为 {decision, target_ids, reason}.
    fallback_id = pending 最近一条 msg_id (用于 REPLY 容错)
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
                return {"decision": "reply" if dec.startswith("reply") else "skip",
                        "target_ids": ids, "reason": str(obj.get("reason", ""))}
        except Exception:
            pass
    # 兼容 REPLY/SKIP
    tok = text.split()[0].upper() if text else ""
    if tok.startswith("REPLY"):
        return {"decision": "reply", "target_ids": [fallback_id], "reason": "fallback REPLY parse"}
    return {"decision": "skip", "target_ids": [], "reason": "fallback SKIP parse"}
```

**默认 judge prompt 模板**（放在 `judge_utils.DEFAULT_JUDGE_PROMPT`）:

```
你当前的人格是:{persona_name}
人格设定:{persona_mask}

以下群聊最近 {pending_count} 条待判定消息:
{pending_msgs}

(历史上下文最近 {history_count} 条,已回复的已标为 [replied]):
{history_lines}

请以你的人格判断是否应该主动回复这些消息,并给出至多 1 个目标 msg_id:
{"decision":"reply","target_ids":["msgid"],"reason":"..."}
{"decision":"skip","reason":"..."}
只输出 JSON,不要额外文字。
```

## 9. 主插件 ( `main.py` )

### Hooks

```python
from astrbot.api.event import filter, AstrMessageEvent, MessageType
from astrbot.api.star import Context
from astrbot.api import star, logger
from astrbot.api.message_components import Plain, At, Reply, Image
from astrbot.core.star.star_tools import StarTools
from astrbot.core.message.message import MessageChain

class Main(star.Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        from .plugin_config import parse_plugin_config
        from .runtime_state import RuntimeState
        from .reply_tracker import ReplyTracker
        self._config = parse_plugin_config(config or {})
        self.runtime = RuntimeState()
        self.runtime.max_origins = self._config.global_settings.max_origins
        self.tracker = ReplyTracker()

    def _cfg(self) -> PluginConfig:
        return self._config
```

### Hook 1: on_group_message

```python
@filter.event_message_type(filter.EventMessageType.ALL, priority=9990)
async def on_group_message(self, event: AstrMessageEvent):
    cfg = self._cfg()
    if not cfg.enable: return
    if event.get_message_type() != MessageType.GROUP_MESSAGE: return
    if event.is_at_or_wake_command: return  # 唤醒消息由主流水线响应(v0.1.1 修正,原为放行快速通道,实测双重回答)
    # 同内容(发送者+文本)指纹窗口折叠: 平台可能双投递纯文本+At 两条事件,
    # 折叠后历史只记一次;若任一副本为唤醒消息,撤回 pending 防止抢答
    sender_id = event.get_sender_id()
    self_id = getattr(event.message_obj, "self_id", "")
    if sender_id and self_id and str(sender_id) == str(self_id): return  # bot自身

    # whitelist
    if cfg.whitelist.allowed_origins:
        if event.unified_msg_origin not in cfg.whitelist.allowed_origins \
           and (event.get_group_id() and event.get_group_id() not in cfg.whitelist.allowed_origins):
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
            if qid: parts.append(f" [Quote #msg{qid} {qnick}: {qtext}]")
            else: parts.append(f" [Quote {qnick}: {qtext}]")
        elif isinstance(comp, Plain): parts.append(f" {comp.text}")
        elif isinstance(comp, Image): parts.append(" [Image]")
        elif isinstance(comp, At): parts.append(f" [At: {comp.name}]")
    line = "".join(parts)
    state = self.runtime.touch(event.unified_msg_origin)
    state.session_chats.append(line)
    if len(state.session_chats) > cfg.history.max_messages:
        removed = state.session_chats.pop(0)
        rid = extract_msg_id_from_line(removed)
        if rid: state.image_registry.pop(rid, None)

    if norm_id and image_urls:
        state.image_registry[norm_id] = {"urls": image_urls, "captions": {}}

    # pending
    state.pending_messages.append({
        "norm_id": norm_id, "nick": nick, "sender_id": sender_id,
        "text": text, "has_image": bool(image_urls), "role": role,
        "ts": time.time(),
    })
    if len(state.pending_messages) > cfg.trigger.max_pending_before_judge:
        state.pending_messages.pop(0)

    # debounce 调度
    quick = self._is_quick(event, cfg)
    self._schedule_debounce(event.unified_msg_origin, quick, cfg)
```

### Hook 2: debounce

```python
def _is_quick(self, event: AstrMessageEvent, cfg: PluginConfig) -> bool:
    if event.is_at_or_wake_command: return True
    text = (event.message_str or "").strip()
    if any(k in text for k in cfg.trigger.quick_trigger_keywords): return True
    if "?" in text or "?" in text: return True
    for comp in event.get_messages():
        if isinstance(comp, Reply):
            # 若 reply 对象是 bot 自身
            return True
    return False

def _schedule_debounce(self, origin: str, quick: bool, cfg: PluginConfig) -> None:
    state = self.runtime.get(origin)
    if not state: return
    delay = cfg.trigger.debounce_quick_seconds if quick else cfg.trigger.debounce_normal_seconds
    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()
    state.debounce_task = asyncio.create_task(
        self._debounce_fire(origin, delay)
    )
```

```python
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
    if not state: return
    if not state.pending_messages: return

    async with state.lock:
        pending = list(state.pending_messages)
        state.pending_messages.clear()

        if self.tracker.on_cooldown(state, cfg.trigger.min_reply_interval_seconds):
            logger.info(f"self-reply | cooldown hit origin={origin} pending={len(pending)}")
            return

        # Build judge input
        history_slice = state.session_chats[-cfg.judge.history_messages:]
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
                provider.text_chat(prompt=judge_prompt, persist=False, session_id=uuid4().hex),
                timeout=cfg.global_settings.judge_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.error("self-reply | judge timeout"); return
        except Exception as e:
            logger.error(f"self-reply | judge fail: {e}"); return

        fallback_id = pending[-1]["norm_id"] if pending else ""
        result = parse_judge_output(
            (resp.completion_text or "").strip(),
            fallback_id=fallback_id,
        )
        logger.info(f"self-reply | judge origin={origin} decision={result}")

        if result["decision"] != "reply": return
        target_ids = [normalize_id(t) for t in result.get("target_ids") or []]
        ok_targets = self.tracker.filter_unreplied(state, target_ids)
        if not ok_targets:
            logger.info("self-reply | all targets already replied; skip")
            return

        # Gather images
        image_urls = []
        if cfg.generate.attach_recent_images > 0:
            recent_ids = [k for k in list(state.session_chats)[-cfg.judge.history_messages:]]
            # 从逆序收集至多 N 条有图的记录
            ctr = 0
            for line in reversed(state.session_chats):
                rid = extract_msg_id_from_line(line)
                if rid and rid in state.image_registry:
                    for u in state.image_registry[rid]["urls"]:
                        resolved = resolve(u)
                        if resolved: image_urls.append(resolved); ctr += 1
                            if ctr >= cfg.generate.attach_recent_images: break
                if ctr >= cfg.generate.attach_recent_images: break

        # Generate
        recent_own = list(state.recent_bot_replies)
        targets_str = "\n".join(
            f"#msg{t} {[p for p in pending if p['norm_id']==t][0]['nick']}: "
            f"[{p for p in pending if p['norm_id']==t][0]['text']}"
            for t in ok_targets if any(p['norm_id']==t for p in pending)
        ) or "the recent message"

        history_text = "\n".join(history_slice)
        anti_repeat_instr = ""
        if cfg.anti_repeat.enable and recent_own:
            anti_repeat_instr = "Recent own replies:\n" + "\n".join(recent_own) + "\n请勿重复以上风格。\n"

        gen_prompt = (
            f"You are in a chatroom. Chat history:\n{history_text}\n\n"
            f"You decided to reply to:\n{targets_str}\n\n"
            f"Rule: start with <quote id=\"{ok_targets[0]}\"/> then your natural reply. "
            "Output only your reply, nothing else. Use the same language as the chatroom.\n"
            f"{anti_repeat_instr}"
        )

        gen_provider_id = cfg.generate.provider_id or None
        gen_provider = self._resolve_provider(gen_provider_id)
        if not gen_provider:
            logger.error("self-reply | gen provider not found"); return

        response_text = await self._generate(gen_provider, gen_prompt, image_urls, cfg)

        # Anti-repeat
        if cfg.anti_repeat.enable and cfg.anti_repeat.compare_window > 0:
            window = list(state.recent_bot_replies)[-cfg.anti_repeat.compare_window:]
            st_subset = OriginState(
                session_chats=[], image_registry={}, pending_messages=[],
                replied_registry=state.replied_registry,
                recent_bot_replies=deque(window, maxlen=20),
                debounce_task=None, last_reply_ts=0, lock=asyncio.Lock(),
            )
            dup, ratio = self.tracker.find_duplicate(st_subset, response_text, cfg.anti_repeat.similarity_threshold)
            if dup:
                logger.info(f"self-reply | dup hit ratio={ratio:.2f} retry={cfg.anti_repeat.max_retries}")
                if cfg.anti_repeat.max_retries > 0:
                    for _ in range(cfg.anti_repeat.max_retries):
                        response_text = await self._generate(gen_provider, gen_prompt, image_urls, cfg)
                        dup, ratio = self.tracker.find_duplicate(st_subset, response_text, cfg.anti_repeat.similarity_threshold)
                        if not dup: break
                    if dup:
                        logger.info("self-reply | dup persist; drop"); return
                else:
                    return

        if response_text.strip() == "<refuse/>" or not response_text.strip():
            logger.info("self-reply | refuse or empty"); return

        # Tags
        allowed_ids = {extract_msg_id_from_line(l) for l in history_slice}
        allowed_ids.discard(None)
        chain = [Plain(response_text)]
        if cfg.generate.quote_policy == "judge" and ok_targets:
            if not any(isinstance(c, Reply) for c in chain):
                if not QUOTE_RE.search(response_text):
                    chain = [Reply(id=ok_targets[0])] + chain
        transformed = transform_result_chain(chain, parse_mention=True, allowed_msg_ids=allowed_ids)
        if not transformed:
            # might be pure refuse; skip
            return

        # Send
        chain_obj = MessageChain(chain=transformed)
        await StarTools.send_message(origin, chain_obj)

        self.tracker.mark_replied(state, ok_targets)
        self.tracker.register_bot_reply(state, response_text)
        state.last_reply_ts = time.time()
        state.session_chats.append(f"[You/{datetime.now().strftime('%H:%M:%S')}]: {clean_text(response_text)}")
        if len(state.session_chats) > cfg.history.max_messages:
            removed = state.session_chats.pop(0)
            rid = extract_msg_id_from_line(removed)
            if rid: state.image_registry.pop(rid, None)
        logger.info(f"self-reply | sent origin={origin} targets={ok_targets}")

    async def _generate(self, provider, prompt: str, image_urls: list[str], cfg: PluginConfig) -> str:
        try:
            r = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt, persist=False, session_id=uuid4().hex,
                    image_urls=image_urls or None,
                ),
                timeout=cfg.global_settings.generate_timeout_sec,
            )
            return r.completion_text or ""
        except asyncio.TimeoutError:
            logger.error("self-reply | generate timeout"); return ""
        except Exception as e:
            logger.error(f"self-reply | generate fail: {e}"); return ""
```

### 工具函数 (main.py 内部)

```python
def _resolve_persona_name(self, origin: str) -> str: ...
def _resolve_persona_mask(self, origin: str) -> str: ...
def _resolve_provider(self, provider_id: str | None) -> Provider | None:
    if provider_id:
        return self.context.get_provider_by_id(provider_id)
    return self.context.get_using_provider()

def _format_pending(self, pending: list[dict], state: OriginState, cfg: PluginConfig) -> str:
    out = []
    for p in pending:
        replied_mark = "[replied]" if p["norm_id"] in state.replied_registry else ""
        out.append(f"[{p['nick']}/{p['sender_id']}{p['role']} #msg{p['norm_id']}{replied_mark}]: {p['text']}")
    return "\n".join(out)

def extract_msg_id_from_line(line: str) -> str | None:
    m = re.search(r"#msg(\w+)", line)
    return m.group(1) if m else None

def clean_text(text: str) -> str:
    # 移除 quote/mention tag 再写历史
    text = QUOTE_RE.sub("", text)
    text = QUOTE_CLOSE_RE.sub("", text)
    text = MENTION_RE.sub(r"[At: \1]", text)
    text = MENTION_CLOSE_RE.sub("", text)
    return text.strip()
```

### Hook 3: cleanup on reload

```python
async def terminate(self):
    for origin in list(self.runtime.origins.keys()):
        self.runtime.cleanup(origin)
```

## 10. 测试 ( `tests/` )

- `conftest.py`: 公共 mock
- `test_plugin_config.py`: 默认值+parse 容错
- `test_tag_utils.py`: mention/quote/refuse/校验
- `test_image_resolver.py`: file→base64/http 直传/不可识别
- `test_reply_tracker.py`: cooldown/重定/相似度
- `test_judge_utils.py`: JSON/fallback/格式异常

## 11. README.md

```
- 用途 / 功能列表 / 配置说明 / 前置条件 / 使用示例 / 测试
```

## 12. 施工顺序 (严格)

1. metadata.yaml + requirements.txt
2. _conf_schema.json (参照组划分)
3. plugin_config.py
4. runtime_state.py
5. tag_utils.py
6. image_resolver.py
7. reply_tracker.py
8. judge_utils.py
9. main.py
10. README.md
11. tests/ (每文件 1 个)
12. 跑 pytest 验证

## 13. 验收清单

- [ ] metadata 格式正确，name 无破折号以外特殊符号
- [ ] 所有 imports 都能 import
- [ ] 触发判定走 debounce+lock
- [ ] judge 解析兼容 JSON/REPLY
- [ ] send 主动走 StarTools
- [ ] anti_repeat 终检执行
- [ ] quote_policy judge 时强制 anchor
- [ ] pytest 通过
- [ ] BLUEPRINT/PLAN 删除或保留与文档同步即可

## 14. 报告

完成后，施工单位向审批人报告：
1. 文件列表 + 行数
2. 测试结果 (`pytest -q`)
3. 验收清单

**审批人检查通过后，在 AstrBot WebUI 启用插件并加载。**
