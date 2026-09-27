
from __future__ import annotations

"""能力路由与子代理运行投影配置加载工具。

这个模块专门放 skill / tool / 子代理授权、能力上抛和子代理运行状态投影相关配置。
它刻意和 `agent_config.yaml` 分开，避免主配置文件越来越像一个杂物间。
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..settings.config import load_simple_yaml

# LLM: 子代理权限、运行投影、包候选展示和阶段提醒的唯一默认配置；新增字段同步随包 YAML 与配置一致性测试。
# 模块用途: 集中读取协作能力设置；包候选只影响上下文，阶段提醒不作为执行超时或权限授予。

# LLM: 包准备默认关闭；主任务一次选择，合格新 child 仅加载已授权同代入口，不再选择；原首请求状态和预算不授予权限。
# 类用途: 定义子代理能力和包上下文设置；推荐只展示摘要，选包会消耗模型用量并记录原任务，数值配额为零仍受模型窗口限制。
@dataclass
class CapabilityConfig:
    """能力路由与子代理运行配置总表。

    既有额度数字项：0 表示不限制；点位决策等待与模型引用只在决策设置覆盖层按点位设置，默认继承通用值。
    决策关闭由 mode=off 表达，不把零解释为无限等待。"""

    decision_subagent_model_mode: str = "off"
    decision_subagent_model_candidate_profile_ids: list[str] = field(default_factory=list)
    decision_skill_tool_mode: str = "off"
    decision_skill_tool_context_policy: str = "progressive"
    decision_skill_tool_optional_categories: list[str] = field(default_factory=lambda: ["plugins"])
    # my-agent 经 user_config decision_patch 自调决策等待时间的上下限（整数秒）；0 表示该侧不限制，用户菜单修改不受限。
    decision_agent_timeout_min_seconds: int = 1
    decision_agent_timeout_max_seconds: int = 30
    enable_capability_routing: bool = False
    enable_capability_package_recommendations: bool = True
    enable_capability_package_selection: bool = False
    capability_package_selection_max_input_tokens: int = 3000
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
    # 使用既有 runner 心跳做阶段化提醒；不改变执行超时、终态或重试，关闭时不产生提醒。
    subagent_activity_notices_enabled: bool = True
    # 首 token/退避后长期无事件、已开始流的静默、长工具分别提醒；0 关闭相应阶段提醒。
    subagent_first_token_notice_seconds: int = 600
    subagent_stream_idle_notice_seconds: int = 180
    subagent_tool_wait_notice_seconds: int = 900
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
    # 加载时累积的告警（已删/未知键等）；只用于诊断展示，不参与路由判断。
    config_warnings: list[str] = field(default_factory=list)


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


# LLM: 决策能力点严格校验，有限正秒数不沿用普通配额的零值约定；不授予新能力。
# 函数用途: 加载原能力配置，已删/未知字段记告警并忽略，非法决策设置仍旧报错。
def load_capability_config(config_path: str | Path) -> CapabilityConfig:
    """加载能力路由配置；未知字段只记告警并忽略（与主配置一致），非法值仍报错。"""

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"能力路由配置文件不存在: {path}")
    raw = load_simple_yaml(path)
    from ..settings.decision_settings_defaults import validate_config_decision_fields

    raw.update(validate_config_decision_fields(raw, domain="capability"))
    allowed = set(CapabilityConfig.__dataclass_fields__.keys())
    # 参数减量的约定：已删的键只告警、不迁移，也不能拦住加载；用户配置里残留旧键是常态。
    config_warnings = [
        f"unknown capability config key: {key!r}; ignored" for key in sorted(set(raw) - allowed)
    ]
    clean = {key: _coerce_capability_value(key, value) for key, value in raw.items() if key in allowed}
    config = CapabilityConfig(**clean)
    config.config_warnings = config_warnings
    return config
