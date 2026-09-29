from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .home_layout import MyAgentHomePaths
from .home_retention import OwnerRetentionPlan, apply_owner_retention

_DEFAULT_INTERVAL_SECONDS = 86_400


@dataclass(frozen=True)
class OwnerMaintenanceResult:
    ran: bool
    status: str
    retention: OwnerRetentionPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ran": self.ran, "status": self.status}
        if self.retention is not None:
            payload["retention"] = self.retention.to_dict()
        return payload


def owner_maintenance_due(
    owner_home: str | Path,
    *,
    now: float | None = None,
) -> bool:
    home = Path(owner_home)
    policy = read_json_object_report(
        home / "retention.json",
        context="owner_maintenance.policy",
    ).payload
    if not bool(policy.get("maintenance_enabled", True)):
        return False
    interval = _positive_interval(policy.get("maintenance_interval_seconds"))
    if interval <= 0:
        return False
    marker = read_json_object_report(
        _maintenance_state_path(home),
        context="owner_maintenance.state",
    ).payload
    last_attempt = _timestamp(marker.get("last_attempt_at"))
    current = float(now if now is not None else time.time())
    return last_attempt <= 0 or current - last_attempt >= interval


def run_owner_retention_if_due(
    home: MyAgentHomePaths,
    *,
    now: float | None = None,
) -> OwnerMaintenanceResult:
    current = float(now if now is not None else time.time())
    if not owner_maintenance_due(home.owner_home_dir, now=current):
        return OwnerMaintenanceResult(False, "not_due")
    state_path = _maintenance_state_path(home.owner_home_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(state_path):
        if not owner_maintenance_due(home.owner_home_dir, now=current):
            return OwnerMaintenanceResult(False, "not_due")
        retention = apply_owner_retention(
            home,
            now=datetime.fromtimestamp(current, timezone.utc),
        )
        # 正文哈希缓存只是派生索引，孤儿键（换模型/迁移/绕过写入路径的改动留下）只有 index_all
        # 会回收，那是手动命令；挂进这里让它在 owner 维护（默认每天一次）里自动收口。
        dropped_cache_keys, reclaim_error = _reclaim_text_vector_cache_orphans(home)
        # global_index 四份只追加索引在这里按 key 内部压缩（纯投影，不读权威源）。
        # 单份失败不中断维护；成功与"试了但失败"分开汇总进维护状态，便于观察是否真的压过。
        compacted, compact_failed = _compact_global_indexes(home, now=current)
        status = _maintenance_status(retention)
        previous = read_json_object_report(
            state_path,
            context="owner_maintenance.state",
        ).payload
        payload = {
            "schema_version": "owner-maintenance.v1",
            "last_attempt_at": current,
            "last_success_at": (
                current
                if status == "success"
                else _timestamp(previous.get("last_success_at"))
            ),
            "status": status,
            "action_count": len(retention.actions),
            "failed_action_count": sum(
                action.status in {"failed", "collision", "state_changed"}
                for action in retention.actions
            ),
            "load_errors": list(retention.load_errors),
            "legal_hold": retention.legal_hold,
            "text_vector_cache_reclaimed": dropped_cache_keys,
            # 非空表示"这次没能真的回收"（建缓存/读记忆失败），0 不等于"没有孤儿"。
            "text_vector_cache_reclaim_error": reclaim_error,
            "indexes_compacted": compacted,
            # "试了但失败"（io_error / identity_changed）单列，免得跟"没到期"混成一片。
            "indexes_compact_failed": compact_failed,
        }
        write_json_file_atomic_unlocked(state_path, payload)
        return OwnerMaintenanceResult(True, status, retention)


# LLM: 回收只读 active 记忆算出保留集合，不加载嵌入模型、不联网；失败只记 0，绝不影响保留策略结果。
#   路径必须用 canonical 字段（owner_memory_long_term_jsonl），此前手拼 `memory/memory.jsonl` 在生产布局下
#   文件根本不存在，于是每天写一个假的 `text_vector_cache_reclaimed: 0`——看起来像"跑过、没有孤儿"。
# 函数用途: 在 owner 维护里回收正文哈希缓存中不属于任何 active 记忆的键。
def _reclaim_text_vector_cache_orphans(home: MyAgentHomePaths) -> tuple[int, str]:
    """返回 (回收数, 错误码)。错误码为空表示这次真的跑过；非空表示"0 不是因为没孤儿"。"""
    try:
        from ..memory_store.jsonl import JsonlMemory

        memory_path = home.owner_memory_long_term_jsonl
        if not memory_path.exists():
            return 0, "memory_file_missing"
        # 回收不需要 embedder：保留集合按 active 记录的正文哈希算（见 reclaim_text_cache_orphans）。
        memory = JsonlMemory(memory_path)
        # 回收自己也会遇到"读不了缓存 / 拿不到锁"，它把错误说明交回来（V5），不能再被当成空串。
        return memory.reclaim_text_cache_orphans()
    except Exception as exc:
        # 建缓存/读记忆失败也返回 0，但与"确实没有孤儿"必须分得开——否则又是一条假的结构化事实。
        return 0, f"{type(exc).__name__}: {exc}"
# LLM: 压缩是派生数据的整理，失败绝不能影响 retention 结果；这里只回传计数与坏行数。
#   "试了但失败"（io_error / identity_changed）必须与"没到期"分开记：
#   只记 compacted=True 的话，maintenance.json 分不出"没到期"和"压失败了"，
#   失败会被静默吞掉——这正是本轮修掉的那类假结构化事实。
# 函数用途: 在维护事务里对四份 global_index 做"该压就压"，返回 (成功摘要, 失败摘要)。
def _compact_global_indexes(
    home: MyAgentHomePaths, *, now: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        from .home_index_compact import compact_global_indexes_if_due

        results = compact_global_indexes_if_due(home, now=now)
    except Exception:
        return [], []
    compacted: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for result in results:
        entry = {"path": result.path.name, **result.to_dict()}
        if result.compacted:
            compacted.append(entry)
            continue
        # "没到期"不是失败；只有真的试过却没成功的才算。
        if result.reason in _COMPACT_NOT_ATTEMPTED_REASONS:
            continue
        failed.append(entry)
    return compacted, failed


# LLM: 这些原因表示"这次根本没动手"，不能算失败，否则维护状态天天报假失败。
# 函数用途: 判断压缩结果是否属于"没尝试"。
_COMPACT_NOT_ATTEMPTED_REASONS = frozenset(
    {"below_min_bytes", "below_growth_ratio", "in_cooldown", "missing"}
)


def _maintenance_status(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    if retention.load_errors:
        return "policy_unavailable"
    if any(action.status in {"failed", "collision"} for action in retention.actions):
        return "partial_failure"
    return "success"


def _maintenance_state_path(owner_home: Path) -> Path:
    return owner_home / "data" / "maintenance.json"


def _positive_interval(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_SECONDS
    return max(0, parsed)


def _timestamp(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "OwnerMaintenanceResult",
    "owner_maintenance_due",
    "run_owner_retention_if_due",
]
