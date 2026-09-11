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


def new_message_id() -> str:
    """生成一个可识别的合成事件 message_id。"""
    return f"{MARKER_PREFIX}{uuid4().hex}"


@dataclass
class PipelineJob:
    """一次“交回主管道”的生成任务载荷。"""

    message_id: str
    origin: str
    #: 交给主流程的 user prompt（插件拼好的插话提示词）
    prompt: str
    #: 追加到主流程 system prompt 前的人格设定
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
