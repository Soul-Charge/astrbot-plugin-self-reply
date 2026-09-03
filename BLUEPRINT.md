# Self-Reply 插件设计蓝图

> 本文件为实施前的最终蓝图，施工严格按此执行；任何变更需先更新本文档。

## 插件基本信息

- **目录**: `data/plugins/astrbot_plugin_self_reply/`
- **版本**: v0.1.0
- **作者**: Soul-Charge
- **定位**: 自主回复（主动回复/触发式回复），单职责，不做记忆/图片转述/WebUI
- **开发由来**: 由 Soul-Charge 发起、AI 辅助从零开发；早期文档中的“阿汐”为参考 enhance_mode 时误带入的署名，非实际作者。
- **参考**: `astrbot_plugin_astrbot_enhance_mode`（作者 Axi404/阿汐，https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode），借鉴其标签约定、历史格式与主动回复分层思路。

## 前置条件（施工前检查）

- [ ] `astrbot_plugin_astrbot_enhance_mode` 在 AstrBot WebUI 中已禁用
- [ ] livingmemory 已启用（长期记忆职责边界）
- [ ] AstrBot 内置 `active_reply.enable` / `group_icl_enable` 关闭，避免双判定

## 决策表（用户已确认）

| 决策项 | 值 |
|---|---|
| 主模型/judge 模型 | `deepseek/deepseek-v4-flash` |
| 图片模型 | `deepseek-v4-flash-vision-exp` |
| 发送方式 | 方案 A：debounce task + `StarTools.send_message` |
| 群历史长度 | 50 条 |
| 历史图片线程 | image_registry 维护 url 注册，generate 时附 max 3 张 |
| judge 结构化输出 | JSON `{decision, target_ids, reason}`，兼容 REPLY/SKIP |
| quote 策略 | 默认 `judge`（强制锚定），可选 `model`（校验存在性） |
| 防重复 | 生成前注入近似回复 + 相似度终检 |
| 快速通道 | `?/？`、Reply@bot、@bot、关键词 |
| 冷却机制 | 每 origin 最小回复间隔 |

## 模块拆分

### `plugin_config.py` (~130 行)

```python
@dataclass(frozen=True)
class TriggerConfig:
    debounce_normal_seconds: float = 5.0
    debounce_quick_seconds: float = 1.0
    min_reply_interval_seconds: float = 15.0
    quick_trigger_keywords: list[str] = []  # 关键词触发快速通道

@dataclass(frozen=True)
class JudgeConfig:
    provider_id: str = "deepseek/deepseek-v4-flash"  # 沿用主模型
    history_messages: int = 30
    max_pending_before_judge: int = 20  # 防抖窗口过长时最多合并多少条
    prompt_template: str = DEFAULT_JUDGE_PROMPT

@dataclass(frozen=True)
class GenerateConfig:
    attach_recent_images: int = 3  # 0=关闭；最多附带多少张近期图片原图
    provider_id: str = ""  # 空=会话默认（允许指定 vision 模型）
    quote_policy: str = "judge"  # judge | model | none

@dataclass(frozen=True)
class AntiRepeatConfig:
    enable: bool = True
    similarity_threshold: float = 0.85
    max_retries: int = 1  # 触发相似度时重试次数
    compare_window: int = 10  # 与 bot 自己最近 N 条回复比较

@dataclass(frozen=True)
class HistoryConfig:
    max_messages: int = 50
    include_sender_id: bool = True
    include_role_tag: bool = True
    include_time: bool = True

@dataclass(frozen=True)
class WhitelistConfig:
    origins: list[str] = []  # unified_msg_origin 或 group_id

@dataclass(frozen=True)
class GlobalSettings:
    max_origins: int = 500
    judge_timeout_sec: float = 45.0
    generate_timeout_sec: float = 60.0

@dataclass(frozen=True)
class PluginConfig:
    trigger: TriggerConfig
    judge: JudgeConfig
    generate: GenerateConfig
    anti_repeat: AntiRepeatConfig
    history: HistoryConfig
    whitelist: WhitelistConfig
    global_settings: GlobalSettings
    enable: bool = False  # 总开关

    @property
    def enabled(self) -> bool:
        return self.enable
```

### `runtime_state.py` (~90 行)

```python
class OriginState:
    session_chats: list[str] = []          # [nick/id/时间(role) #msgId: text]
    image_registry: dict[str, list[str]]   # norm_msg_id → [url, ...]
    pending_messages: list[dict]           # {msg_id, nick, sender_id, text, has_image, timestamp}
    replied_registry: OrderedDict[str, float]  # norm_msg_id → ts (cap 200)
    recent_bot_replies: deque[str]         # bot 最近 N 条回复 (cap 10)
    debounce_task: asyncio.Task | None
    last_reply_ts: float
    lock: asyncio.Lock

class RuntimeState:
    origins: dict[str, OriginState]
    lru: OrderedDict[str, None]
    def touch(self, origin): ...
    def get(self, origin) -> OriginState: ...
    def cleanup(self, origin): ...
```

### `tag_utils.py` (~90 行)

从 enhance_mode 移植 `MENTION_RE/QUOTE_RE/TRANSFORM`，并增加：
```python
def transform_result_chain(chain, parse_mention=True, allowed_msg_ids: set[str]|None = None) -> list
    # quote id 若不在 allowed_msg_ids 中（近期历史 msg_id 集合），剥掉 Reply
    # 老逻辑同样处理 mention/refuse
```

### `reply_tracker.py` (~120 行)

