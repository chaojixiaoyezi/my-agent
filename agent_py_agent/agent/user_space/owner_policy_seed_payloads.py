
from __future__ import annotations

"""Owner 权限、配额、Memory retention、Skill 与工具策略默认值。"""

# LLM: seed payload 是新 owner 的机器配置起点；字段变更必须同步 migration、CLI 中文说明和策略测试。
# 模块用途: 生成首次初始化时写入 owner home 的结构化策略 JSON。

# LLM: 权限 seed 只定义通用边界，不从提示词或用户正文推导例外。
# 函数用途: 返回新 owner 的默认文件、网络、shell 和子代理权限。
def default_permissions_payload() -> dict[str, object]:
    return {
        "schema_version": "permissions.v1",
        "filesystem": {"access_mode": "workspace-write", "dangerous_paths": ["/", "/etc", "/System", "~/.ssh"]},
        "network": {"enabled": True},
        "shell": {"inherits_parent": True},
        "subagents": {"inheritance": "parent_capped"},
    }


# LLM: quota 数字是 owner 资源上界；运行时仍由统一 quota enforcer 执行。
# 函数用途: 返回新 owner 的默认并发、深度和磁盘配额。
def default_quota_payload() -> dict[str, object]:
    return {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }


# LLM: retention v2 key 是唯一运行合同；旧 raw_days/task_completed_days 只能经显式 Memory migration 转换。
# 函数用途: 返回完整会话、审计、Daily、工具输出、候选、Curator、Compact 和任务默认保留期。
def default_retention_payload() -> dict[str, object]:
    return {
        "schema_version": "my-agent.memory-retention.v2",
        "conversation_days": 365,
        "audit_days": 180,
        "daily_days": 365,
        "tool_output_days_after_terminal": 30,
        "rejected_candidate_days": 30,
        "curator_run_days": 90,
        "compact_days": 365,
        "completed_task_days": 365,
        "subagent_scratch_days": 30,
        "cache_days": 30,
        "tmp_days": 7,
        "trash_days": 30,
        "legal_hold": False,
        "legal_hold_task_ids": [],
        "maintenance_enabled": True,
        "maintenance_interval_seconds": 86400,
    }


# LLM: Memory 总闸是单一结构化 effective flag（memory-policy.v1.enabled）；关闭后
# curator 调度/发现层判活/决策点召回全部短路，与 skill-policy 完全对称、互不级联。
# 缺失该文件的老 owner 视为开启（默认 True，兼容既有行为）。
# 函数用途: 返回新 owner 的 Memory 子系统默认策略。
def default_memory_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "memory-policy.v1",
        "enabled": True,
    }


# LLM: Skill source allowlist 只约束来源，不在 seed 中复制 Skill 内容。
# enabled 是单一结构化总闸（与 memory-policy 对称）:关闭后快照为空，与来源名单独立。
# 函数用途: 返回新 owner 可见 Skill 来源的默认策略。
def default_skill_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "skill-policy.v1",
        "enabled": True,
        "enabled_sources": ["owner", "workspace", "shared", "builtin"],
        "enabled_shared_skills": [],
        "disabled_skills": [],
    }


# LLM: 工具禁用列表是结构化机器事实；空列表表示继承其他安全边界而非全权限。
# 函数用途: 返回新 owner 的默认工具策略。
def default_tool_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "tool-policy.v1",
        "disabled_tools": [],
    }


__all__ = [
    "default_memory_policy_payload",
    "default_permissions_payload",
    "default_quota_payload",
    "default_retention_payload",
    "default_skill_policy_payload",
    "default_tool_policy_payload",
]
