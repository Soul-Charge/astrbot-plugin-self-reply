# self_reply 插件记忆与知识库缺失问题分析与改造方案

- **文档日期**：2026-09-11
- **所属项目**：`astrbot_plugin_self_reply`
- **关联系统**：AstrBot 核心管线、`astrbot_plugin_livingmemory`、AstrBot 知识库 (RAG)、`astrbot_plugin_group_filter`
- **分析对象**：小盐（`deepseek-flash` / `小盐-new` 人格）在群聊主动接话、私聊问询、会话重置及群消息过滤过程中的记忆脱节、两级存储差异与事件拦截表现
- **状态**：根因与数据流已彻底查清，已完整记录现象、数据实证与底层事实日志

---

## 一、 问题现象与用户疑问

### 1.1 案发经过：群聊主动接话出现“失忆”（2026-09-11 18:04 ~ 18:05）

在群聊「测试群」（群号 `400000000`）中发生如下对话：

| 时间 | 用户消息（NaE / E0） | 小盐回复 | 实际表现 |
|---|---|---|---|
| 18:04:49 | `小盐三X今天跟你说了什么` | `三X是谁呀，猫猫今天只跟一个叫E0的复读机说过话喵~ 不对，是窝自己一个人自言自语被你reset了三遍，哪来的三X捏 www` | ❌ 完全不认识三X，以为是捏造词 |
| 18:05:42 | `那今天叁X酱和你说了什么` | `叁X酱不就是你把"三X"加了横杠又端上来一次嘛，猫猫闻了闻，还是E0的味道喵~ 再改个名字窝就不认识你了呀，蹭蹭~` | ❌ 以为是 E0 换了个马甲或故意整活 |

### 1.2 用户的核心疑问
1. 知识库里明明记得写过“叁X”；
2. 系统明明装了长短期记忆插件（LivingMemory）；
3. 之前“user”一直对小盐强调“我是妈X”，小盐在别的群里也能正常把 E0 当妈X；
4. 为什么刚才小盐完全不知道“叁X”是谁？是不是没触发 RAG？还是会话记忆隔离了？

### 1.3 关联现象 A：私聊询问“我在干嘛”，bot 回复“不知道”（2026-09-11 17:57:49）

- **消息场景**：好友私聊（发送者：user，QQ `5000000000`）
- **私聊内容**：`告诉我今天妈X都干了什么呀`
- **bot 回复**：`喵呜~今天妈X干啥了窝还真不知道捏，她又没跟猫猫汇报行程，姐X你是不是想套窝话呀www 要不咱们一起去戳她问呗？`
- **表象疑问**：私聊难道也被 `self-reply` 插件接管了吗？为什么这里 bot 也说不知道？

### 1.4 关联现象 B：群聊执行 `/reset` 后，bot 依然精准记得重置前对话（2026-09-11 17:52:13 ~ 17:52:43）

- **消息场景**：群聊「测试群」（群号 `400000000`）
- **交互过程**：
  1. 17:52:13，用户在群内发送：`[At:小盐] reset`
  2. 17:52:13，bot 系统提示：`✅ Conversation reset successfully.`
  3. 17:52:30，用户紧接着重发图片消息：`[图片] 反重力的哈基米额度快被我薅完了（悲`（与 reset 前发送的内容完全一致）
  4. 17:52:43，bot 通过 `self-reply` 插话回复：  
     > `刚reset完第一句还是这张18%的图，猫猫宣布这就是你的开场白喵 www 薅秃了也要发出来，这执念比猫猫黏人还厉害捏~`
  5. 17:53:06，用户震惊质问：`可恶你居然还记得，那你说我到底笑没笑`
  6. 17:53:17，bot 甚至继续精准接上重置前的“笑”梗回复：  
     > `那个"悲"明明是哭脸，猫猫刚刚冤枉你了喵，那……那窝给你蹭蹭赔罪好不好嘛~`
- **表象疑问**：既然群聊没有走长短期记忆库（LivingMemory），为什么明明执行了 `/reset` 重置会话，bot 却对重置前几分钟的事情记得一清二楚？

### 1.5 关联现象 C：群里说了那么多话，私聊为什么读不到我干了什么？