```python
class ReplyTracker:
    def is_on_cooldown(origin_state, min_interval) -> bool
    def already_replied(origin_state, msg_ids: Iterable[str]) -> list[str]  # 返回已回复过得 subset
    def mark_replied(origin_state, msg_ids)
    def is_duplicate(origin_state, new_text, threshold) -> bool  # difflib ratio
    def register_bot_reply(origin_state, text)  # 进 recent_bot_replies
```

### `main.py` (~320 行)

四类职责：
1. `on_group_message` 入口后置 debounce
2. `_judge_with_target`：judge prompt + JSON 解析 + 容错回退
3. `_generate_reply`：生成 prompt、附加图片、终检、重试
4. `_send_and_record`：tag 变换、发送、记账、记录

## 主流程

```
on_group_message (priority=9990):
  1. 基础过滤：非 GROUP_MESSAGE / bot 自身 / 空 / 无 enable → return
  2. self._record(event): 更新 session_chats 与 image_registry
  3. quick = is_quick_trigger(event)
  4. self._schedule_debounce(origin, quick)
     - 取消旧 debounce_task
     - 创建新 task: asyncio.sleep(normal or quick) → self._debounce_fire(origin, event)
     - 注: event 对象不在 task 里使用（方案A 主动发送），task 只 origin 工作

_debounce_fire(origin):
  async with origin.lock:
    st = self.runtime.get(origin)
    if not st.pending_messages: return
    if self._on_cooldown(st): 
       st.pending_messages.clear() (放弃判定)  # 或者移动回历史待下轮
       return
    # Judge
    decision = await _judge_with_target(origin, st.pending_messages, st.session_chats)
    if decision.decision != "reply":
        st.pending_messages.clear(); return
    # 终检
    targets = [t for t in decision.target_ids if not already_replied(st, t)]
    if not targets: st.pending_messages.clear(); return

    # Generate (可能携带图片)
    recent_image_urls: 通过 image_registry 从近历史收集, 最多 attach_recent_images 张
    prompt = build_generate_prompt(targets, st.recent_bot_replies, recent_image_urls)
    resp_text = provider.text_chat(prompt, image_urls=recent_image_urls, ...)

    # 终检
    if resp_text == "<refuse/>": 放弃
    if anti_repeat.enable and is_duplicate(st, resp_text): 重试 max_retries 次或放弃
    chain = [Plain(resp_text)]
    if generate.quote_policy == "judge":
         chain = prepend_if_no_quote(chain, targets[0])
    chain = transform_result_chain(chain, allowed_msg_ids=get_recent_msg_ids(st))

    # Send
    await StarTools.send_message(origin, MessageChain(chain=chain))

    # Record
    reply_tracker.mark_replied(st, targets)
    reply_tracker.register_bot_reply(st, resp_text)
    session_chats.append(f"[You/{time}]: {cleaned_text}")
    st.last_reply_ts = now
    st.pending_messages.clear()
```

## Judge Prompt 默认模板

```
你当前的人格是:{persona_name}
人格设定:{persona_mask}

以下是群聊最近 {pending_count} 条消息:
{pending_msgs}

(此前已回复过的消息已用 [replied] 标记)

最近群组历史上下文:
{history_lines}

请以{persona_name}的角度判断是否需要主动回复。
只输出 JSON,不要其他文字:
{"decision":"reply","target_ids":["msgid1"],"reason":"..."}
或
{"decision":"skip","reason":"..."}

target_ids 必须是上面消息中的 #msgXXX 数字部分,可以多选或只选一条。
```

容错解析：
1. `json.loads` 成功 → 走 `decision/target_ids`
2. 否则正则找 `REPLY` → 用 pending 最近一条的 msg_id 作为 target
3. 否则默认 skip

## Generate Prompt 模板

```
You are in a chatroom. History:
{history_lines}

You decided to reply to: #msgXXX [nick/id/time]: content

Recent own replies (避免重复风格):
{recent_bot_replies}

注意:请不要重复上述内容。
Your entire output is your reply. 开头用 <quote id="msgXXX"/> tag。
```

## schema

`_conf_schema.json` 对应 7 组配置,每组若干字段,启用状态按组。

## 策略与映射

| 策略 | 实现 |
|---|---|
| 快速通道 | `event.message_obj.is_at_or_wake_command` 或 Raw 中含 `?/？` 或关键词或 Reply 到 bot |
| 冷却 | `min_reply_interval_seconds` 时间窗,超窗自动放弃本次 |
| 防重复 | `difflib.SequenceMatcher.ratio() > threshold` |
| 引用锚定 | `prepend_if_no_quote(chain, msg_id)` / `transform_result_chain` 校验 |
| 并发 | per-origin `asyncio.Lock` 包整个 debounce_fire |

## 测试

- `test_config.py`：默认值、parse 容错、类型转换
- `test_judge_parse.py`：JSON 成功/容错回退/格式异常
- `test_reply_tracker.py`：冷却/已回复标记/相似度
- `test_tag_utils.py`：mention/quote/refuse + allowed 校验

## 风险与 edge case

- Debounce task 在 `terminate()` 中必须 cancel
- `StarTools.send_message` 失败需要日志 + 记录不注册 replied_registry
- pending 长度超 `max_pending_before_judge` 时立即 fire
- origin 突然整改/删会话：after_message_sent 清理
- bot 自身消息过滤

## 施工顺序

1. metadata.yaml + requirements.txt
2. plugin_config.py
3. runtime_state.py
4. tag_utils.py
5. reply_tracker.py
6. main.py
7. _conf_schema.json
8. README.md
9. tests/
