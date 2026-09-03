"""pytest 公共配置：包别名 + astrbot 桩（仅在运行环境没有 astrbot 包时安装）。"""

import logging
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = Path(__file__).resolve().parents[2]

for _candidate in (PROJECT_ROOT, PLUGINS_DIR):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


def _ensure_plugin_package_alias() -> None:
    """插件根目录没有 __init__.py（AstrBot 惯例），测试中用包别名暴露源码。"""
    if "astrbot_plugin_self_reply" in sys.modules:
        return
    pkg = types.ModuleType("astrbot_plugin_self_reply")
    pkg.__path__ = [str(PROJECT_ROOT)]
    pkg.__file__ = str(PROJECT_ROOT / "__init__.py")
    sys.modules["astrbot_plugin_self_reply"] = pkg


def _install_astrbot_stubs() -> None:
    try:
        import astrbot.api.message_components  # noqa: F401
        return
    except Exception:
        pass

    logger = logging.getLogger("astrbot-test")

    async def _async_none():
        return None

    def _package(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        if "." in name:
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child, mod)
        return mod

    astrbot_mod = _package("astrbot")
    astrbot_mod.logger = logger
    api_mod = _package("astrbot.api")
    api_mod.logger = logger
    api_mod.sp = types.SimpleNamespace(
        get_async=lambda *args, **kwargs: _async_none()
    )

    class _Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config

    api_mod.star = types.SimpleNamespace(Star=_Star)

    components_mod = _package("astrbot.api.message_components")

    class Plain:
        def __init__(self, text: str = "", **kwargs):
            self.text = text

    class At:
        def __init__(self, qq="", name="", **kwargs):
            self.qq = qq
            self.name = name

    class Reply:
        def __init__(self, id="", **kwargs):
            self.id = id
            self.sender_nickname = ""
            self.message_str = ""

    components_mod.Plain = Plain
    components_mod.At = At
    components_mod.Reply = Reply

    event_mod = _package("astrbot.api.event")
    filter_mod = _package("astrbot.api.event.filter")

    def _passthrough_decorator(*args, **kwargs):
        def wrapper(func):
            return func

        return wrapper

    filter_mod.event_message_type = _passthrough_decorator
    event_mod.filter = filter_mod
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = object

    platform_mod = _package("astrbot.api.platform")

    class MessageType:
        GROUP_MESSAGE = "GroupMessage"
        FRIEND_MESSAGE = "FriendMessage"

    platform_mod.MessageType = MessageType

    star_pkg_mod = _package("astrbot.api.star")
    star_pkg_mod.Context = object

    core_mod = _package("astrbot.core")
    core_star_mod = _package("astrbot.core.star")
    star_tools_mod = _package("astrbot.core.star.star_tools")

    class StarTools:
        @classmethod
        async def send_message(cls, session, message_chain):
            return True

    star_tools_mod.StarTools = StarTools
    core_star_mod.star_tools = star_tools_mod

    _package("astrbot.core.message")
    msg_result_mod = _package("astrbot.core.message.message_event_result")

    class MessageChain:
        def __init__(self, chain=None, **kwargs):
            self.chain = chain or []

    msg_result_mod.MessageChain = MessageChain


_ensure_plugin_package_alias()
_install_astrbot_stubs()