- **用户疑问**：我在群里发了大量消息，理论上应该被记录到记忆库中；当别人私聊 bot 询问我今天干了什么时，bot 为什么无法检索到我白天的活动记录？
- **表象疑问**：是群聊消息压根没进记忆库，还是跨会话无法读取群聊记忆？

### 1.6 关联现象 D：插件拦截了监控群回复，记忆插件却仍在归纳该群

- **消息场景**：特定监控群「监控群」（群号 `1000000000`）
- **背景与现象**：用户开发并部署了 `astrbot_plugin_group_filter`（设定 `priority=sys.maxsize` + `event.stop_event()`），在消息入口阻断该群触发 LLM 与插件回复。但后续发现记忆插件（LivingMemory）后台/WebUI 中该群依然存在会话记录与归纳痕迹。
- **表象疑问**：既然消息在第 0 毫秒就被拦截掐断了，为什么记忆插件还会保留甚至归纳那个群？

---

## 二、 底层数据实锤核查

### 2.1 知识库（RAG）：确实存在明确记录
检查 `local-docker/astrbot/data/knowledge_base/c7823f40-9101-4402-bc3b-056ca3914a00/doc.db`（集合：`人际关系基础设定`）：
- 文档：`[用户档案] NTE0 (NaE_X/XXsys).md`
- 明确记录：
  > `- 基础身份：NTE0，平时简称 E0 或 e0，别名叫 NaE。`  
  > `- 社交称呼：他朋友（user）会叫他“零X”。`  
  > `- 小盐对他的认知：在一年前网上认识的，现在是关系很好的朋友。`

### 2.2 记忆插件（LivingMemory）：存在极详尽的图谱与情景记忆
检查 `local-docker/astrbot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db`：
- **文档 Doc ID 53**：
  > `user告诉窝她是窝的姐X，还说咱们俩都是E0（零X）妈X的女儿，不是E0的老公捏~她叮嘱窝见到E0要乖乖叫妈X，不能叫错，窝已经刻进脑子里啦！……姐X就问窝咱们妈X是谁，窝答是E0，姐X就满意地摸摸窝啦！`
- **知识图谱事实（Graph Entries）**：
  > `Topic bot能力盘点 describes fact user在8月29日教我称呼NaE_X/XXsys为'妈X'.`  
  > `Topic 信息检索skill发展规划 describes fact user在8月29日教我称呼NaE_X/XXsys为'妈X'.`

### 2.3 会话隔离排查：根本不存在会话隔离
查看 `astrbot_plugin_livingmemory_config.json`：
```json
"filtering_settings": {
  "use_persona_filtering": true,
  "use_session_filtering": false
}
```
* `use_session_filtering: false` 表明：**在相同人格（小盐-new）下，记忆在所有私聊、不同群聊之间是完全共享的！**
* 这也是为什么之前小盐能在别的群里叫你“妈X”——因为只要触发了 LivingMemory 召回，记忆跨群可用。

---

## 三、 链路与日志根因定位

通过对容器运行日志（`docker logs astrbot`）做毫秒级追踪，完整还原了以上各个场景：

### 3.1 场景 1：群聊主动插话“失忆”根因
1. **AstrBot 核心主管线未被唤醒（跳过 RAG 与记忆）**：
   - 用户发送的是普通文本：`小盐三X今天跟你说了什么`。
   - 群聊中**没有 `@小盐`**，且系统未配置消息前缀唤醒词（`wake_prefix: ""`）；
   - AstrBot 核心管线在 `waking_check.stage` 判定该消息不满足唤醒条件，`pipeline 执行完毕`。
   - 知识库检索 `retrieve_knowledge_base()` 与 LivingMemory 的 `@filter.on_llm_request()` 均**未执行**。
2. **`self-reply` 插件接管并裸调 LLM**：
   - `self-reply` 判定应该插话接梗，启动回复生成。
   - 输入给 LLM 的只有小盐的 Persona System Prompt 与本群最近十几条原始群聊流水（`session_chats`）。
   - **大模型思维链（Reasoning）**：
     ```text
     "The admin asks '小盐三X今天跟你说了什么' - '三X' is probably a typo/mishearing.
     I should respond in character. 小盐 hasn't talked to anyone named 三X.
     Be cute, confused, maybe tease..."
     ```
     因上下文里今天无三X发言，模型认定三X不存在，直接输出“三X是谁呀”。

