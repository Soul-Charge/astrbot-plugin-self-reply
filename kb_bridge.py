"""AstrBot 知识库（RAG）桥接：让自主插话也能检索知识库。

对应 issue 文档第五节方案 2：

- 优先复用 AstrBot 官方入口 ``astrbot.core.tools.knowledge_base_tools.retrieve_knowledge_base``，
  该入口自带会话级 ``kb_config`` 覆盖、全局 ``kb_names`` 解析、空库短路与日志；
- 官方入口不可用（版本差异）时退回本地实现，直接调 ``context.kb_manager.retrieve``；
- 任何失败都返回空串，由调用方决定是否拼接。
"""

from __future__ import annotations

from astrbot.api import logger

from .memory_bridge import DEFAULT_MAX_CHARS, truncate_text

KB_HEADER = "【知识库检索结果｜与本轮无关时忽略，禁止据此编造】"


async def retrieve_kb_block(
    context,
    *,
    origin: str,
    query: str,
    top_k: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """检索知识库并返回可直接拼进 prompt 的文本块（失败/无结果返回空串）。"""
    query = (query or "").strip()
    if not query or context is None or not origin:
        return ""

    text = None
    if top_k <= 0:
        # top_k <= 0 表示跟随 AstrBot 自己的知识库配置，走官方入口
        text = await _retrieve_via_core(context, origin=origin, query=query)
    if text is None:
        text = await _retrieve_locally(context, origin=origin, query=query, top_k=top_k)

    text = (text or "").strip()
    if not text:
        return ""
    return truncate_text(f"{KB_HEADER}\n{text}", max_chars)


async def _retrieve_via_core(context, *, origin: str, query: str):
    """返回 str（检索结果，可能为空）或 None（需要退回本地实现）。"""
    try:
        from astrbot.core.tools.knowledge_base_tools import (
            retrieve_knowledge_base,
        )
    except Exception as e:
        logger.debug(f"self-reply | kb | 官方知识库入口不可用，改用本地实现: {e}")
        return None

    try:
        text = await retrieve_knowledge_base(query=query, umo=origin, context=context)
        return text or ""
    except Exception as e:
        logger.warning(f"self-reply | kb | 官方知识库检索失败: {e}")
        return None


def _kb_settings(context, origin: str) -> tuple[list[str], int, int]:
    """读取知识库名称与 top_k 配置（与核心一致：全局配置项）。"""
    cfg = {}
    try:
        cfg = context.get_config(umo=origin) or {}
    except Exception as e:
        logger.debug(f"self-reply | kb | 读取会话配置失败: {e}")
    if not isinstance(cfg, dict):
        return [], 5, 20

    names = cfg.get("kb_names") or []
    if isinstance(names, str):
        names = [names]
    names = [str(name).strip() for name in names if str(name).strip()]

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return names, _int("kb_final_top_k", 5), _int("kb_fusion_top_k", 20)


async def _retrieve_locally(context, *, origin: str, query: str, top_k: int) -> str:
    kb_manager = getattr(context, "kb_manager", None)
    if kb_manager is None:
        return ""

    names, final_k, fusion_k = _kb_settings(context, origin)
    if not names:
        logger.debug("self-reply | kb | 未配置知识库名称，跳过知识库检索")
        return ""
    if top_k > 0:
        final_k = top_k

    try:
        result = await kb_manager.retrieve(
            query=query,
            kb_names=names,
            top_k_fusion=fusion_k,
            top_m_final=final_k,
        )
    except Exception as e:
        logger.warning(f"self-reply | kb | 本地知识库检索失败: {e}")
        return ""

    if not isinstance(result, dict):
        return ""
    text = str(result.get("context_text") or "").strip()
    if text:
        logger.info(f"self-reply | kb | 检索到知识块 kb={len(names)} chars={len(text)}")
    return text
