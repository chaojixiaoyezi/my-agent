from __future__ import annotations

"""CLI 与外围运行时可依赖的窄 Memory v2 公共接口。"""

# LLM: CLI只接收稳定服务及只读查询结果；状态解释归记忆领域，预检不创建Agent、不写游标或取得租约。
# 模块用途: 提供Memory公共接口与冷owner三态预检，阻止命令层解析内部状态文件。

import os
from collections.abc import Callable
from itertools import islice
from pathlib import Path

from .memory_store.agents_migration import (
    AgentsCleanupError,
    AgentsCleanupPlan,
    AgentsCleanupRequest,
    apply_agents_cleanup,
    plan_agents_cleanup,
)
from .memory_store.candidate_models import (
    CANDIDATE_STATUSES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
)
from .memory_store.curator_state import MemoryCuratorStateStore
from .memory_store.daily import DAILY_ACTORS, DAILY_EVENT_TYPES, DailyMemoryStore
from .memory_store.lessons import LessonRepository
from .memory_store.lifecycle import request_memory_curator_for_session_best_effort
from .memory_store.migration import MemoryMigrationService
from .memory_store.retention import MemoryRetentionService

__all__ = [
    "AgentsCleanupError",
    "AgentsCleanupPlan",
    "AgentsCleanupRequest",
    "CANDIDATE_STATUSES",
    "DAILY_ACTORS",
    "DAILY_EVENT_TYPES",
    "PROMOTION_TARGETS",
    "PROPOSED_ACTIONS",
    "DailyMemoryStore",
    "LessonRepository",
    "MemoryCuratorStateStore",
    "MemoryMigrationService",
    "MemoryRetentionService",
    "apply_agents_cleanup",
    "cold_owner_curator_precheck",
    "plan_agents_cleanup",
    "request_memory_curator_for_session_best_effort",
]


# LLM: 每目录最多采样65个名字；预算用尽或IO未知不推断空目录，不创建服务或读取正文。
# 函数用途: 三态检查一个目录是否有指定后缀的非空输入。
def _directory_activity(root: Path, suffix: str) -> str:
    try:
        with os.scandir(root) as entries:
            sample = list(islice(entries, 65))
    except FileNotFoundError:
        return "not_due"
    except OSError:
        return "unknown"
    try:
        if any(entry.name.endswith(suffix) and entry.is_file() and entry.stat().st_size for entry in sample[:64]):
            return "due"
    except OSError:
        return "unknown"
    return "unknown" if len(sample) > 64 else "not_due"


# LLM: 只检查持久队列元数据，不实例化自学习服务；未知不缓存为无任务。
# 函数用途: 对冷owner自学习队列作三态轻量检查。
def _owner_home_skill_learning_state(home: Path) -> str:
    states = [_directory_activity(home / "data" / "skill_learning" / folder, ".json")
              for folder in ("requests", "requests_overflow")]
    return "due" if "due" in states else "unknown" if "unknown" in states else "not_due"


# LLM: 只采样最多65个消息文件；线程元数据明确learning_excluded=True时跳过，不唤醒冷owner。
# 函数用途: 三态检查会话库是否有非禁学线程的消息输入。
def _store_messages_activity(store_root: Path) -> str:
    from .common.json_io import read_json_object_report

    try:
        with os.scandir(store_root / "messages") as entries:
            sample = list(islice(entries, 65))
    except FileNotFoundError:
        return "not_due"
    except OSError:
        return "unknown"
    try:
        for entry in sample[:64]:
            if not (entry.name.endswith(".jsonl") and entry.is_file() and entry.stat().st_size):
                continue
            thread_path = store_root / "threads" / f"{entry.name[:-6]}.json"
            report = read_json_object_report(thread_path, context="gateway.curator.thread_precheck")
            payload = report.payload if not report.load_error and isinstance(report.payload, dict) else None
            meta = payload.get("metadata") if isinstance(payload, dict) else None
            if isinstance(payload, dict) and (
                payload.get("learning_excluded") is True
                or (isinstance(meta, dict) and meta.get("learning_excluded") is True)
            ):
                continue
            return "due"
    except OSError:
        return "unknown"
    return "unknown" if len(sample) > 64 else "not_due"


# LLM: 只读消息/候选/audit元数据；沿原有界三态扫描和存储根，禁学线程不参与，未知不推为空。
# 函数用途: 确认周期整理是否有非空可学输入，只有审计记录的冷owner也能唤醒。
def _cold_curator_activity(home: Path) -> str:
    try:
        candidate = home / "memory" / "candidates.jsonl"
        if candidate.exists() and candidate.stat().st_size:
            return "due"
    except OSError:
        return "unknown"
    from .owner_wake_discovery import _store_roots

    states = [_store_messages_activity(root) for root in _store_roots(home)]
    states.append(_directory_activity(home / "audit", ".jsonl"))
    return "due" if "due" in states else "unknown" if "unknown" in states else "not_due"


# LLM: 查询仅接宿主可信home/config；保留due/not_due/unknown与urgent组合，自学习due覆盖坏预检。
#   配置provider在原state解析之后才求值，保持关闭/坏态时的惰性；原执行器仍独立复核退避及租约。
# 函数用途: 在创建冷owner Agent前只读判断记忆策展/自学习工作，不取得执行权或写入状态。
def cold_owner_curator_precheck(
    owner_home: Path, *, config: object, curator_config_provider: Callable[[], object] | None = None,
) -> tuple[str, bool]:
    from .common.json_io import read_json_object_report
    from .memory_store.curator_models import MemoryCuratorState
    from .owner_wake_discovery import curator_work_is_due
    from .user_space.owner_policy import effective_memory_enabled

    try:
        home = Path(owner_home)
        skill = (_owner_home_skill_learning_state(home) if getattr(config, "enable_self_learning", False)
                 else "not_due")
        policy = read_json_object_report(home / "memory_policy.json", context="gateway.curator.precheck")
        if policy.load_error:
            return ("due" if skill == "due" else "unknown"), False
        if not getattr(config, "memory_curator_enabled", True) or not effective_memory_enabled(policy.payload):
            return skill, False
        report = read_json_object_report(home / "memory" / "curator" / "state.json", context="gateway.curator.state")
        if report.load_error:
            return ("due" if skill == "due" else "unknown"), False
        state = MemoryCuratorState.from_dict(report.payload or {})
        urgent = bool(state.pending_reasons or state.active_lease)
        due = curator_work_is_due(home, state,
            curator_config=curator_config_provider() if curator_config_provider is not None else None,
            timezone_name=getattr(config, "timezone", ""))
        if skill == "due" or (due and urgent):
            return "due", urgent
        activity = _cold_curator_activity(home) if due else "not_due"
        return (activity if activity != "not_due" else skill), False
    except (OSError, ValueError, TypeError, AttributeError):
        return "unknown", False