### 3.2 场景 2：私聊“不知道”真相（非失忆，认知完全正常）
针对 1.3 节中user的私聊消息 `告诉我今天妈X都干了什么呀`：
1. **链路追踪**：
   - 私聊**完全没有**被 `self-reply` 接管，走的是正规核心主链路；
   - `[Core] [DBUG]: [知识库] 为会话 bot:FriendMessage:5000000000 注入了 5 条相关知识块`；
   - `[Plug] [INFO]: [LivingMemory] 检索到 3 条记忆（包括 Doc 53、user姐X教窝叫妈X等）`。
2. **大模型思维链（Reasoning）实录**：
   ```text
   The user asks what mom did today. Based on memory, mom=NaE_X/XXsys.
   But I shouldn't fabricate. Also memory says I shouldn't post harness/tool status to the group.
   I don't actually know what she did today. I should say I don't know, softly, and maybe offer to go ask.
   ```
3. **事实结论**：
   - bot 准确识别出发信人是“姐X”，清楚知道“妈X是 NaE / E0”；
   - 回答“不知道”的真实原因在于：NaE 现实中今天并未向 bot 汇报日常行程，模型遵从不编造事实的逻辑，如实说明情况并打趣，**认知与记忆完全正常**。

### 3.3 场景 3：群聊重置后“还记得”真相（self-reply 内存未清空）
针对 1.4 节中群聊重置后 bot 依然记得前因后果的现象：
1. **链路追踪**：
   - 17:52:13 用户发送 `@小盐 reset`，AstrBot 主管线与 LivingMemory 执行了会话清空；
   - 但紧随其后的钩子链中记录：
     ```text
     [17:52:13.775] hook(OnAfterMessageSentEvent) -> astrbot_plugin_livingmemory - handle_session_reset
     [17:52:13.775] [Core] [INFO]: astrbot_plugin_livingmemory - handle_session_reset 终止了事件传播。
     ```
   - **`LivingMemory` 触发重置后直接终止了事件传播（`stop_event`）**；
   - 导致 `astrbot_plugin_self_reply` 插件的重置清理逻辑（`on_after_message_sent`）**完全没有被调用到**。
2. **事实结论**：
   - `self-reply` 内存字典中的群聊滑动窗口历史（`state.session_chats`）和最近自身回复（`state.recent_bot_replies`）**压根没有被重置**；
   - 用户随后重发额度图片时，插件把重置前包括 18% 额度截图、上次回复吐槽以及刚发生的 reset 命令全量打包发给了 LLM；
   - LLM 看到上下文里上一秒才发生的事，直接现挂调侃“刚reset完第一句还是这张图”；
   - **bot 并非通过记忆库回溯了往事，而是直接读了 `self-reply` 未清空的内存缓存**。

### 3.4 场景 4：群聊消息未沉淀进长期记忆库的架构根因（两级存储差异）
针对 1.5 节中“为什么群里说了那么多话，私聊却读不到”的疑问：
1. **两级存储架构差异（工作记忆 vs 长期记忆）**：
   - **工作记忆（`conversations.db`）**：存放群聊原始消息流水。`LivingMemory` 虽通过 `handle_all_group_messages` 将群消息存入了 `conversations.db`，但**检索引擎（`recall_engine`）绝不会直接跨会话去扫全量原始聊天记录**（避免 token 爆炸与跨群隐私泄露）；
   - **长期记忆（`livingmemory.db`）**：仅存放经大模型提炼后的事实与图谱。私聊检索引擎**只查询这一层**。
2. **记忆反思任务（Reflection Engine）未触发**：
   - 消息从第一级沉淀到第二级，必须依赖反思提炼任务。该任务的触发条件为：未总结对话达到 **15 轮（30 条）**，且**仅挂载在核心主流程的 `@filter.on_llm_response` 阶段**；
   - 今天群聊大部分时间处于群友闲聊或 `self-reply` 独立接管状态，AstrBot 核心主管线回复未触发，反思任务从未被激活。
