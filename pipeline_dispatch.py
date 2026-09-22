"""把自主回复“交回” AstrBot 主管道的派发层。

为什么需要它
------------
插件判定发生在 handler 返回之后的防抖任务里，此时原始事件早已跑完自己那条
管道，`yield event.request_llm(...)` 这种“交回主管道”的写法不再可用
（yield 语义只在 handler 自己的 async generator 内有效，见
``astrbot/core/pipeline/context_utils.py:call_handler``）。

因此这里改用 AstrBot 为插件准备的事件注入入口（与官方
``StarTools.create_event`` 等价，见 ``astrbot/core/star/star_tools.py``）：
构造一条“bot 发给自己所在群”的唤醒事件并提交进事件队列，由
``PipelineScheduler`` 跑完整管道。于是：

* ``on_llm_request``：长期记忆召回注入、核心知识库注入照常发生；
* ``on_llm_response``：长期记忆的反思/总结自然触发（无需再直接调
  LivingMemory 的内部函数）；
* ``on_decorating_result`` / ``RespondStage``：结果由平台适配器真正发送；
* ``after_message_sent``：发送后钩子照常触发。

载荷登记表
----------
``StarTools.create_event`` 不支持给事件挂 extras，所以这里用自己构造的事件，
并把这次派发需要的上下文（prompt、人格、引用目标等）按 ``message_id`` 登记，
由本插件的 ``on_llm_request`` / ``on_decorating_result`` / ``after_message_sent``
钩子按同一个 ``message_id`` 找回。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from astrbot.api import logger

MARKER_PREFIX = "selfreply-pipe-"

#: 载荷在登记表中的存活时间（秒）。超时未走完管道即视为失败并清理。
JOB_TTL_SEC = 300.0

#: 改写 req 的钩子必须早于长期记忆插件的召回钩子（默认优先级 0），
#: 否则后者的注入会被本插件的 prompt 覆盖。
PIPELINE_HOOK_PRIORITY = 100

#: pipeline 模式的导演指令模板（chat：闲聊接梗）
DEFAULT_CHAT_NOTE_TEMPLATE = (
    "[旁白：以下是群聊最近的滚动记录（按时间序，最新在下；可能包含你自己刚说过的话）：\n"
    "{recent_context}\n"
    "你（{persona_name}）决定主动接一句。\n"
    "目标消息 #msg{target_id} {target_nick}：{target_text}\n"
    "要求：用一两句口语接住，别复述旁白、别解释你在做什么。\n"
    "{quote_rule}{anti_repeat_instr}]"
)

#: pipeline 模式的导演指令模板（task：派活/求答，可用工具）
DEFAULT_TASK_NOTE_TEMPLATE = (
    "[旁白：群聊近况（最近若干条滚动记录）：\n"
    "{recent_context}\n"
    "群友 #msg{target_id} {target_nick} 向你派了活/提了问题：{target_text}\n"
    "你决定接住这件事。需要查证就调用你可用的工具，查完再回答；不需要就直接答。\n"
    "要求：结论优先、控制在 3 句以内、不要复述旁白。\n"
    "{quote_rule}]"
)


#: chat 类插话的工具策略（P0b）：none = 一个工具都不给；
#: readonly = 只保留 DEFAULT_CHAT_TOOLS / dispatch.chat_tools 白名单内的；
#: all = 不收窄（回退用，等同 task）。
CHAT_TOOL_MODES = ("none", "readonly", "all")

#: readonly 模式的默认白名单。只放「本地、只读、与接梗直接相关」的工具：
#: search_memes 是纯检索（真正的发图由 meme_manager 的 on_decorating_result
#: 兜），而且 meme_manager 注入取图提示词时会检查 search_memes 是否在工具集里，
#: 留着它才能让提示词与工具一致。其余全量工具（联网搜索、读站、戳一戳、定时
#: 任务、主动私信、长期记忆召回）要么有副作用、要么是 task 类的事，都不默认给。
DEFAULT_CHAT_TOOLS = ("search_memes",)

#: 空回复兜底的生效范围（P1b）：task = 只有派活轮；always = 所有插话轮；
#: off = 关闭（等同旧行为：这一轮静默不发言）。
FALLBACK_ON_MODES = ("task", "always", "off")

DEFAULT_FALLBACK_ON = "task"

#: 默认兜底文案：派活轮最后没有可发送内容时顶上，免得群友等了个空。
DEFAULT_FALLBACK_TEXT = "呜…猫猫这次没查成，再戳我一下喵"


def normalize_chat_tool_mode(raw) -> str:
    """容错解析 chat 工具策略；非法值一律回退 none（最保守）。"""
    mode = str(raw or "").strip().lower()
    return mode if mode in CHAT_TOOL_MODES else "none"


def normalize_fallback_on(raw) -> str:
    """容错解析空回复兜底范围；非法值一律回退 task（默认档）。"""
    mode = str(raw or "").strip().lower()
    return mode if mode in FALLBACK_ON_MODES else DEFAULT_FALLBACK_ON


def should_inject_fallback(*, kind: str, mode: str) -> bool:
    """空回复兜底是否对这一轮生效。

    **调用点必须限定在「结果链非空但没有任何可发送文本」的分支**：工具调用
    轮的「结果链为空」是正常过程（而且主管道在结果链为空时根本不会跑装饰
    钩子——`result_decorate/stage.py:131` 直接 return——所以能看到的空链只
    可能是别的钩子把结果清掉了），在那里注入文案会被当成工具轮的
    early-return 结果发出去（规划 §4.4 修正）。
    """
    normalized = normalize_fallback_on(mode)
    if normalized == "off":
        return False
    if normalized == "always":
        return True
    return kind == "task"


def kept_tool_names(
    *,
    kind: str,
    mode: str,
    allowlist,
    current: list[str],
) -> list[str]:
    """算出本轮插话应当保留的工具名（保持 current 的原始顺序）。

    规则（规划 §10 P0b）：

    * kind == "task"：全量保留——派活要真本事，联网、读站都靠它；
    * mode == "all"：不做任何收窄（回退开关）；
    * mode == "readonly"：只保留白名单命中的工具；
    * 其余（none 或非法值）：一个都不留。

    未知 kind（判定失败时的兜底值）按 chat 处理：保守收窄，宁少给不多给。
    """
    normalized = normalize_chat_tool_mode(mode)
    if kind == "task" or normalized == "all":
        return list(current)
    if normalized == "readonly":
        allowed = {
            str(name).strip() for name in (allowlist or []) if str(name).strip()
        }
        return [name for name in current if name in allowed]
    return []


def truncate_recent_context(
    text: str, max_lines: int = 12, max_chars: int = 1200
) -> str:
    """把滚动群聊记录压成近况块：先取最后 max_lines 行，再按 max_chars 掐尾。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if max_lines > 0:
        lines = lines[-max_lines:]
    out = "\n".join(lines)
    if max_chars > 0 and len(out) > max_chars:
        out = out[-max_chars:]
    return out


