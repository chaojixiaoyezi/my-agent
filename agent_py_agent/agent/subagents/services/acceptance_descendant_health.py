# LLM: Descendant health acceptance keeps parent verification tied to persisted child task state.
# 模块用途: 父级验收时扫描真实 child_ids，避免后代仍在 PLANNING/BLOCKED/FAILED 时父级被误标为 VERIFIED。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..reports import AcceptanceReviewFinding

_DESCENDANT_SCAN_MAX_NODES = 96
_DESCENDANT_SCAN_MAX_BYTES = 65536
_HEALTHY_DONE = ("DONE", "VERIFIED")
_CLOSED_STATUSES = {"TAKEN_OVER", "ABANDONED"}


# LLM: descendant_health_finding is the public hard gate for coordinator tasks with children.
# 函数用途: 有真实 child_ids 的父任务，必须看到所有后代已健康收口，否则返回 P0 验收失败。
def descendant_health_finding(task: Any, created_at: float) -> AcceptanceReviewFinding:
    child_ids = _string_list(getattr(task, "child_ids", []))
    if not child_ids:
        return AcceptanceReviewFinding(
            name="descendant_health",
            ok=True,
            severity="P0",
            message="当前任务没有真实 child run，不需要后代健康门。",
            evidence_path=getattr(task, "output_json", ""),
            created_at=created_at,
        )
    state = _scan_descendants(task, child_ids)
    blocked = [item for item in state.items if not _is_healthy(item)]
    return AcceptanceReviewFinding(
        name="descendant_health",
        ok=not blocked,
        severity="P0",
        message=_descendant_health_message(state, blocked),
        evidence_path=getattr(task, "output_json", ""),
        created_at=created_at,
    )


# LLM: _scan_descendants walks child_ids breadth-first using exact task.json paths only.
# 函数用途: 有界扫描父任务下的真实后代状态，不读取报告正文或产物文件。
def _scan_descendants(task: Any, child_ids: list[str]) -> _DescendantScanState:
    workspace = _child_workspace(task)
    state = _DescendantScanState(workspace=workspace, queue=list(child_ids), seen=set(), items=[])
    if workspace is None:
        return state
    scanned = 0
    while state.queue and scanned < _DESCENDANT_SCAN_MAX_NODES:
        run_id = state.queue.pop(0)
        scanned += _scan_one_descendant(state, run_id)
    return state


# LLM: _DescendantScanState bundles scan state so helper signatures stay stable as fields grow.
# 类用途: 保存后代健康扫描需要的 workspace、queue、seen 和已读取状态项。
@dataclass
class _DescendantScanState:
    workspace: Path | None
    queue: list[str]
    seen: set[str]
    items: list[_DescendantStatus]


# LLM: _DescendantStatus carries only small persisted identity/status fields for acceptance messages.
# 类用途: 表示一个后代 run 的最小健康状态，避免把完整 task.json 传来传去。
@dataclass(frozen=True)
class _DescendantStatus:
    run_id: str
    role: str
    agent_name: str
    status: str
    verification_status: str


# LLM: _scan_one_descendant reads one task.json and queues its child_ids.
# 函数用途: 读取单个后代状态；缺文件也作为 blocked 项记录，避免父级无声通过。
def _scan_one_descendant(state: _DescendantScanState, run_id: str) -> int:
    if run_id in state.seen:
        return 0
    state.seen.add(run_id)
    record = _read_child_task_record(state.workspace, run_id)
    if not record:
        state.items.append(_DescendantStatus(run_id, "", "", "MISSING", "UNVERIFIED"))
        return 1
    state.items.append(_status_from_record(run_id, record))
    state.queue.extend(child_id for child_id in _string_list(record.get("child_ids", [])) if child_id not in state.seen)
    return 1


# LLM: _is_healthy defines the parent acceptance truth source for descendant completion.
# 函数用途: 判断后代是否已经健康收口；DONE 必须 VERIFIED，接管/放弃视为已由恢复链路收口。
def _is_healthy(item: _DescendantStatus) -> bool:
    if (item.status, item.verification_status) == _HEALTHY_DONE:
        return True
    return item.status in _CLOSED_STATUSES


# LLM: _child_workspace derives sibling run dirs from the current task_dir.
# 函数用途: 通过当前 run 的 task_dir 定位同一个 subagents workspace；失败时保守返回 None。
def _child_workspace(task: Any) -> Path | None:
    try:
        task_dir = Path(str(getattr(task, "task_dir", "") or "")).expanduser()
    except OSError:
        return None
    if not str(task_dir):
        return None
    return task_dir.parent


# LLM: _read_child_task_record is a bounded exact JSON read, never a glob scan.
# 函数用途: 读取 child_id/task.json，并限制大小，防止验收读取大文件或误扫工作区。
def _read_child_task_record(workspace: Path | None, run_id: str) -> dict[str, object]:
    if workspace is None:
        return {}
    path = workspace / run_id / "task.json"
    try:
        if path.stat().st_size > _DESCENDANT_SCAN_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _status_from_record normalizes one persisted task record for stable acceptance output.
# 函数用途: 从 task.json 提取 run_id、role、agent_name、status 和 verification_status。
def _status_from_record(run_id: str, record: dict[str, object]) -> _DescendantStatus:
    return _DescendantStatus(
        run_id=str(record.get("id") or run_id),
        role=str(record.get("role") or ""),
        agent_name=str(record.get("agent_name") or ""),
        status=str(record.get("status") or "").upper(),
        verification_status=str(record.get("verification_status") or "").upper(),
    )


# LLM: _descendant_health_message gives recovery agents concrete run ids instead of vague failure text.
# 函数用途: 生成后代健康门的验收消息，列出最多 6 个阻塞后代状态。
def _descendant_health_message(state: _DescendantScanState, blocked: list[_DescendantStatus]) -> str:
    if state.workspace is None:
        return "无法定位 child workspace，不能证明后代健康收口。"
    if not state.items:
        return "父任务有 child_ids，但没有读到任何后代 task.json。"
    if not blocked:
        return f"已扫描 {len(state.items)} 个后代 run，全部健康收口。"
    examples = ", ".join(_status_label(item) for item in blocked[:6])
    return f"仍有 {len(blocked)} 个后代未健康收口，不能验收父级: {examples}"


# LLM: _status_label keeps blocked descendant examples compact for model-readable recovery.
# 函数用途: 将一个阻塞后代格式化成 run_id/role/name/status/verify。
def _status_label(item: _DescendantStatus) -> str:
    label = item.agent_name or item.role or "unknown"
    return f"{item.run_id}({label} status={item.status} verify={item.verification_status})"


# LLM: _string_list normalizes persisted child_ids without trusting model prose.
# 函数用途: 将 child_ids 等字段转为干净字符串列表。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