3. **`/reset` 的物理清除**：
   - 17:52:13 执行 reset 时，`ConversationStore` 物理删除了该群累积的 976 条尚未总结的聊天记录，未结消息在被提炼前直接被抹除。
4. **数据库实测**：
   - 查询 `livingmemory.db` 可见，该群**最新一条记忆（Doc ID: 77）停留在 2026-09-11 00:55:00（凌晨）**；从凌晨 1 点至下午 18 点之间，数据库内无任何新的长期记忆产生。

### 3.5 场景 5：监控群拦截后记忆插件仍保留记录的根因分析
针对 1.6 节中“`group_filter` 阻断了回复，LivingMemory 却仍有记录和归纳痕迹”的现象：
1. **历史数据存留**：
   - 在 `group_filter` 插件于 2026-09-09 部署生效之前，LivingMemory 已经如常监听并记录了该群多达 671 条历史消息（最后一条记录时间为 `2026-09-09 16:54:22`），这些数据长期存留在 `conversations.db` 的 `messages` 和 `sessions` 表中，并未被自动清理；
2. **事件阻断时机与管线旁路**：
   - `group_filter` 监听 `EventMessageType.GROUP_MESSAGE` 并以 `sys.maxsize` 优先级执行 `event.stop_event()` 终止了 `ProcessStage`；
   - 但 AstrBot 核心调度器在 `ProcessStage` 终止后，仍会生成空结果进入 `RespondStage`，派发 `OnAfterMessageSentEvent`；
   - 每次阻断均伴随 `Prepare to send - [发送者]: ` 日志并触发 `livingmemory - handle_session_reset`，插件间事件管线存在后置旁路感知；
3. **插件间数据独立性**：
   - `group_filter` 仅在 AstrBot 事件派发层阻断当次消息向下传播，并没有接口去主动清除其他持久化插件（如 LivingMemory）之前已录入的会话元数据（`sessions` 表），因此 WebUI 仍能看到该群会话卡片。

---

## 四、 实体命名的边缘影响

- 知识库与 LivingMemory 图谱中的规范实体名为繁体带姓氏的 **`user`**；
- 用户日常输入为简体、简称或别称 **`三X`**、**`叁X`**、**`叁X酱`**；
- 走正常的 LivingMemory 混合检索（向量 + 图谱 + BM25）或知识库 RAG 时，模型能根据语义向量大致关联；但当没有任何上下文召回时，LLM 仅凭字面判断，必然产生幻觉。

---

## 五、 改造与修复方案建议（供后续开发参考）

为了让 `self-reply` 插件在主动接话时也能具备“长期记忆”与“知识库检索能力”，建议按以下层级进行改造：

### 方案 1：在 `self-reply` 生成前挂接 LivingMemory 检索（推荐，收益最高）

LivingMemory 在运行期会在 AstrBot 上注册相关组件。可在 `self-reply` 的 `main.py` 中引入轻量召回：

1. **获取 LivingMemory 实例或引擎**：
   ```python
   # 通过 context.star_manager 寻找已加载的 LivingMemory 插件实例
   living_memory_star = self.context.star_manager.get_star("LivingMemory")
   if living_memory_star and living_memory_star.initializer.memory_engine:
       memory_engine = living_memory_star.initializer.memory_engine
       # 对 targets 或当前待回复的消息执行快速混合检索
       recalled_memories = await memory_engine.retrieve(query=target_text, top_k=3)
   ```
2. **将检索到的记忆文本注入 `gen_prompt`**：
   在 `DEFAULT_GENERATE_PROMPT` 中增加可选占位符 `{recalled_memories}`：
   ```text
   相关背景与过往记忆：
   {recalled_memories}
   ```
   若未找到或未安装 LivingMemory，则该占位符留空，保持平滑降级。

### 方案 2：接入 AstrBot 原生知识库（KB Manager）检索