def build_director_note(
    template: str,
    *,
    recent_context: str,
    persona_name: str,
    targets: list[dict] | None = None,
    quote_rule: str = "",
    anti_repeat_instr: str = "",
) -> str:
    """渲染 pipeline 模式的导演指令。

    targets 是判定阶段选中的目标消息（每条至少含 norm_id / nick / text）；
    当前策略一轮至多 1 个目标，模板只暴露第一条。渲染失败宁可回退标准模板
    也不要抛异常——它跑在防抖任务里，抛出会静默丢掉这次插话。
    """
    first = (targets or [{}])[0] if targets else {}
    fields = {
        "recent_context": recent_context,
        "persona_name": persona_name or "你",
        "target_id": first.get("norm_id", ""),
        "target_nick": first.get("nick", ""),
        "target_text": first.get("text", ""),
        "quote_rule": quote_rule,
        "anti_repeat_instr": anti_repeat_instr,
    }
    tmpl = template or DEFAULT_CHAT_NOTE_TEMPLATE
    try:
        return tmpl.format(**fields)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"self-reply | 导演指令模板渲染失败，回退标准拼接: {e}")
        return DEFAULT_CHAT_NOTE_TEMPLATE.format(**fields)


def new_message_id() -> str:
    """生成一个可识别的合成事件 message_id。"""
    return f"{MARKER_PREFIX}{uuid4().hex}"


@dataclass
class PipelineJob:
    """一次“交回主管道”的生成任务载荷。"""

    message_id: str
    origin: str
    #: 交给主流程的导演指令（旁白式，由 build_director_note 渲染）
    director_note: str
    #: 插话类型：chat = 闲聊接梗 / task = 派活求答 / proactive = 主动开口（P3）
    kind: str = "chat"
    #: 【P0a 起停用】人格改由核心 _ensure_persona_and_skills 原生注入，
    #: 钩子不再把它写进 req.system_prompt；字段保留以兼容旧调用方。
    system_prompt: str = ""
    #: 用于 KB / 记忆召回的查询文本（合成事件的 message_str）
    query_text: str = ""
    image_urls: list[str] = field(default_factory=list)
    #: 本次允许引用的消息 id（防幻觉锚定）
    allowed_msg_ids: set[str] = field(default_factory=set)
    #: 判定阶段选中的目标消息 id（强制引用兜底用）
    reply_targets: list[str] = field(default_factory=list)
    should_quote: bool = False
    quote_policy: str = "judge"
    #: 指定生成用的 Provider（空则用会话默认 Provider）
    provider_id: str = ""
    self_id: str = ""
    group_id: str = ""
    platform: str = ""
    nickname: str = ""
    created_at: float = field(default_factory=time.time)
    #: 装饰阶段算出的最终文本（发送成功后写回历史用）
    final_text: str = ""
    dropped: bool = False
    dispatched: bool = False


