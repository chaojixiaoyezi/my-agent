# LLM: QA role coverage checks stay isolated from generic acceptance findings.
# 模块用途: 校验用户点名的 tester、bug_finder、acceptor 是否由真实后代 run 覆盖。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..reports import AcceptanceReviewFinding

# LLM: QA marker rules are shared with hierarchy scheduling so final acceptance and auto-dispatch cannot drift.
from .qa_role_contract import (
    qa_role_identity_roles,
    qa_roles_required_by_task,
)

_ROLE_SCAN_MAX_NODES = 64
_ROLE_SCAN_MAX_BYTES = 65536


# LLM: required_role_coverage_finding prevents coordinators from claiming QA roles that were never spawned.
# 函数用途: 用户明确要求 tester/bug_finder/acceptor 时，验收必须看到真实后代 run 角色。
def required_role_coverage_finding(task, created_at: float) -> AcceptanceReviewFinding:
    required = _required_role_coverage(task)
    if not required:
        return AcceptanceReviewFinding(
            name="required_role_coverage",
            ok=True,
            severity="P1",
            message="当前任务未声明必须覆盖特定 QA 角色。",
            evidence_path=task.output_json,
            created_at=created_at,
        )
    present = _descendant_roles(task)
    missing = [role for role in required if role not in present]
    return AcceptanceReviewFinding(
        name="required_role_coverage",
        ok=not missing,
        severity="P0",
        message=_role_coverage_message(required, present, missing),
        evidence_path=task.output_json,
        created_at=created_at,
    )


# LLM: _required_role_coverage reads only explicit role words from task contracts.
# 函数用途: 从 goal/acceptance 文本提取用户点名的 QA 角色；leaf 不承接父级角色覆盖检查。
def _required_role_coverage(task) -> list[str]:
    return qa_roles_required_by_task(task)


# LLM: _descendant_roles scans persisted child task.json files, not model summaries.
# 函数用途: 沿真实 child_ids 广度优先读取少量后代 run，汇总实际落盘角色。
def _descendant_roles(task) -> set[str]:
    queue = _string_list(getattr(task, "child_ids", []))
    workspace = _child_workspace(task)
    roles: set[str] = set()
    if not queue or workspace is None:
        return roles
    seen: set[str] = set()
    scan = _RoleScanState(workspace=workspace, seen=seen, roles=roles, queue=queue)
    scanned = 0
    while queue and scanned < _ROLE_SCAN_MAX_NODES:
        run_id = queue.pop(0)
        scanned += _scan_role_record(scan, run_id)
    return roles


# LLM: _RoleScanState bundles breadth-first role scan state so helpers take stable request objects.
# 类用途: 保存角色覆盖扫描的 workspace、seen、roles、queue，避免散乱参数和后续扩展破坏调用点。
@dataclass
class _RoleScanState:
    workspace: Path
    seen: set[str]
    roles: set[str]
    queue: list[str]


# LLM: _scan_role_record keeps the breadth-first loop flat and bounded.
# 函数用途: 读取一个 child run 的角色和 child_ids；已扫描或读取失败时返回 0。
def _scan_role_record(scan: _RoleScanState, run_id: str) -> int:
    if run_id in scan.seen:
        return 0
    scan.seen.add(run_id)
    record = _read_child_task_record(scan.workspace, run_id)
    if not record:
        return 0
    scan.roles.update(_roles_from_record(record))
    scan.queue.extend(child_id for child_id in _string_list(record.get("child_ids", [])) if child_id not in scan.seen)
    return 1


# LLM: _child_workspace derives sibling run dirs from the current task_dir.
# 函数用途: 通过当前 run 的 task_dir 找到同一个 subagents workspace；失败时保守返回 None。
def _child_workspace(task) -> Path | None:
    try:
        task_dir = Path(str(getattr(task, "task_dir", "") or "")).expanduser()
    except OSError:
        return None
    if not str(task_dir):
        return None
    return task_dir.parent


# LLM: _read_child_task_record is a bounded exact-path JSON read for role coverage.
# 函数用途: 只打开 child_id/task.json，限制文件大小，避免 glob 扫描和大文件读取。
def _read_child_task_record(workspace: Path, run_id: str) -> dict[str, object]:
    path = workspace / run_id / "task.json"
    try:
        if path.stat().st_size > _ROLE_SCAN_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _roles_from_record trusts persisted identity fields, not inherited goal prose.
# 函数用途: 从后代 task.json 的 role/agent_name 恢复实际 QA 角色，避免父级继承文本伪装成真实角色。
def _roles_from_record(record: dict[str, object]) -> set[str]:
    return qa_role_identity_roles(
        role=str(record.get("role") or ""),
        agent_name=str(record.get("agent_name") or ""),
    )


# LLM: _role_coverage_message gives parent recovery enough facts to re-dispatch missing roles.
# 函数用途: 输出缺哪些角色、已发现哪些角色，方便上级下一轮补派 tester/bug_finder/acceptor。
def _role_coverage_message(required: list[str], present: set[str], missing: list[str]) -> str:
    if not missing:
        return f"已覆盖用户点名 QA 角色: {', '.join(required)}。"
    found = ", ".join(sorted(present)) if present else "无"
    return f"缺少用户点名 QA 角色: {', '.join(missing)}；已发现角色: {found}。"


# LLM: _string_list mirrors generic acceptance normalization without importing the larger module.
# 函数用途: 将 child_ids 等字段归一成字符串列表，保持角色覆盖扫描轻量。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