若群聊问题涉及人设档案或专有名词：
```python
if hasattr(self.context, "kb_manager") and self.context.kb_manager:
    kb_mgr = self.context.kb_manager
    config = self.context.get_config(umo=origin)
    kb_names = config.get("kb_names", [])
    if kb_names:
        kb_context = await kb_mgr.retrieve(
            query=target_text,
            kb_names=kb_names,
            top_k_fusion=10,
            top_m_final=3
        )
        if kb_context and kb_context.get("context_text"):
            rag_text = kb_context["context_text"]
```

### 方案 3：实体别名补齐（低成本兜底）
在 `c7823f40-9101-4402-bc3b-056ca3914a00`（`人际关系基础设定`）或小盐人设补充别名关联：
> `user：又称三X、叁X、叁X酱、3X、姐X。`

---

## 六、 总结速记表

| 场景 / 环节 | 实际状态 | 日志与底层事实判定 |
|---|---|---|
| **知识库与图谱** | ✅ 数据完备 | 包含 `user` 明确关系记录 |
| **会话隔离机制** | ❌ 不存在 | `use_session_filtering: false`，同一个 persona 跨群共享记忆 |
| **群聊问“三X是谁”** | ❗ 架构脱节 | 消息未 `@`，主管道跳过，`self-reply` 插件直接裸调 LLM，无 RAG 与记忆 |
| **私聊问“妈X干嘛”** | ℹ️ 实问实答 | 走正规主管线，RAG 与记忆均生效（认姐X、认妈X），如实说明不知妈X今日行程 |
| **群聊重置后“还记得”** | ⚠️ 内存未清 | LivingMemory 终止了事件传播，`self-reply` 内部会话历史缓存未被 reset 成功 |
| **群聊消息私聊读不到** | ⌛ 两级架构 | 仅停留在 `conversations.db` 未达到 15 轮反思提炼，且在 17:52 被 reset 清除 |
| **监控群被拦仍有记录** | 📦 历史与旁路 | 2026-09-09 部署前已存 671 条历史，阻断后 RespondStage 仍触发后置钩子感知 |

---

## 七、 修复实施记录（2026-09-11）

按第五节方案 1 / 方案 2 完成代码改造，并追加了场景 3 的重置可靠性修复。代码位于 `astrbot_plugin_self_reply`，测试基线 68 passed → 120 passed。

### 7.1 根因勘误（场景 3）

原第三节 3.3 判定“LivingMemory 触发重置后 `stop_event` 导致 `self-reply` 未被调用”，与日志不符。复核 `docker logs astrbot` 17:52:13 的重置事件：

```text
[17:52:13.775] hook(OnAfterMessageSentEvent) -> astrbot_plugin_livingmemory - handle_session_reset
[17:52:13.880] hook(OnAfterMessageSentEvent) -> meme_manager - after_message_sent
[17:52:13.880] hook(OnAfterMessageSentEvent) -> astrbot - after_message_sent
```

三个钩子全部执行完毕，**没有任何一条“终止了事件传播”**，即当次事件并未被 stop；且整段日志中 `astrbot_plugin_self_reply - on_after_message_sent` 出现 0 次（同一时刻 `self-reply - on_group_message` 正常执行），说明当次运行的插件版本尚未注册该钩子（插件在 21:20:51 才被重载为含钩子的版本）。

但机制性隐患真实存在，且被 20:56 的日志证实：

```text
[20:56:51.868] hook(OnAfterMessageSentEvent) -> meme_manager - after_message_sent
[20:56:51.869] meme_manager - after_message_sent 终止了事件传播。
```

`astrbot/core/pipeline/context_utils.py:call_event_hook()` 在每个 handler 之后只要 `event.is_stopped()` 为真就 `return True` 提前结束整条链；被 `group_filter` 拦截、被 `waking_check` 判定不唤醒等场景，事件在进入 RespondStage 前已是 stopped 状态，于是**只有排在链首的钩子能收到通知**。`star_handlers_registry` 按 `-priority` 稳定排序（默认 priority=0，后加载者靠后），self-reply 作为最后加载的插件排在最末。

### 7.2 代码改动