class PipelineDispatcher:
    """合成事件的构造/注入，以及载荷登记表。"""

    def __init__(self, context) -> None:
        self.context = context
        self._jobs: dict[str, PipelineJob] = {}

    # ---------------- 载荷登记 ----------------

    def register(self, job: PipelineJob) -> None:
        self._gc()
        self._jobs[job.message_id] = job

    def get(self, message_id: str) -> PipelineJob | None:
        return self._jobs.get(message_id)

    def find(self, event) -> PipelineJob | None:
        """按事件上的 message_id 找回载荷；非本插件合成的事件返回 None。"""
        mid = _event_message_id(event)
        if not mid or not mid.startswith(MARKER_PREFIX):
            return None
        return self._jobs.get(mid)

    def take(self, event) -> PipelineJob | None:
        job = self.find(event)
        if job is None:
            return None
        self._jobs.pop(job.message_id, None)
        return job

    def discard(self, job: PipelineJob | None) -> None:
        if job is not None:
            self._jobs.pop(job.message_id, None)

    def _gc(self) -> None:
        if len(self._jobs) < 64:
            return
        now = time.time()
        stale = [
            mid for mid, job in self._jobs.items() if now - job.created_at > JOB_TTL_SEC
        ]
        for mid in stale:
            self._jobs.pop(mid, None)

    # ---------------- 注入 ----------------

    async def dispatch(self, job: PipelineJob) -> bool:
        """把任务作为一条唤醒事件提交进主流程；失败返回 False（调用方回退直连）。"""
        if job.platform and job.platform != "aiocqhttp":
            logger.warning(
                f"self-reply | pipeline dispatch 仅支持 aiocqhttp，"
                f"当前 platform={job.platform}，回退直连"
            )
            return False
        if not job.self_id or not job.group_id:
            logger.warning(
                "self-reply | pipeline dispatch 缺少 self_id/group_id，回退直连"
            )
            return False

        try:
            from astrbot.api.message_components import At, Plain
            from astrbot.api.platform import MessageMember, MessageType
            from astrbot.core.platform.astrbot_message import AstrBotMessage
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (  # noqa: E501
                AiocqhttpMessageEvent,
            )
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (  # noqa: E501
                AiocqhttpAdapter,
            )
        except Exception as e:  # pragma: no cover - 平台模块缺失时的兜底
            logger.warning(f"self-reply | 无法导入 aiocqhttp 事件类，回退直连: {e}")
            return False

        adapter = None
        try:
            platforms = self.context.platform_manager.get_insts()
        except Exception as e:
            logger.warning(f"self-reply | 获取平台实例失败，回退直连: {e}")
            return False
        for inst in platforms:
            if isinstance(inst, AiocqhttpAdapter):
                adapter = inst
                break
        if adapter is None:
            logger.warning("self-reply | 未找到 aiocqhttp 适配器，回退直连")
            return False

        bot_qq = str(job.self_id)
        group_id = str(job.group_id)
        nickname = job.nickname or bot_qq
        query_text = job.query_text or "..."

        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE
        abm.self_id = bot_qq
        abm.session_id = group_id
        abm.message_id = job.message_id
        abm.sender = MessageMember(user_id=bot_qq, nickname=nickname)
        # At 段让 WakingCheckStage 走标准唤醒分支（不依赖 wake_prefix 配置）
        abm.message = [At(qq=bot_qq, name=nickname), Plain(text=query_text)]
        abm.message_str = query_text
        abm.raw_message = None
        abm.group_id = group_id

        try:
            event = AiocqhttpMessageEvent(
                message_str=query_text,
                message_obj=abm,
                platform_meta=adapter.metadata,
                session_id=group_id,
                bot=adapter.bot,
            )
        except Exception as e:
            logger.error(f"self-reply | 构造合成事件失败，回退直连: {e}")
            return False

        if job.provider_id:
            event.set_extra("selected_provider", job.provider_id)
        event.is_wake = True
        event.is_at_or_wake_command = True

        self.register(job)
        try:
            adapter.commit_event(event)
        except Exception as e:
            self.discard(job)
            logger.error(f"self-reply | 提交合成事件失败，回退直连: {e}")
            return False

        job.dispatched = True
        logger.info(
            f"self-reply | pipeline dispatch origin={job.origin} "
            f"msg_id={job.message_id} provider={job.provider_id or 'session-default'}"
        )
        return True


def _event_message_id(event) -> str:
    obj = getattr(event, "message_obj", None)
    return str(getattr(obj, "message_id", "") or "")
