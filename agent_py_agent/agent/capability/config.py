
from __future__ import annotations

"""能力路由配置加载工具。

这个模块专门放 skill / tool / 子代理授权 / 能力上抛相关配置。
它刻意和 `agent_config.yaml` 分开，避免主配置文件越来越像一个杂物间。
"""

from dataclasses import dataclass
from pathlib import Path

from ..settings.config import load_simple_yaml


@dataclass
class CapabilityConfig:
    """能力路由配置总表。

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
    # 子代理 task-local 回合的 compact 触发百分比；0 表示继承主代理
    # memory_compact_auto_trigger_percent，不另起一套默认值。
    subagent_compact_trigger_percent: int = 0
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
    clean = {key: value for key, value in raw.items() if key in allowed}
    return CapabilityConfig(**clean)
