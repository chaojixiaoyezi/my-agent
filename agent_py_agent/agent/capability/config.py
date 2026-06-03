
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
    capability_fallback_max_attempts: int = 3
    subagent_heartbeat_timeout: int = 0
    subagent_run_timeout: int = 0
    subagent_due_check_interval: int = 0
    subagent_min_evidence_for_done: int = 1
    subagent_no_progress_attempt_limit: int = 4
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
    """加载能力路由配置，并忽略旧版本暂不认识的字段。"""

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"能力路由配置文件不存在: {path}")
    raw = load_simple_yaml(path)
    allowed = set(CapabilityConfig.__dataclass_fields__.keys())
    clean = {key: value for key, value in raw.items() if key in allowed}
    return CapabilityConfig(**clean)
