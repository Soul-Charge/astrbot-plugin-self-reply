from __future__ import annotations

from dataclasses import dataclass, field

from .judge_utils import DEFAULT_JUDGE_PROMPT

_QUOTE_POLICIES = ("judge", "model", "none")


def _to_bool(raw, default: bool = False) -> bool:
    """容错布尔转换：bool 直取；数值非 0 为真；字符串按常见真值解析。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "yes", "on", "y"):
            return True
        if s in ("0", "false", "no", "off", "n", ""):
            return False
    return default


def _to_float(raw, default: float) -> float:
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            pass
    return default


def _to_int(raw, default: int) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            try:
                return int(float(raw.strip()))
            except ValueError:
                pass
    return default


def _to_list(raw, default: list[str]) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    return list(default)


def _sanitize_quote_policy(raw) -> str:
    policy = str(raw or "").strip().lower()
    return policy if policy in _QUOTE_POLICIES else "judge"


@dataclass(frozen=True)
class TriggerConfig:
    debounce_normal_seconds: float = 5.0
    debounce_quick_seconds: float = 1.0
    min_reply_interval_seconds: float = 15.0
    quick_trigger_keywords: list[str] = field(default_factory=list)
    max_pending_before_judge: int = 20


@dataclass(frozen=True)
class JudgeConfig:
    provider_id: str = "deepseek/deepseek-v4-flash"
    history_messages: int = 30
    prompt_template: str = DEFAULT_JUDGE_PROMPT


@dataclass(frozen=True)
class GenerateConfig:
    provider_id: str = "deepseek-vision"
    attach_recent_images: int = 3
    quote_policy: str = "judge"
    include_role_tag: bool = True
    include_sender_id: bool = True


@dataclass(frozen=True)
class AntiRepeatConfig:
    enable: bool = True
    similarity_threshold: float = 0.85
    max_retries: int = 1
    compare_window: int = 10


@dataclass(frozen=True)
class HistoryConfig:
    max_messages: int = 50


@dataclass(frozen=True)
class WhitelistConfig:
    allowed_origins: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GlobalSettings:
    max_origins: int = 500
    judge_timeout_sec: float = 45.0
    generate_timeout_sec: float = 60.0


@dataclass(frozen=True)
class PluginConfig:
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)
    anti_repeat: AntiRepeatConfig = field(default_factory=AntiRepeatConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    whitelist: WhitelistConfig = field(default_factory=WhitelistConfig)
    global_settings: GlobalSettings = field(default_factory=GlobalSettings)
    enable: bool = False

    @property
    def enabled(self) -> bool:
        return self.enable


def _as_dict(raw) -> dict:
    return raw if isinstance(raw, dict) else {}


def parse_plugin_config(raw: dict) -> PluginConfig:
    raw = _as_dict(raw)
    trigger_raw = _as_dict(raw.get("trigger"))
    judge_raw = _as_dict(raw.get("judge"))
    generate_raw = _as_dict(raw.get("generate"))
    anti_raw = _as_dict(raw.get("anti_repeat"))
    history_raw = _as_dict(raw.get("history"))
    whitelist_raw = _as_dict(raw.get("whitelist"))
    global_raw = _as_dict(raw.get("global_settings"))

    return PluginConfig(
        enable=_to_bool(raw.get("enable"), False),
        trigger=TriggerConfig(
            debounce_normal_seconds=_to_float(
                trigger_raw.get("debounce_normal_seconds"), 5.0
            ),
            debounce_quick_seconds=_to_float(
                trigger_raw.get("debounce_quick_seconds"), 1.0
            ),
            min_reply_interval_seconds=_to_float(
                trigger_raw.get("min_reply_interval_seconds"), 15.0
            ),
            quick_trigger_keywords=_to_list(
                trigger_raw.get("quick_trigger_keywords"), []
            ),
            max_pending_before_judge=_to_int(
                trigger_raw.get("max_pending_before_judge"), 20
            ),
        ),
        judge=JudgeConfig(
            provider_id=str(
                judge_raw.get("provider_id") or "deepseek/deepseek-v4-flash"
            ).strip(),
            history_messages=_to_int(judge_raw.get("history_messages"), 30),
            prompt_template=str(judge_raw.get("prompt_template") or DEFAULT_JUDGE_PROMPT),
        ),
        generate=GenerateConfig(
            provider_id=str(generate_raw.get("provider_id") or "deepseek-vision").strip(),
            attach_recent_images=_to_int(generate_raw.get("attach_recent_images"), 3),
            quote_policy=_sanitize_quote_policy(generate_raw.get("quote_policy", "judge")),
            include_role_tag=_to_bool(generate_raw.get("include_role_tag"), True),
            include_sender_id=_to_bool(generate_raw.get("include_sender_id"), True),
        ),
        anti_repeat=AntiRepeatConfig(
            enable=_to_bool(anti_raw.get("enable"), True),
            similarity_threshold=_to_float(anti_raw.get("similarity_threshold"), 0.85),
            max_retries=_to_int(anti_raw.get("max_retries"), 1),
            compare_window=_to_int(anti_raw.get("compare_window"), 10),
        ),
        history=HistoryConfig(max_messages=_to_int(history_raw.get("max_messages"), 50)),
        whitelist=WhitelistConfig(
            allowed_origins=_to_list(whitelist_raw.get("allowed_origins"), [])
        ),
        global_settings=GlobalSettings(
            max_origins=_to_int(global_raw.get("max_origins"), 500),
            judge_timeout_sec=_to_float(global_raw.get("judge_timeout_sec"), 45.0),
            generate_timeout_sec=_to_float(
                global_raw.get("generate_timeout_sec"), 60.0
            ),
        ),
    )
