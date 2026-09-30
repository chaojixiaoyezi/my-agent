# LLM: owner 维护的唯一入口与状态文件 O/data/maintenance.json（schema owner-maintenance.v1，只加键、不改旧键含义）。
#   status/last_success_at 是旧汇总口径（有任何错误就 policy_unavailable）；「到底执行了没有」看 apply_outcome，
#   被隔离的路径级错误看 isolated_error_count，最近一次真正执行看 last_applied_at。改动联测 test_owner_maintenance
#   与 test_gateway_owner_maintenance。
# 模块用途: 按 owner 到期跑保留、向量缓存回收与 global_index 压缩，并把结构化结果写进维护状态文件。
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


# LLM: status 保持旧口径；apply_outcome（applied / refused / legal_hold）、isolated_error_count 与 failed_action_count
#   供 Gateway 摘要区分「整次被拒」「执行期动作失败」和「执行了、只是隔离了 N 条」。没跑（not_due）时新字段为空值，
#   to_dict 也不输出它们。
# 类用途: 一次维护调用的结果：跑没跑、旧状态、执行结果、被隔离的错误条数与执行期失败的动作数。
@dataclass(frozen=True)
class OwnerMaintenanceResult:
    ran: bool
    status: str
    retention: OwnerRetentionPlan | None = None
    apply_outcome: str = ""
    isolated_error_count: int = 0
    failed_action_count: int = 0

    # LLM: 旧键原样输出；新键只在真正跑过时附加，not_due 的输出与旧版逐字相同。
    # 函数用途: 转成可打印或测试比对的字典。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ran": self.ran, "status": self.status}
        if self.apply_outcome:
            payload["apply_outcome"] = self.apply_outcome
            payload["isolated_error_count"] = self.isolated_error_count
            payload["failed_action_count"] = self.failed_action_count
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


# LLM: 维护状态只加键：apply_outcome / isolated_error_count / last_applied_at（旧文件缺这个键时按 0 起算）；
#   status 与 last_success_at 含义不变。写 maintenance.json 与审计属于副作用，都在 locked_json_path 内完成。
# 函数用途: owner 到期时跑一轮维护并写结构化状态；没到期直接返回 not_due。
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
        outcome = _apply_outcome(retention)
        failed_actions = sum(
            action.status in {"failed", "collision", "state_changed"}
            for action in retention.actions
        )
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
            "failed_action_count": failed_actions,
            "load_errors": list(retention.load_errors),
            "legal_hold": retention.legal_hold,
            "text_vector_cache_reclaimed": dropped_cache_keys,
            # 非空表示"这次没能真的回收"（建缓存/读记忆失败），0 不等于"没有孤儿"。
            "text_vector_cache_reclaim_error": reclaim_error,
            "indexes_compacted": compacted,
            # "试了但失败"（io_error / identity_changed）单列，免得跟"没到期"混成一片。
            "indexes_compact_failed": compact_failed,
            # status 有路径级错误就记 policy_unavailable，会把"其实执行了"掩盖掉；这三个键说清执行事实。
            "apply_outcome": outcome,
            "isolated_error_count": len(retention.isolated_errors),
            "last_applied_at": (
                current if outcome == "applied" else _timestamp(previous.get("last_applied_at"))
            ),
        }
        write_json_file_atomic_unlocked(state_path, payload)
        return OwnerMaintenanceResult(
            True, status, retention, apply_outcome=outcome, isolated_error_count=len(retention.isolated_errors),
            failed_action_count=failed_actions,
        )


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


# LLM: 旧汇总口径，含义保持不变（持久化字段只加不改）：只要有任何错误（含已被隔离的路径级错误）就是 policy_unavailable。
#   判断是否真的执行过用 _apply_outcome，不要改这里。
# 函数用途: 给出维护状态文件里的旧 status 值。
def _maintenance_status(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    if retention.load_errors:
        return "policy_unavailable"
    if any(action.status in {"failed", "collision"} for action in retention.actions):
        return "partial_failure"
    return "success"


# LLM: 只看 report.applied 与 legal_hold，不看错误列表：有路径级错误但执行了其余动作仍是 applied；
#   refused 与 retention.apply() 的整份拒绝是同一个判定（_has_policy_level_error，即 _POLICY_LEVEL_ERROR_CODES：
#   策略无效/读不了、候选账本读不了），这里不另造错误码集合。
# 函数用途: 给出本轮保留到底执行了没有：applied / refused / legal_hold。
def _apply_outcome(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    return "applied" if retention.applied else "refused"


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
