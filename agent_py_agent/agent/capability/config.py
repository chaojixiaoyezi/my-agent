
from __future__ import annotations

"""能力路由与子代理运行投影配置加载工具。

这个模块专门放 skill / tool / 子代理授权、能力上抛和子代理运行状态投影相关配置。
它刻意和 `agent_config.yaml` 分开，避免主配置文件越来越像一个杂物间。
"""

from dataclasses import dataclass
from pathlib import Path

from ..settings.config import load_simple_yaml


@dataclass
class CapabilityConfig:
    """能力路由与子代理运行配置总表。

    数字限制项统一约定：0 表示不限制。
    这样用户可以先只打开关键限制，其余细节等系统成熟后再慢慢调。"""

    enable_capability_routing: bool = False
    capability_request_max_tokens: int = 600
    capability_escalation_max_hops: int = 0
    capability_candidate_limit: int = 5
    capability_bundle_max_tokens: int = 3000
    capability_alternative_max_attempts: int = 3
    subagent_heartbeat_timeout: int = 0
    subagent_run_timeout: int = 0
    subagent_due_check_interval: int = 0
    subagent_min_evidence_for_done: int = 1
    subagent_no_progress_attempt_limit: int = 4
    # 慢模型流式活动投影：只写时间、阶段和字符计数，不保存正文。
    subagent_stream_activity_projection_enabled: bool = True
    # 同一模型流阶段写 canonical child state 的最小间隔，避免逐 token 落盘。
    subagent_stream_activity_interval_seconds: int = 15
    # 失败自省自动拆分：should_split + 拆分建议存在时自动 split_task 重新派工。
    # 默认关闭——拆分会创建新任务并改变原任务状态，需用户显式开启。
    subagent_failure_auto_split_enabled: bool = False
    # 失败自省自动拆分的最大深度；0 表示不限制（统一约定）。
    subagent_failure_split_max_depth: int = 2
    capability_request_max_tried_items: int = 0
    capability_request_max_evidence_items: int = 0
    capability_request_max_per_task: int = 0
    capability_grant_max_skills: int = 0
    capability_grant_max_tools: int = 0
    capability_grant_expires_after_task: bool = True
    skill_card_max_tokens: int = 0
    tool_card_max_tokens: int = 0
    skill_body_max_tokens: int = 0


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes", "on"}:
            return True
        if token in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, int):
        return value != 0
    return default


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):  # bool 是 int 子类,别把 True 当 1
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return default


def _coerce_capability_value(field_name: str, value: object) -> object:
    """按 dataclass 声明类型归一配置值,不依赖 parse_scalar 的引号类型推断。

    自建 yaml 里带引号的标量按 YAML 语义是字符串(qq_app_id: "190…" 等纯数字 ID 必须保字符串),
    所以这里按字段默认值的真实类型(int/bool)把值coerce回来,'800' 与 800 都能正确落成 int。"""
    default = CapabilityConfig.__dataclass_fields__[field_name].default
    if isinstance(default, bool):
        return _coerce_bool(value, default)
    if isinstance(default, int):
        return _coerce_int(value, default)
    return value


def load_capability_config(config_path: str | Path) -> CapabilityConfig:
    """加载能力路由配置；未知字段直接报错。"""

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"能力路由配置文件不存在: {path}")
    raw = load_simple_yaml(path)
    allowed = set(CapabilityConfig.__dataclass_fields__.keys())
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"能力路由配置包含未知字段: {', '.join(unknown)}")
    clean = {key: _coerce_capability_value(key, value) for key, value in raw.items() if key in allowed}
    return CapabilityConfig(**clean)
