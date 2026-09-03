# astrbot_plugin_self_reply

自主回复插件：bot 在群聊中无需被 @，按防抖节流自主判断是否发言；普通聊天直接发言，仅在回复特别问句时使用引用模式，并防止重复刷屏。

## 开发由来与参考

本插件由 **Soul-Charge** 发起，使用 AI 辅助从零开发，用于替代/补充 AstrBot 的主动回复能力，定位为单职责的“自主回复”插件。

开发过程中参考了本地已有的 `astrbot_plugin_astrbot_enhance_mode`（作者：Axi404 / 阿汐，仓库：https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode），主要借鉴其交互约定与设计思路：

- `<quote id="..."/>`、`<mention id="..."/>`、`<refuse/>` 等控制标签约定
- 带 `#msgID` 的结构化群聊历史格式
- 主动回复/判定的分层思想、防重复与冷却机制

本插件的代码为实现“自主回复”这一职责而独立编写/裁剪，并非 `astrbot_plugin_astrbot_enhance_mode` 的复制品。

> 说明：早期规划文档中 `author` 曾写成“阿汐”，这是参考 enhance_mode 时误带入的署名；本插件实际作者为 **Soul-Charge**。

## 用途

在群聊场景下，为 bot 提供一套克制的"主动搭话"能力：

1. 所有群消息被记录成带 `#msgID` 锚点的结构化历史（昵称/QQ号/时间/角色/引用/图片标记）。
2. 消息静默一段时间（防抖）后，用轻量 judge 模型以人格视角判定"要不要回、回哪条"，输出结构化 JSON。
3. 判定为回复时，用生成模型（可带近期群图）产出自然回复；普通消息直接发言，只有目标消息是特别问句时才以 `<quote>` 锚定。
4. 发送后记账（已回复注册表、最近回复缓存、冷却时间戳），保证不重复回复、不刷屏。

## 功能列表

- **防抖触发**：普通消息 5s / 快速通道 1s，同一会话内新消息会重置计时。
- **唤醒消息让行**：@bot、引用 bot 消息、唤醒前缀等会触发主流水线的消息不参与自主回复判定（仍会记入历史上下文），避免同一句话被两条链路各答一次。
- **同内容折叠**：平台可能把一条消息双投递成"纯文本 + @bot"两个事件，插件按（发送者, 文本）指纹在 10 秒窗口内折叠，唤醒副本会撤回已入队的待判定消息。
- **快速通道**：含 `?/？`、引用消息、命中关键词的消息更快触发判定（仅对非唤醒消息生效）。
- **冷却机制**：同一会话两次主动回复之间的最小间隔（默认 15s），冷却期放弃判定。
- **结构化判定**：judge 输出 `{"decision":"reply|skip","target_ids":[...],"reason":"..."}`，兼容裸 `REPLY/SKIP` 文本。
- **按需引用**：默认不引用、直接在群里说话；当目标消息是问句时，`quote_policy=judge` 会自动补引用标签，模型自己写的引用也会校验 msg_id 是否存在于近期历史，防幻觉。
- **防重复**：生成前注入 bot 近期回复；生成后与最近 N 条做相似度终检，重复则重试或放弃。
- **图片上下文**：近期群图注册表，生成时最多附带 N 张原图（视觉模型）。
- **容量治理**：历史 50 条/会话、已回复注册表 200 条、会话状态 LRU 500 个，插件卸载时清理防抖任务。

## 配置说明

| 组 | 字段 | 默认 | 说明 |
|---|---|---|---|
| enable | 总开关 | false | 启用自主回复 |
| trigger | debounce_normal_seconds | 5.0 | 普通防抖秒数 |
| | debounce_quick_seconds | 1.0 | 快速通道防抖秒数 |
| | min_reply_interval_seconds | 15.0 | 最小回复间隔 |
| | quick_trigger_keywords | [] | 关键词快速通道 |
| | max_pending_before_judge | 20 | 待判定消息上限 |
| judge | provider_id | deepseek/deepseek-v4-flash | 判定模型 |
| | history_messages | 30 | 判定附带历史行数 |
| | prompt_template | 内置 | 判定提示词模板 |
| generate | provider_id | deepseek-vision/deepseek-v4-flash-vision-exp | 生成模型（可视觉） |
| | attach_recent_images | 3 | 附带近期图片数，0 关闭 |
| | quote_policy | judge | judge / model / none；judge 下仅对问句自动引用 |
| | include_role_tag | true | 历史含角色标签 |
| | include_sender_id | true | 历史含发送者 ID |
| anti_repeat | enable | true | 防重复开关 |
| | similarity_threshold | 0.85 | 相似度阈值 |
| | max_retries | 1 | 重复重试次数 |
| | compare_window | 10 | 比较窗口条数 |
| history | max_messages | 50 | 每会话历史上限 |
| whitelist | allowed_origins | [] | unified_msg_origin 或群号白名单，留空全群生效 |
| global_settings | max_origins | 500 | 会话状态 LRU 上限 |
| | judge_timeout_sec | 45.0 | 判定超时 |
| | generate_timeout_sec | 60.0 | 生成超时 |

## 前置条件

- `astrbot_plugin_astrbot_enhance_mode` 已在 WebUI 禁用（避免双判定）。
- AstrBot 内置 `active_reply.enable` / `group_icl_enable` 关闭。
- 判定/生成所填 provider_id 已在 AstrBot 中配置。
- Python ≥ 3.10，AstrBot v4.24.5。

## 使用示例

1. WebUI 插件页启用本插件，确认总开关 `enable` 打开。
2. 在群里正常聊天，静默约 5 秒后 judge 判定；被 @ 或提问时约 1 秒。
3. bot 默认直接在群里发言；回复特别问句时才会引用对应消息；连续聊天不会每条必回（冷却 + 防重复）。
4. 想只在特定群启用时，把群号或 `platform_name:group_id` 形式的 unified_msg_origin 填入 `whitelist.allowed_origins`。

## 测试

插件本身零第三方依赖（`requirements.txt` 为空）。单元测试放在 `tests/`：

```bash
# AstrBot 容器内（推荐，含真实 astrbot 包）
python -m pytest /AstrBot/data/plugins/astrbot_plugin_self_reply/tests -q

# 宿主机（无 astrbot 包时 conftest 自动打桩）
python -m pytest tests -q
```

覆盖范围：配置解析容错、标签/引用/拒答处理、图片 URL/base64 解析、回复记账（冷却/已回复/相似度）、判定输出解析（JSON/容错）。
