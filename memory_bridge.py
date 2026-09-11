"""LivingMemory 桥接层：让"自主插话"链路也能用上长期记忆。

设计约束（与 issue 文档《MEMORY_AND_RAG_INTEGRATION_ISSUE.md》第五节方案 1 对应）：

- 只做**只读检索**；可选写回（``record_bot_reply``）由调用方显式开启；
- 所有对 LivingMemory 内部结构的访问都走 ``getattr`` + ``try/except``，
  少了任何一环都平滑降级（返回空串），绝不把异常抛给主流程；
- 召回门禁（白名单 / 会话开关 / 人格 / 会话作用域）优先复用 LivingMemory
  自己的实现，避免绕过上游配置；取不到实现时使用行为等价的本地兜底。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.platform import MessageType

# 单条记忆进入 prompt 前的字符上限（防止一条长记忆挤掉群聊上下文）
MAX_ITEM_CHARS = 400
# 召回块总字符上限的兜底值
DEFAULT_MAX_CHARS = 3000
# 截断标记
TRUNCATED_SUFFIX = "…（已截断）"

_MISS_LOG_LIMIT = 3


def _split_list(raw: Any) -> list[str]:
    """把 LiveMemory 配置里的 "a,b\\nc" 形式拆成列表。"""
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").replace(",", "\n").replace(";", "\n").split("\n")
    out: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def _parse_identity_aliases(raw: Any) -> dict[str, str]:
    """解析 ``source=canonical`` 形式的人格/身份别名（用于白名单兜底）。"""
    aliases: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        source, separator, target = line.partition("=")
        if not separator:
            continue
        source = source.strip()
        target = target.strip()
        if source and target:
            aliases[source.casefold()] = target
    return aliases


def truncate_text(text: str, max_chars: int) -> str:
    """按字符数截断文本，超出时追加截断标记。"""
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(max_chars - len(TRUNCATED_SUFFIX), 1)
    return text[:keep] + TRUNCATED_SUFFIX


class EventShim:
    """LivingMemory 工具函数需要的最小只读事件接口。

    LivingMemory 的 ``memory_scope`` / ``conversation_manager`` 只通过
    以下几个入口读取事件信息，因此可以在防抖任务里（真实事件已结束）
    用一个轻量对象代替 ``AstrMessageEvent``。
    """

    def __init__(
        self,
        *,
        origin: str,
        sender_id: str = "",
        sender_name: str = "",
        group_id: str = "",
        platform: str = "",
        platform_id: str = "",
        self_id: str = "",
        bot_name: str = "",
        message_type=MessageType.GROUP_MESSAGE,
    ) -> None:
        self.unified_msg_origin = origin or ""
        self._sender_id = str(sender_id or "")
        self._sender_name = str(sender_name or "")
        self._group_id = str(group_id or "")
        self._platform = str(platform or "")
        self._platform_id = str(platform_id or "")
        self._self_id = str(self_id or "")
        self._message_type = message_type
        # LivingMemory 的 _resolve_bot_identity 会读取这些属性名
        self.bot_name = str(bot_name or "")
        self.bot_nickname = self.bot_name
        self.self_name = self.bot_name
        self._extras: dict[str, Any] = {}

    # --- LivingMemory / AstrBot 共用的读取入口 ---
    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self._group_id

    def get_platform_name(self) -> str:
        return self._platform

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_message_type(self):
        return self._message_type

    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)


class MemoryBridge:
    """定位 LivingMemory 插件实例并提供记忆召回 / 写回。"""

    def __init__(self, context) -> None:
        self.context = context
        self._cached: tuple[Any, Any, Any] | None = None  # (instance, engine, config_manager)
        self._miss_logs = 0

    # ---------------- 发现与缓存 ----------------

    @staticmethod
    def _find_metadata():
        """在 AstrBot 插件注册表里找到 LivingMemory 的元数据。"""
        try:
            from astrbot.core.star.star import star_registry
        except Exception:
            return None
        for md in list(star_registry or []):
            name = str(getattr(md, "name", "") or "")
            module_path = str(getattr(md, "module_path", "") or "")
            if (
                name == "LivingMemory"
                or "livingmemory" in name.lower()
                or "livingmemory" in module_path.lower()
            ):
                return md
        return None

    def _resolve(self) -> tuple[Any, Any, Any] | None:
        """返回 (plugin_instance, memory_engine, config_manager)；不可用时返回 None。

        只对"命中"做缓存；未初始化（engine 为空）时下次调用会重新探测。
        """
        if self._cached is not None:
            instance, engine, config_manager = self._cached
            try:
                if getattr(instance, "_terminating", False):
                    self._cached = None
                elif getattr(getattr(instance, "initializer", None), "memory_engine", None) is engine:
                    return self._cached
                else:
                    self._cached = None
            except Exception:
                self._cached = None

        md = self._find_metadata()
        if md is None:
            self._log_miss("未找到 LivingMemory 插件，跳过记忆召回")
            return None

        instance = getattr(md, "star_cls", None)
        if instance is None:
            self._log_miss("LivingMemory 插件实例不可用，跳过记忆召回")
            return None

        initializer = getattr(instance, "initializer", None)
        engine = getattr(initializer, "memory_engine", None)
        config_manager = getattr(instance, "config_manager", None)
        if engine is None or config_manager is None:
            self._log_miss("LivingMemory 尚未初始化完成，跳过记忆召回")
            return None

        self._cached = (instance, engine, config_manager)
        self._miss_logs = 0
        return self._cached

    def _log_miss(self, message: str) -> None:
        """限流输出降级日志，避免每个会话都刷屏。"""
        if self._miss_logs < _MISS_LOG_LIMIT:
            self._miss_logs += 1
            logger.debug(f"self-reply | memory | {message}")

    def invalidate(self) -> None:
        """插件重载等场景下清掉缓存。"""
        self._cached = None
        self._miss_logs = 0

    @staticmethod
    def _module_helpers(instance) -> Any:
        """定位 LivingMemory 的 ``core`` 子包（用于复用其门禁/格式化实现）。

        AstrBot 以 ``data.plugins.<dir>.main`` 的形式导入插件，
        因此 ``type(instance).__module__`` 的 ``__package__`` 就是插件包名。
        """
        import sys

        for module_name in (
            getattr(instance, "__module__", ""),
            type(instance).__module__,
        ):
            if not module_name:
                continue
            package_name = getattr(sys.modules.get(module_name), "__package__", None)
            if not package_name:
                continue
            package = sys.modules.get(package_name)
            core = getattr(package, "core", None) or sys.modules.get(
                f"{package_name}.core"
            )
            if core is not None:
                return core
        return None

    # ---------------- 门禁：白名单 / 会话开关 ----------------

    @staticmethod
    def _local_allowed(config_manager, shim: EventShim) -> bool:
        """``is_event_memory_allowed`` 的行为等价兜底实现。"""
        if not _get_cfg(config_manager, "access_control.whitelist_enabled", False):
            return True
        allowed = {
            value.casefold()
            for value in _split_list(
                _get_cfg(config_manager, "access_control.allowed_ids", "")
            )
        }
        if not allowed:
            return False
        sender_id = shim.get_sender_id()
        platform = shim.get_platform_name()
        identity = sender_id or shim.get_sender_name() or shim.unified_msg_origin
        aliases = _parse_identity_aliases(
            _get_cfg(config_manager, "access_control.identity_aliases", "")
        )
        for candidate in (
            f"{platform.casefold()}:{sender_id}" if platform and sender_id else "",
            sender_id,
            shim.get_sender_name(),
        ):
            if candidate and candidate.casefold() in aliases:
                identity = aliases[candidate.casefold()]
                break
        candidates = {
            sender_id,
            identity,
            shim.unified_msg_origin,
            shim.get_group_id(),
            f"{platform}:{sender_id}" if platform and sender_id else "",
        }
        return any(c and c.casefold() in allowed for c in candidates)

    def _is_allowed(self, instance, config_manager, shim: EventShim) -> bool:
        core = self._module_helpers(instance)
        scope_mod = getattr(core, "memory_scope", None)
        checker = getattr(scope_mod, "is_event_memory_allowed", None)
        if callable(checker):
            try:
                return bool(checker(config_manager, shim))
            except Exception as e:
                logger.debug(f"self-reply | memory | 白名单检查失败，回退本地实现: {e}")
        return self._local_allowed(config_manager, shim)

    async def _session_gate(self, instance, shim: EventShim) -> bool:
        """尊重 LivingMemory 的会话总开关 / 单会话禁用配置（best-effort）。"""
        core = self._module_helpers(instance)
        capture = getattr(core, "passive_group_capture", None)
        for name in ("is_session_enabled", "is_plugin_enabled_for_session"):
            checker = getattr(capture, name, None)
            if not callable(checker):
                continue
            try:
                if not await checker(shim.unified_msg_origin):
                    logger.debug(
                        f"self-reply | memory | 会话 {shim.unified_msg_origin} 已关闭记忆，跳过召回"
                    )
                    return False
            except Exception as e:
                logger.debug(f"self-reply | memory | 会话开关检查失败({name}): {e}")
        return True

    # ---------------- 作用域 ----------------

    @staticmethod
    def _local_scope(config_manager, shim: EventShim) -> str | None:
        """``resolve_memory_scope`` 的行为等价兜底实现。"""
        session_id = shim.unified_msg_origin
        isolated = _split_list(
            _get_cfg(config_manager, "filtering_settings.isolated_sessions", "")
        )
        if session_id in isolated:
            return session_id

        mode = str(
            _get_cfg(config_manager, "filtering_settings.memory_scope_mode", "legacy")
        ).casefold()
        if mode == "session":
            return session_id
        if mode == "global":
            return "livingmemory:global"
        if mode == "user":
            platform = shim.get_platform_name().casefold() or "unknown"
            identity = shim.get_sender_id() or shim.get_sender_name() or "unknown"
            return f"livingmemory:user:{platform}:{identity}"

        use_session = bool(
            _get_cfg(config_manager, "filtering_settings.use_session_filtering", True)
        )
        if use_session:
            return session_id
        return "livingmemory:global" if isolated else None

    def _resolve_scope(self, instance, config_manager, shim: EventShim) -> str | None:
        core = self._module_helpers(instance)
        scope_mod = getattr(core, "memory_scope", None)
        resolver = getattr(scope_mod, "resolve_memory_scope", None)
        if callable(resolver):
            try:
                return resolver(config_manager, shim)
            except Exception as e:
                logger.debug(f"self-reply | memory | 作用域解析失败，回退本地实现: {e}")
        return self._local_scope(config_manager, shim)

    # ---------------- 召回 ----------------

    async def recall(
        self,
        *,
        shim: EventShim,
        query: str,
        persona_id: str | None = None,
        top_k: int = 0,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> str:
        """检索长期记忆并返回可直接拼进 prompt 的文本块（失败返回空串）。"""
        query = (query or "").strip()
        if not query:
            return ""

        resolved = self._resolve()
        if resolved is None:
            return ""
        instance, engine, config_manager = resolved

        if not self._is_allowed(instance, config_manager, shim):
            logger.debug(
                f"self-reply | memory | 会话 {shim.unified_msg_origin} 不在记忆白名单，跳过召回"
            )
            return ""
        if not await self._session_gate(instance, shim):
            return ""

        k = top_k if top_k > 0 else _to_int(
            _get_cfg(config_manager, "recall_engine.top_k", 5), 5
        )
        if k <= 0:
            return ""

        use_persona = bool(
            _get_cfg(config_manager, "filtering_settings.use_persona_filtering", True)
        )
        scope = self._resolve_scope(instance, config_manager, shim)

        try:
            results = await engine.search_memories(
                query=query,
                k=k,
                session_id=scope,
                persona_id=(persona_id or None) if use_persona else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"self-reply | memory | 记忆检索失败: {e}")
            return ""

        if not results:
            logger.debug(f"self-reply | memory | 未检索到相关记忆 query='{query[:60]}'")
            return ""

        for idx, mem in enumerate(results, 1):
            logger.debug(
                f"self-reply | memory | 命中 #{idx} score="
                f"{float(getattr(mem, 'final_score', 0.0) or 0.0):.3f} "
                f"{str(getattr(mem, 'content', '') or '')[:60]}"
            )

        block = self._format(instance, results)
        if not block:
            return ""
        block = truncate_text(block, max_chars)
        logger.info(
            f"self-reply | memory | 召回 {len(results)} 条记忆 "
            f"scope={scope or 'global'} persona={persona_id or 'any'} chars={len(block)}"
        )
        return block

    @staticmethod
    def _local_format(memories: list[dict]) -> str:
        """格式化实现的兜底版本（LivingMemory 的 formatter 不可用时使用）。"""
        lines = ["【相关长期记忆】", "以下是之前对话沉淀下来的记忆，供你参考：", ""]
        for idx, mem in enumerate(memories, 1):
            metadata = mem.get("metadata") or {}
            importance = float(metadata.get("importance", 0.5) or 0.5)
            timestamp = mem.get("timestamp") or metadata.get("create_time") or ""
            time_part = f"，记录时间 {timestamp}" if timestamp else ""
            lines.append(f"记忆 #{idx}（重要度 {importance:.2f}{time_part}）")
            lines.append(str(mem.get("content") or ""))
            lines.append("")
        return "\n".join(lines).strip()

    def _format(self, instance, results) -> str:
        memories: list[dict] = []
        for mem in results:
            metadata = getattr(mem, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
            content = str(getattr(mem, "content", "") or "")
            memories.append(
                {
                    "id": getattr(mem, "doc_id", None),
                    "content": truncate_text(content.strip(), MAX_ITEM_CHARS),
                    "score": getattr(mem, "final_score", 0.0) or 0.0,
                    "metadata": metadata,
                    "timestamp": metadata.get("create_time"),
                }
            )

        core = self._module_helpers(instance)
        utils_mod = getattr(core, "utils", None)
        formatter = getattr(utils_mod, "format_memories_for_injection", None)
        if callable(formatter):
            try:
                text = formatter(memories)
                if text:
                    return text
            except Exception as e:
                logger.debug(f"self-reply | memory | 复用 LivingMemory 格式化失败: {e}")
        return self._local_format(memories)

    # ---------------- 可选写回（对应场景 4） ----------------

    async def record_bot_reply(
        self, *, shim: EventShim, text: str, timeout: float = 5.0
    ) -> bool:
        """把自主回复写回 LivingMemory，使其反思/总结任务能覆盖纯插话群聊。

        只有 LivingMemory 已初始化、通过白名单与会话开关时才写；
        任何异常都只记日志，不影响已发出的回复。
        """
        text = (text or "").strip()
        if not text:
            return False

        resolved = self._resolve()
        if resolved is None:
            return False
        instance, _engine, config_manager = resolved

        if not self._is_allowed(instance, config_manager, shim):
            return False
        if not await self._session_gate(instance, shim):
            return False

        handler = getattr(
            getattr(instance, "event_handler", None), "handle_memory_reflection", None
        )
        if not callable(handler):
            logger.debug("self-reply | memory | LivingMemory 反思入口不可用，跳过写回")
            return False

        try:
            from astrbot.api.provider import LLMResponse
        except Exception as e:
            logger.debug(f"self-reply | memory | 无法构造 LLMResponse，跳过写回: {e}")
            return False

        try:
            await asyncio.wait_for(
                handler(shim, LLMResponse(role="assistant", completion_text=text)),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("self-reply | memory | 写回记忆超时")
            return False
        except Exception as e:
            logger.warning(f"self-reply | memory | 写回记忆失败: {e}")
            return False
        return True


def _get_cfg(config_manager, key: str, default: Any = None) -> Any:
    """从 LivingMemory 的 ConfigManager（或普通 dict）读取点号分隔配置。"""
    getter = getattr(config_manager, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
        except Exception:
            return default
    if isinstance(config_manager, dict):
        current: Any = config_manager
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