| 文件 | 改动 |
|---|---|
| `memory_bridge.py` | 新增。经 `astrbot.core.star.star.star_registry` 定位 LivingMemory 实例，复用其 `core.memory_scope`（白名单 / 作用域）、`core.utils.format_memories_for_injection`（格式化）与 `config_manager`（top_k / 过滤开关）调用 `memory_engine.search_memories()`；用 `EventShim` 替代已结束的真实事件；可选 `record_bot_reply` 调用其 `handle_memory_reflection()` |
| `kb_bridge.py` | 新增。优先复用官方入口 `astrbot.core.tools.knowledge_base_tools.retrieve_knowledge_base`（含会话级 `kb_config`、全局 `kb_names`、空库短路），不可用时退回 `context.kb_manager.retrieve()` |
| `main.py` | 判定/生成前统一召回（`_build_recall_block` / `_collect_recall`），支持 `{recalled_memories}` 占位符与自动尾部追加；重置钩子改为 `priority=sys.maxsize`（`RESET_HOOK_PRIORITY`）；新增会话指纹自愈 `_detect_core_session_reset`（`/new` 换 id、`/reset` 历史清空）；新增在途守卫（生成期间被重置则丢弃本轮回复）；`_resolve_persona` 同时返回 persona_id 供记忆过滤 |
| `plugin_config.py` / `_conf_schema.json` | 新增 `memory` 配置组：`enable` / `top_k` / `kb_enable` / `kb_top_k` / `max_chars`(3000) / `timeout_sec`(5.0) / `inject_into_judge`(true) / `record_bot_reply`(false) |
| `runtime_state.py` | `OriginState` 新增 `core_session_fp`（(conversation_id, 历史条数) 指纹） |
| `tests/` | 新增 `test_memory_bridge.py`(18)、`test_kb_bridge.py`(10)、`test_main_reset.py`(22)，并补充 `test_plugin_config.py` 的 memory 组断言 |

### 7.3 对应的场景结论

| 场景 | 修复后 |
|---|---|
| 1. 群聊主动接话“失忆” | 判定与生成 prompt 都会带上 LivingMemory 召回 + 知识库检索块，`三X/叁X` 之类实体可由记忆与档案关联，不再裸判“这个词不存在” |
| 3. 重置后仍记得 | 钩子提到链首必被执行；另有指纹自愈与在途守卫兜底 |
| 4. 群聊内容未沉淀 | 可选开启 `memory.record_bot_reply` 后，自主回复会写回 LivingMemory 会话流，使纯插话群聊也能累积到反思阈值并提炼为长期记忆（默认关闭，需手动打开） |
| 5. 监控群历史残留 | 属历史数据与上游事件语义，不在本插件范围内；`conversations.db` 中 671 条历史如需清理另行授权操作 |
| 方案 3 实体别名 | 属知识库数据补充（`人际关系基础设定` 中补 `user：又称三X、叁X、叁X酱、姐X`），建议在 WebUI 手动维护，未做代码改动 |

### 7.4 验证

- 单元测试：容器内 `python -m pytest /AstrBot/data/plugins/astrbot_plugin_self_reply/tests -q` → **120 passed**（含降级、超时、截断、门禁、指纹、在途守卫等用例）。
- 真实接口联调（独立进程，不触网）：用真实 `astrbot_plugin_livingmemory` 模块与真实 `astrbot_plugin_livingmemory_config.json` 校验 —— 模块定位成功、`is_event_memory_allowed(cm, shim)=True`、`resolve_memory_scope(cm, shim)=None`（`use_session_filtering=false`）、`recall_engine.top_k=3` 读取正常、`format_memories_for_injection` 复用成功。
- 线上验证（重载插件后）：见 7.5。

### 7.5 线上验证清单（重载插件后执行）

1. 群 400000000 发不带 `@` 的记忆相关消息 → 日志出现 `self-reply | memory | 召回 N 条记忆 ...`，回复能认出“叁X/姐X”。
2. `@小盐 reset` 后紧接旧话题 → 日志出现 `self-reply | 会话已重置，同步清空插话历史缓存`，回复不再复述重置前细节。
3. `docker logs astrbot | grep OnAfterMessageSentEvent` → `astrbot_plugin_self_reply - on_after_message_sent` 现在排在链首。

